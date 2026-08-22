import uuid
from datetime import datetime, timezone
from typing import Dict, Any, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models import Order, SupportTicket

# Deterministic tool schemas for LLM Function Calling
TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "lookup_order",
            "description": "Fetch real-time shipping status and tracking info for a customer order ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {
                        "type": "string",
                        "description": "The order ID, e.g. ORD-88412 or ORD-10294"
                    }
                },
                "required": ["order_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "check_refund_eligibility",
            "description": "Check if an order is eligible for a return/refund (must be within 30 days of purchase).",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {
                        "type": "string",
                        "description": "The order ID to check refund eligibility for"
                    }
                },
                "required": ["order_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "escalate_to_human",
            "description": "Create an urgent human support ticket when the customer is angry or the issue cannot be resolved by AI.",
            "parameters": {
                "type": "object",
                "properties": {
                    "issue_summary": {
                        "type": "string",
                        "description": "Concise explanation of the customer issue"
                    },
                    "priority": {
                        "type": "string",
                        "enum": ["medium", "high", "urgent"],
                        "description": "Priority level for the ticket"
                    }
                },
                "required": ["issue_summary"]
            }
        }
    }
]

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

async def escalate_to_human(issue_summary: str, priority: str = "urgent", user_id: Optional[uuid.UUID] = None, db: AsyncSession = None) -> Dict[str, Any]:
    """Creates a high-priority ticket in the support_tickets table."""
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
