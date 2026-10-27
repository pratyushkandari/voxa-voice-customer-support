import asyncio
import uuid
import datetime
from unittest.mock import AsyncMock, MagicMock
from app.agent_tools import check_refund_eligibility, lookup_order
from app.rag_engine import rag_engine

def test_lookup_order_tool():
    async def _test():
        mock_db_session = AsyncMock()
        mock_order = MagicMock()
        mock_order.id = "ORD-88412"
        mock_order.item_name = "Voxa Wireless AI Earbuds Pro"
        mock_order.order_date = datetime.datetime.now(datetime.timezone.utc)
        mock_order.status = "Shipped"
        mock_order.tracking_number = "1Z9999999999999999"
        
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_order
        mock_db_session.execute = AsyncMock(return_value=mock_result)

        result = await lookup_order("ORD-88412", mock_db_session, uuid.uuid4())

        assert result["found"] is True
        assert result["status"] == "Shipped"
        assert result["tracking_number"] == "1Z9999999999999999"
        assert result["order_id"] == "ORD-88412"

    asyncio.run(_test())

def test_check_refund_eligibility_tool():
    async def _test():
        mock_db_session = AsyncMock()
        mock_order = MagicMock()
        mock_order.id = "ORD-88412"
        mock_order.order_date = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=10)
        
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_order
        mock_db_session.execute = AsyncMock(return_value=mock_result)

        result = await check_refund_eligibility("ORD-88412", mock_db_session, uuid.uuid4())

        assert result["found"] is True
        assert result["eligible"] is True
        assert "Within 30-day" in result["reason"]

    asyncio.run(_test())

def test_rag_cosine_similarity():
    async def _test():
        mock_db_session = AsyncMock()
        
        # Mock database row returned from pgvector query
        mock_row = MagicMock()
        mock_row.topic = "Return Policy"
        mock_row.content = "Customers are eligible for a full refund within 30 days."
        mock_row.similarity = 0.95
        
        mock_result = MagicMock()
        mock_result.fetchall.return_value = [mock_row]
        mock_db_session.execute = AsyncMock(return_value=mock_result)

        docs = await rag_engine.search_knowledge_base("How do refunds work?", mock_db_session, limit=1)
        
        assert len(docs) == 1
        assert docs[0]["topic"] == "Return Policy"
        assert docs[0]["similarity"] == 0.95

    asyncio.run(_test())
