"""
Idempotent demo-data seeding: admin/demo users, three sample orders and the knowledge
base (with embeddings). Runs on every gateway start and only adds what is missing.
Run standalone with ``python -m app.seed_data``.
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
    },
    {
        "topic": "Bulk & Business Orders",
        "content": "Orders of 20 or more units of Earbuds Pro or Studio Headphones get a 20% to 25% volume discount, Net-30 invoicing with VAT/GST receipts and a 2-year warranty. Ask for a bulk quote and a support specialist will follow up."
    },
    {
        "topic": "International Shipping, Customs & VAT",
        "content": "International orders ship Delivered Duty Paid (DDP): local VAT and import duties are charged at checkout, so there are no extra fees on delivery. International delivery takes 7 to 14 business days with tracking."
    },
    {
        "topic": "Bluetooth Multipoint Pairing",
        "content": "Earbuds Pro and Studio Headphones support Bluetooth 5.3 multipoint, so they can stay connected to two devices at once, for example a phone and a laptop. Pair the first device, hold the case button for 3 seconds to enter pairing mode, then select 'Voxa Audio' on the second device. Audio switches to the phone for incoming calls."
    },
    {
        "topic": "Water Damage & Out-of-Warranty Replacement",
        "content": "The 1-year warranty covers manufacturing defects. It does not cover water damage beyond the IPX7 rating, such as swimming with the earbuds, or devices older than 12 months. In those cases we offer an out-of-warranty replacement at 35% off."
    },
    {
        "topic": "Price Protection",
        "content": "If an item you bought goes on sale within 14 days of your order, we refund the price difference to your original payment method within 3 to 5 business days."
    },
    {
        "topic": "Package Marked Delivered But Not Received",
        "content": "Couriers sometimes mark a package delivered up to 24 hours before it arrives. Check with neighbours, your building's reception and anyone at your address. If it still hasn't arrived after 24 hours, contact support with your order ID and we will send a free replacement."
    },
    {
        "topic": "Product Guide: Earbuds Pro vs Studio Headphones",
        "content": "Wireless Earbuds Pro ($149) suit workouts and travel: IPX7 water resistance and 32 hours of battery with the case. Smart Studio Headphones ($249) suit focus and listening at home or in the office: 42 dB active noise cancellation and a fuller sound. Choose the earbuds for portability and the headphones for noise cancellation."
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

        # 3. Knowledge base: insert any topic that is missing, so databases seeded by an
        #    older release also pick up new documents.
        existing_topics = set((await db.execute(select(KnowledgeBase.topic))).scalars().all())
        new_docs = [doc for doc in DEFAULT_KNOWLEDGE_DOCS if doc["topic"] not in existing_topics]
        if new_docs:
            logger.info(f"Embedding {len(new_docs)} knowledge-base documents...")
            for doc in new_docs:
                embedding_vec = await rag_engine.generate_embedding_async(doc["content"])
                db.add(KnowledgeBase(topic=doc["topic"], content=doc["content"], embedding=embedding_vec))
            await db.commit()
            
        logger.info("Database seeding complete")

async def _main():
    await init_db()
    await seed_database()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
