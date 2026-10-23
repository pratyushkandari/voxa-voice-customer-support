"""Deterministic business tools (order lookup, refund eligibility, human escalation)."""
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models import Order, SupportTicket

ORDER_ID_PATTERN = re.compile(r"\bord-\d{4,6}\b", re.IGNORECASE)
ESCALATION_PATTERN = re.compile(
    r"\b(angry|human|agent|manager|escalat\w*|terrible|lawyer)\b", re.IGNORECASE
)


def detect_tool_intent(prompt: str) -> Tuple[bool, str, dict]:
    """
    Deterministic intent router (<1ms) shared by the LLM worker and the REST gateway.

    Returns (has_tool, tool_name, arguments). Prompts that resolve to a tool produce
    user-specific or side-effecting answers (order data, new tickets) and therefore must
    never be served from, or written to, the shared semantic cache.
    """
    order_match = ORDER_ID_PATTERN.search(prompt)
    if order_match:
        order_id = order_match.group(0).upper()
        prompt_lower = prompt.lower()
        if "refund" in prompt_lower or "return" in prompt_lower:
            return True, "check_refund_eligibility", {"order_id": order_id}
        return True, "lookup_order", {"order_id": order_id}

    if ESCALATION_PATTERN.search(prompt):
        return True, "escalate_to_human", {"issue_summary": prompt, "priority": "urgent"}

    return False, "", {}


async def lookup_order(order_id: str, db: AsyncSession) -> Dict[str, Any]:
    """Fetches real-time status of an order from the PostgreSQL database."""
    stmt = select(Order).where(Order.id == order_id.strip().upper())
    result = await db.execute(stmt)
    order = result.scalar_one_or_none()
    
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

async def check_refund_eligibility(order_id: str, db: AsyncSession) -> Dict[str, Any]:
    """Calculates whether an order was purchased within the 30-day return window."""
    stmt = select(Order).where(Order.id == order_id.strip().upper())
    result = await db.execute(stmt)
    order = result.scalar_one_or_none()
    
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

async def execute_tool_call(tool_name: str, arguments: dict, user_id: Optional[uuid.UUID], db: AsyncSession) -> Dict[str, Any]:
    """Dispatcher for deterministic tool calls."""
    if tool_name == "lookup_order":
        return await lookup_order(arguments.get("order_id", ""), db)
    elif tool_name == "check_refund_eligibility":
        return await check_refund_eligibility(arguments.get("order_id", ""), db)
    elif tool_name == "escalate_to_human":
        return await escalate_to_human(arguments.get("issue_summary", ""), arguments.get("priority", "urgent"), user_id, db)
    else:
        return {"error": f"Unknown tool: {tool_name}"}
