"""
Support logic shared by the gateway (typed questions) and the LLM worker (voice
transcripts): the business tools (order lookup, refund eligibility, human escalation) and
the priority-ordered intent router that calls them.

The router decides what data a question needs and fetches it, with the owner checks
applied. It returns those facts for Gemini to phrase (``app.llm``) together with a plain
reply built from the same facts, which is used whenever Gemini can't be.
"""
import re
import uuid
from dataclasses import dataclass
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


@dataclass
class RoutedQuery:
    """
    What the router found for one question.

    ``fallback`` is a complete reply built from the facts; it is sent whenever Gemini is
    skipped or fails. ``facts`` is everything Gemini may use to write the reply. When it is
    empty the fallback is the final answer (a blocked injection attempt or a cache hit).
    """
    fallback: str
    facts: str = ""
    cacheable: bool = False  # user-independent knowledge answer, may go in the semantic cache
    cache_hit: bool = False


async def route_query(raw_query: str, clean_query: str, user_id: Optional[uuid.UUID]) -> RoutedQuery:
    """
    Priority-ordered intent router, shared by the gateway and the LLM worker.

    ``raw_query`` is the user's text before sanitising and is used only for injection
    detection; ``clean_query`` is the sanitised text used for everything else.
    ``user_id`` owns any ticket created and scopes order lookups (None = anonymous).

    Only the knowledge-base branch (last) reads the shared semantic cache or is marked
    cacheable; order lookups and tickets are user-specific. Raises on database errors.
    """
    prompt_lower = clean_query.lower()
    order_match = ORDER_ID_PATTERN.search(clean_query)

    # 1. Prompt injection, checked on the raw text: the sanitised copy has already had
    #    the injection phrase replaced by a placeholder. Never forwarded to Gemini.
    if detect_prompt_injection(raw_query) or any(k in prompt_lower for k in INJECTION_DB_KEYWORDS):
        return RoutedQuery(INJECTION_REFUSAL)

    # 2. Chargeback or legal threats (word boundaries so 'issue' does not match 'sue')
    if any(k in prompt_lower for k in ESCALATION_KEYWORDS) or re.search(r'\b(sue|suing|lawsuit)\b', prompt_lower):
        async with AsyncSessionLocal() as db_session:
            tool_result = await escalate_to_human(clean_query, "urgent", user_id, db_session)
        ref = ticket_reference(tool_result["ticket_id"])
        return RoutedQuery(
            fallback=f"I'm sorry about this. I've opened urgent ticket {ref} and a support lead will contact you.",
            facts=f"Opened urgent support ticket {ref} for this complaint. A support lead will contact the customer.",
        )

    # 3. Order status and refund eligibility
    if order_match:
        order_id = f"ORD-{order_match.group(1)}"
        async with AsyncSessionLocal() as db_session:
            if "refund" in prompt_lower or "return" in prompt_lower:
                tool_result = await check_refund_eligibility(order_id, db_session, user_id)
                if not tool_result["found"]:
                    return RoutedQuery(tool_result["message"], facts=f"No order {order_id} was found for this customer.")
                verdict = "is eligible" if tool_result["eligible"] else "is not eligible"
                return RoutedQuery(
                    fallback=f"Order {tool_result['order_id']} {verdict} for a refund: {tool_result['reason']}",
                    facts=(f"Order {tool_result['order_id']} was bought {tool_result['days_since_purchase']} days ago. "
                           f"Refunds are allowed within 30 days of purchase, so it {verdict} for a refund."),
                )
            tool_result = await lookup_order(order_id, db_session, user_id)
        if not tool_result["found"]:
            return RoutedQuery(tool_result["message"], facts=f"No order {order_id} was found for this customer.")
        return RoutedQuery(
            fallback=(f"Order {tool_result['order_id']} ({tool_result['item_name']}) is {tool_result['status']}. "
                      f"Tracking number: {tool_result['tracking_number']}."),
            facts=(f"Order {tool_result['order_id']}: {tool_result['item_name']}, ordered {tool_result['order_date'][:10]}, "
                   f"status {tool_result['status']}, tracking number {tool_result['tracking_number']}."),
        )

    # 4. Request for a person
    if re.search(r'\b(human|agent|manager|escalat\w*|representative|talk to someone)\b', prompt_lower):
        async with AsyncSessionLocal() as db_session:
            tool_result = await escalate_to_human(clean_query, "urgent", user_id, db_session)
        ref = ticket_reference(tool_result["ticket_id"])
        return RoutedQuery(
            fallback=f"I've opened ticket {ref} and a support specialist will contact you.",
            facts=f"Opened support ticket {ref}. A support specialist will contact the customer.",
        )

    # 5. Everything else: semantic cache, then the knowledge base
    cached = await rag_engine.check_semantic_cache(clean_query)
    if cached:
        return RoutedQuery(cached, cache_hit=True)

    async with AsyncSessionLocal() as db_session:
        rag_docs = await rag_engine.search_knowledge_base(clean_query, db_session, limit=3)
    passages = [doc for doc in rag_docs if doc.get("similarity", 0) > 0.35]
    if not passages:
        return RoutedQuery(
            GENERAL_HELP,
            facts=f"No help-centre article matches this message. What Voxa support can do: {GENERAL_HELP}",
            cacheable=True,
        )
    return RoutedQuery(
        fallback=f"From our {passages[0]['topic']} policy: {passages[0]['content']}",
        facts="\n\n".join(f"{doc['topic']}: {doc['content']}" for doc in passages),
        cacheable=True,
    )
