"""
Idempotent demo-data seeding: admin/demo users, three sample orders and the RAG
knowledge base (with embeddings). Runs on every gateway start; each block is skipped
when its data already exists. Run standalone with ``python -m app.seed_data``.
"""
import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.auth import get_password_hash_async
from app.config import settings
from app.database import AsyncSessionLocal, init_db
from app.models import KnowledgeBase, Order, User
from app.rag_engine import rag_engine

logger = logging.getLogger("voxa.seed")

DEFAULT_KNOWLEDGE_DOCS = [
    {
        "topic": "Return Policy & Refunds",
        "content": "Customers are eligible for a full refund or exchange within 30 calendar days of the original purchase date. Items must be in original packaging and in working condition. Once received, refunds are processed to the original payment method within 3 to 5 business days."
    },
    {
        "topic": "Shipping & Expedited Delivery",
        "content": "Standard domestic shipping takes 3 to 5 business days. Expedited priority shipping delivers within 1 to 2 business days for an additional $15 fee. International delivery takes 7 to 14 business days depending on customs clearance. All packages include real-time tracking numbers."
    },
    {
        "topic": "Hardware Warranty & Defective Items",
        "content": "All hardware devices sold by Voxa are backed by an extensive 1-Year Limited Manufacturer Warranty covering defects in materials and craftsmanship. If your item arrives broken or stops functioning under normal use, contact support for an immediate prepaid replacement shipping label."
    },
    {
        "topic": "Subscription Cancellation & Billing",
        "content": "You can cancel your recurring monthly or annual plan at any time from your account settings. Upon cancellation, your access remains active until the end of your current billing cycle. We offer prorated refunds if requested within 48 hours of billing renewal."
    },
    {
        "topic": "Order Tracking & Status Guide",
        "content": "To check your order status, provide your order ID format ORD-XXXXX. Status states include 'Processing' (order being packed), 'Shipped' (in transit with courier), and 'Delivered' (successfully left at shipping address)."
    }
]

async def seed_database():
    """Seeds demo data. Assumes the schema exists (call ``init_db()`` first)."""
    async with AsyncSessionLocal() as db:
        # 1. Seed Admin & Demo Users
        admin_check = await db.execute(select(User).where(User.email == "admin@voxa.ai"))
        if not admin_check.scalar_one_or_none():
            logger.info("Seeding admin user (admin@voxa.ai)...")
            admin_user = User(
                id=uuid.uuid4(),
                email="admin@voxa.ai",
                hashed_password=await get_password_hash_async(settings.SEED_ADMIN_PASSWORD),
                role="admin"
            )
            demo_user = User(
                id=uuid.uuid4(),
                email="demo@voxa.ai",
                hashed_password=await get_password_hash_async("demopassword123"),
                role="user"
            )
            db.add_all([admin_user, demo_user])
            await db.commit()
            
        # 2. Seed Simulated Orders
        order_check = await db.execute(select(Order).where(Order.id == "ORD-88412"))
        if not order_check.scalar_one_or_none():
            logger.info("Seeding simulated business orders...")
            now = datetime.now(timezone.utc)
            orders = [
                Order(
                    id="ORD-88412",
                    item_name="Voxa Wireless AI Earbuds Pro",
                    order_date=now - timedelta(days=5),
                    status="Shipped",
                    tracking_number="TRK-99281744US"
                ),
                Order(
                    id="ORD-10294",
                    item_name="Voxa Smart Studio Noise-Cancelling Microphone",
                    order_date=now - timedelta(days=45), # Exceeds 30-day refund window!
                    status="Delivered",
                    tracking_number="TRK-88172635US"
                ),
                Order(
                    id="ORD-99120",
                    item_name="Voxa Ultra 4K Conference Webcam",
                    order_date=now - timedelta(days=1),
                    status="Processing",
                    tracking_number=None
                )
            ]
            db.add_all(orders)
            await db.commit()

        # 3. Seed Knowledge Base Embeddings (pgvector)
        kb_check = await db.execute(select(KnowledgeBase))
        if not kb_check.scalars().first():
            logger.info("Generating embeddings and seeding RAG knowledge base...")
            kb_entries = []
            for doc in DEFAULT_KNOWLEDGE_DOCS:
                embedding_vec = await rag_engine.generate_embedding_async(doc["content"])
                kb_entries.append(KnowledgeBase(
                    topic=doc["topic"],
                    content=doc["content"],
                    embedding=embedding_vec
                ))
            db.add_all(kb_entries)
            await db.commit()
            
        logger.info("Database seeding complete")

async def _main():
    await init_db()
    await seed_database()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
