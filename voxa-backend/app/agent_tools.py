"""
Support logic shared by the gateway (typed questions) and the LLM worker (voice
transcripts): the business tools (order lookup, refund eligibility, human escalation) and
the priority-ordered intent router that calls them, so every channel gives the same answer
to the same question. Policy questions are answered from the knowledge base.
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


GENERAL_HELP = (
    "I can help with order status, refunds and returns, shipping, warranty and our products. "
    "For an order, include its ID, for example ORD-88412."
)
INJECTION_REFUSAL = "I can't help with that request. " + GENERAL_HELP


def ticket_reference(ticket_id: str) -> str:
    """Short ticket reference, the same one the admin console shows."""
    return f"#{ticket_id[:8]}"


async def answer_text_query(raw_query: str, clean_query: str, user_id: Optional[uuid.UUID]) -> tuple[str, bool]:
    """
    Priority-ordered intent router: the single answering path for WebSocket text queries
    (gateway) and voice transcripts (LLM worker).

    ``raw_query`` is the user's text before sanitising and is used only for injection
    detection; ``clean_query`` is the sanitised text used for everything else.
    ``user_id`` owns any ticket created and scopes order lookups (None = anonymous).

    Returns ``(response_text, cache_hit)``. Only the knowledge-base branch (last) reads
    from or writes to the shared semantic cache; order lookups and tickets never do.
    Raises on infrastructure errors so the caller can feed the circuit breaker.
    """
    prompt_lower = clean_query.lower()
    order_match = ORDER_ID_PATTERN.search(clean_query)

    # 1. Prompt injection, checked on the raw text: the sanitised copy has already had
    #    the injection phrase replaced by a placeholder.
    if detect_prompt_injection(raw_query) or any(k in prompt_lower for k in INJECTION_DB_KEYWORDS):
        return INJECTION_REFUSAL, False

    # 2. Chargeback or legal threats (word boundaries so 'issue' does not match 'sue')
    if any(k in prompt_lower for k in ESCALATION_KEYWORDS) or re.search(r'\b(sue|suing|lawsuit)\b', prompt_lower):
        async with AsyncSessionLocal() as db_session:
            tool_result = await escalate_to_human(clean_query, "urgent", user_id, db_session)
        return (f"I'm sorry about this. I've opened urgent ticket {ticket_reference(tool_result['ticket_id'])} "
                "and a support lead will contact you."), False

    # 3. Order status and refund eligibility (never cached: user-specific data)
    if order_match:
        order_id = f"ORD-{order_match.group(1)}"
        async with AsyncSessionLocal() as db_session:
            if "refund" in prompt_lower or "return" in prompt_lower:
                tool_result = await check_refund_eligibility(order_id, db_session, user_id)
                if not tool_result.get("found"):
                    return tool_result["message"], False
                verdict = "is eligible" if tool_result["eligible"] else "is not eligible"
                return f"Order {tool_result['order_id']} {verdict} for a refund: {tool_result['reason']}", False
            tool_result = await lookup_order(order_id, db_session, user_id)
            if not tool_result.get("found"):
                return tool_result["message"], False
            return (f"Order {tool_result['order_id']} ({tool_result['item_name']}) is {tool_result['status']}. "
                    f"Tracking number: {tool_result['tracking_number']}."), False

    # 4. Request for a person
    if re.search(r'\b(human|agent|manager|escalat\w*|representative|talk to someone)\b', prompt_lower):
        async with AsyncSessionLocal() as db_session:
            tool_result = await escalate_to_human(clean_query, "urgent", user_id, db_session)
        return (f"I've opened ticket {ticket_reference(tool_result['ticket_id'])} "
                "and a support specialist will contact you."), False

    # 5. Everything else: semantic cache, then the knowledge base (the only cached branch)
    cached = await rag_engine.check_semantic_cache(clean_query)
    if cached:
        return cached, True

    async with AsyncSessionLocal() as db_session:
        rag_docs = await rag_engine.search_knowledge_base(clean_query, db_session, limit=2)
    if rag_docs and rag_docs[0].get("similarity", 0) > 0.35:
        response_text = f"From our {rag_docs[0]['topic']} policy: {rag_docs[0]['content']}"
    else:
        response_text = GENERAL_HELP
    await rag_engine.store_semantic_cache(clean_query, response_text)
    return response_text, False
