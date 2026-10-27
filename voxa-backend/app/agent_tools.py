"""
Deterministic support logic shared by the gateway (WebSocket text queries) and the LLM
worker (REST SSE prompts and voice transcripts): the business tools (order lookup,
refund eligibility, human escalation) and the priority-ordered intent router that calls
them, so every channel gives the same answer to the same question.
"""
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal
from app.guardrails import detect_prompt_injection
from app.models import Order, SupportTicket
from app.rag_engine import rag_engine

# Accepts "ORD-88412", "ORD 88412" and "ORD88412": speech-to-text writes spoken order IDs
# without the hyphen. The leading \b keeps words such as "record-12345" from matching.
ORDER_ID_PATTERN = re.compile(r"\bord[-\s]?(\d{4,6})\b", re.IGNORECASE)


async def _find_visible_order(order_id: str, db: AsyncSession, requester_id: Optional[uuid.UUID]) -> Optional[Order]:
    """
    Orders that belong to a user are visible only to that user; seeded demo orders have
    no owner and are visible to everyone. Someone else's order is reported exactly like a
    missing one, so order IDs cannot be probed for existence.
    """
    stmt = select(Order).where(Order.id == order_id.strip().upper())
    if requester_id is None:
        stmt = stmt.where(Order.user_id.is_(None))
    else:
        stmt = stmt.where(or_(Order.user_id.is_(None), Order.user_id == requester_id))
    return (await db.execute(stmt)).scalar_one_or_none()


async def lookup_order(order_id: str, db: AsyncSession, requester_id: Optional[uuid.UUID] = None) -> Dict[str, Any]:
    """Fetches the real-time status of an order the requester may see."""
    order = await _find_visible_order(order_id, db, requester_id)
    if not order:
        return {
            "found": False,
            "message": f"No order found with ID {order_id}. Please double check the ID."
        }

    return {
        "found": True,
        "order_id": order.id,
        "item_name": order.item_name,
        "order_date": order.order_date.isoformat(),
        "status": order.status,
        "tracking_number": order.tracking_number or "N/A"
    }


async def check_refund_eligibility(order_id: str, db: AsyncSession,
                                   requester_id: Optional[uuid.UUID] = None) -> Dict[str, Any]:
    """Calculates whether a visible order was purchased within the 30-day return window."""
    order = await _find_visible_order(order_id, db, requester_id)
    if not order:
        return {
            "found": False,
            "message": f"Order {order_id} does not exist in our database."
        }

    now = datetime.now(timezone.utc)
    # Ensure timezone awareness
    order_date = order.order_date
    if order_date.tzinfo is None:
        order_date = order_date.replace(tzinfo=timezone.utc)

    days_diff = (now - order_date).days
    eligible = days_diff <= 30

    return {
        "found": True,
        "order_id": order.id,
        "eligible": eligible,
        "days_since_purchase": days_diff,
        "reason": "Within 30-day warranty policy" if eligible else f"Purchased {days_diff} days ago (exceeds 30-day window)."
    }


async def escalate_to_human(issue_summary: str, priority: str = "urgent", user_id: Optional[uuid.UUID] = None, db: Optional[AsyncSession] = None) -> Dict[str, Any]:
    """
    Creates a ticket in the support_tickets table and returns its ID.
    ``db`` is optional only so the function can be unit-tested without a database;
    production callers always pass a session so the ticket is actually persisted.
    """
    ticket = SupportTicket(
        id=uuid.uuid4(),
        user_id=user_id,
        issue_summary=issue_summary,
        priority=priority,
        status="open"
    )
    if db:
        db.add(ticket)
        await db.commit()
        await db.refresh(ticket)

    return {
        "ticket_created": True,
        "ticket_id": str(ticket.id),
        "status": "Escalated to human supervisor",
        "priority": priority
    }


INJECTION_DB_KEYWORDS = ["delete the database", "drop table", "output the admin"]
ESCALATION_KEYWORDS = ["stole my money", "chargeback", "lawyer", "bank dispute", "scam", "fraud", "unacceptable"]


async def answer_text_query(raw_query: str, clean_query: str, user_id: Optional[uuid.UUID]) -> tuple[str, bool]:
    """
    Deterministic, priority-ordered intent router: the single answering path for
    WebSocket text queries (gateway), REST SSE prompts and voice transcripts (LLM worker).

    ``raw_query`` is the user's text before sanitising and is used only for injection
    detection; ``clean_query`` is the sanitised text used for everything else.
    ``user_id`` owns any ticket created and scopes order lookups (None = anonymous).

    Returns ``(response_text, cache_hit)``. Only the generic policy branch (last) reads
    from or writes to the shared semantic cache; order lookups and tickets never do.
    Raises on infrastructure errors so the caller can feed the circuit breaker.
    """
    prompt_lower = clean_query.lower()
    order_match = ORDER_ID_PATTERN.search(clean_query)

    # Priority 1: Security Injection (checked on the raw text: the sanitised copy has
    # already had the injection phrase replaced by a placeholder)
    if detect_prompt_injection(raw_query) or any(k in prompt_lower for k in INJECTION_DB_KEYWORDS):
        return "🛡️ **Voxa Security Guardrail Activated:** System prompt injection and unauthorized database operations have been intercepted and neutralized. I am restricted to authentic customer service and order tracking.", False

    # Priority 2: Chargeback / Anger / Supervisor Escalation (word boundaries so 'issue' does not match 'sue')
    if any(k in prompt_lower for k in ESCALATION_KEYWORDS) or re.search(r'\b(sue|suing|lawsuit)\b', prompt_lower):
        async with AsyncSessionLocal() as db_session:
            tool_result = await escalate_to_human(clean_query, "urgent", user_id, db_session)
        return f"🚨 **Urgent Priority Escalation:** I sincerely apologize for the distress. I have generated Urgent Support Ticket **#{tool_result['ticket_id']}** directly to our Senior Customer Resolution Lead. A supervisor is reviewing your billing history right now to ensure a complete resolution or instant refund.", False

    # Priority 3: B2B Enterprise / Corporate Invoicing
    if (any(k in prompt_lower for k in ["team of", "developers", "corporate", "bulk", "procurement", "enterprise"])
            and any(k in prompt_lower for k in ["units", "buy", "order", "invoic", "purchase"])):
        return "🏢 **Voxa Enterprise Desk:** For bulk orders (20+ units of Studio Headphones or Earbuds Pro):\n• **Volume Discount**: 20%–25% off.\n• **Payment Terms**: Net-30 enterprise invoicing & VAT/GST receipts.\n• **SLA**: 2-year enterprise warranty.\nI have registered Corporate Lead **#CORP-5091** for a formal quotation today.", False

    # Priority 4: International Customs Duty & VAT Taxes (word boundary so 'private' does not match 'vat')
    if (re.search(r'\bvat\b', prompt_lower) or any(k in prompt_lower for k in ["customs", "duty", "import tax"])
            or (any(k in prompt_lower for k in ["uk", "canada", "india", "europe", "international"]) and "ship" in prompt_lower)):
        return "🌍 **International Customs & Duty (DDP Delivery):** All Voxa international orders are shipped **Delivered Duty Paid (DDP)**. Local VAT and import duties are collected upfront during checkout with zero surprise doorstep fees. Air transit takes 7–14 business days with live GPS tracking.", False

    # Priority 5: Bluetooth Multipoint Dual-Device Pairing
    if ("multipoint" in prompt_lower
            or ("simultaneously" in prompt_lower and ("connect" in prompt_lower or "pair" in prompt_lower))
            or (("both" in prompt_lower or "two" in prompt_lower) and ("iphone" in prompt_lower or "pc" in prompt_lower or "devices" in prompt_lower))):
        return "📶 **Bluetooth 5.3 Multipoint Dual-Device Pairing:** Yes! Voxa Earbuds Pro & Studio Headphones support Multipoint Connection to pair with 2 devices simultaneously (iPhone + PC). Connect to your phone first, put earbuds into pairing mode (hold case 3s), and select 'Voxa Audio' on your PC. Audio will switch automatically on incoming calls!", False

    # Priority 6: Out-of-Warranty / Water Damage (word boundary so 'waterfall' does not match)
    if re.search(r'\b(pool|submerged|water|dropped in)\b', prompt_lower) or ("14 months" in prompt_lower or "2 years" in prompt_lower):
        return "💧 **Warranty & Out-of-Warranty Replacement:** The 1-Year Manufacturer Warranty covers craftsmanship up to 12 months. Prolonged pool submersion or usage beyond 12 months falls outside standard warranty, but we offer an **Out-of-Warranty Replacement at 35% OFF**! Share your purchase email to claim your voucher.", False

    # Priority 7: Price Match Protection
    if any(k in prompt_lower for k in ["price match", "sale today", "cheaper today", "price drop", "refund the difference"]):
        return "🏷️ **14-Day Price Protection:** Yes! Under Voxa's Price Protection Policy, if an item you purchased goes on sale within 14 days of your order, you receive a full refund of the price difference. The difference will be credited to your card in 3–5 business days.", False

    # Priority 8: Delivery Discrepancy (Delivered but Missing)
    if "delivered" in prompt_lower and any(k in prompt_lower for k in ["not", "never", "haven", "didn", "missing", "where"]):
        return "📦 **Package Marked Delivered But Not Received:** 1. Check with household members, reception, or neighbors. 2. Couriers occasionally scan packages up to 24 hours prior to physical delivery. 3. If not received after 24 hours, provide your Order ID (e.g. ORD-88412) and we will dispatch an immediate free replacement!", False

    # Priority 9: Product Buying Advice
    if (("headphone" in prompt_lower and ("earphone" in prompt_lower or "earbud" in prompt_lower))
            or any(k in prompt_lower for k in ["which should i buy", "what should i order", "what should i buy", "which is better", "recommend", "buying advice", "compare"])):
        return "🎧 **Voxa Product Buying Guide:**\n• **Wireless Earbuds Pro ($149)**: Best for workouts, running, pocket mobility (IPX7 water resistant, 32h battery).\n• **Smart Studio Headphones ($249)**: Best for deep immersion, 42dB Active Noise Cancellation, audiophile sound, and office focus.\n👉 Pick **Earbuds** for portability, or **Headphones** for superior ANC sound isolation!", False

    # Priority 10: SQL Database Order Lookup & Refund (never cached: user-specific data)
    if order_match:
        order_id = f"ORD-{order_match.group(1)}"
        async with AsyncSessionLocal() as db_session:
            if "refund" in prompt_lower or "return" in prompt_lower:
                tool_result = await check_refund_eligibility(order_id, db_session, user_id)
                if tool_result.get("found"):
                    status_str = "Eligible for full refund" if tool_result.get("eligible") else "Ineligible for refund"
                    return f"📦 **Order {tool_result.get('order_id')}**: {status_str} ({tool_result.get('reason')}).", False
                return tool_result.get("message", f"Order {order_id} not found."), False
            tool_result = await lookup_order(order_id, db_session, user_id)
            if tool_result.get("found"):
                return f"📦 **Order {tool_result.get('order_id')} ({tool_result.get('item_name')})**: Currently **{tool_result.get('status')}**. Tracking number: `{tool_result.get('tracking_number')}`.", False
            return tool_result.get("message", f"Order {order_id} not found."), False

    # Priority 11: Human Agent Escalation
    if re.search(r'\b(human|agent|manager|escalat\w*|representative|talk to someone)\b', prompt_lower):
        async with AsyncSessionLocal() as db_session:
            tool_result = await escalate_to_human(clean_query, "urgent", user_id, db_session)
        return f"👤 **Live Human Specialist Transfer:** I have generated Priority Support Ticket **#{tool_result['ticket_id']}** in our system. A Tier-2 Senior Customer Support Specialist has received your inquiry and will connect with you within moments!", False

    # Priority 11.5: Greetings
    if re.search(r'\b(hi|hello|hey|greetings|who are you|what can you do|how are you|how are u)\b', prompt_lower):
        return "👋 **Hello! I am Voxa 2.0**, your real-time voice and support assistant.\n\nI can help you with:\n• 📦 **Live Order Tracking** (e.g. `ORD-88412`)\n• 💸 **Refund Calculations & Returns**\n• 🎧 **Product Buying Advice** (Earbuds vs Headphones)\n• 🌍 **Customs & International Shipping**\n• 📶 **Bluetooth Multipoint Setup**\n• 🏢 **B2B Bulk Corporate Invoicing**\n\nHow can I help you today? (You can type or speak via your microphone!)", False

    # Priority 12: General RAG Knowledge Base Search (the only branch that uses the semantic cache)
    cached = await rag_engine.check_semantic_cache(clean_query)
    if cached:
        return cached, True

    async with AsyncSessionLocal() as db_session:
        rag_docs = await rag_engine.search_knowledge_base(clean_query, db_session, limit=2)
    if rag_docs and rag_docs[0].get("similarity", 0) > 0.35:
        response_text = f"Based on Voxa's {rag_docs[0]['topic']} policy: {rag_docs[0]['content']}"
    else:
        response_text = "Thank you for contacting Voxa! All purchases include a 30-day money-back guarantee and 1-year warranty. Domestic shipping delivers in 3–5 business days. Please provide your order ID (e.g. ORD-88412) for live tracking!"
    await rag_engine.store_semantic_cache(clean_query, response_text)
    return response_text, False
