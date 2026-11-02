"""Gateway start-up, demo-data seeding and the migration entry point, without a database."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

import app.main as main
import app.migrate as migrate
import app.seed_data as seed_data
from app.models import KnowledgeBase, Order, User


def test_gateway_starts_even_if_seeding_redis_and_the_model_fail(monkeypatch):
    """Each start-up step logs and carries on, so /health answers and the operator sees the warnings."""
    monkeypatch.setattr(main, "seed_database", AsyncMock(side_effect=OSError("database not ready")))
    monkeypatch.setattr(main.queue_manager, "init_consumer_groups", AsyncMock(side_effect=ConnectionError("redis down")))
    monkeypatch.setattr(main.rag_engine, "generate_embedding_async", AsyncMock(side_effect=RuntimeError("no model")))

    with TestClient(main.app) as client:
        assert client.get("/health").json()["status"] == "healthy"

    main.seed_database.assert_awaited_once()
    main.queue_manager.init_consumer_groups.assert_awaited_once()


class SeedSession:
    """Answers the seed's three lookups (admin user, demo order, existing topics) from fixed values."""
    def __init__(self, admin=None, order=None, topics=()):
        self.lookups = [admin, order, list(topics)]
        self.added = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt):
        value = self.lookups.pop(0)
        result = MagicMock()
        result.scalar_one_or_none.return_value = value
        result.scalars.return_value.all.return_value = value
        return result

    def add(self, obj):
        self.added.append(obj)

    def add_all(self, objs):
        self.added.extend(objs)

    async def commit(self):
        pass


def run_seed(monkeypatch, session):
    embed = AsyncMock(return_value=[0.0] * 384)
    monkeypatch.setattr(seed_data, "AsyncSessionLocal", lambda: session)
    monkeypatch.setattr(seed_data, "get_password_hash_async", AsyncMock(return_value="hashed"))
    monkeypatch.setattr(seed_data.rag_engine, "generate_embedding_async", embed)
    asyncio.run(seed_data.seed_database())
    return embed


def test_seed_adds_the_demo_data_and_embeds_each_article_with_its_title(monkeypatch):
    session = SeedSession()

    embed = run_seed(monkeypatch, session)

    users = [o for o in session.added if isinstance(o, User)]
    orders = [o for o in session.added if isinstance(o, Order)]
    articles = [o for o in session.added if isinstance(o, KnowledgeBase)]
    assert sorted(u.email for u in users) == ["admin@voxa.ai", "demo@voxa.ai"]
    assert sorted(o.id for o in orders) == ["ORD-10294", "ORD-88412", "ORD-99120"]
    assert len(articles) == len(seed_data.DEFAULT_KNOWLEDGE_DOCS)
    first = seed_data.DEFAULT_KNOWLEDGE_DOCS[0]
    embed.assert_any_await(f"{first['topic']}. {first['content']}")


def test_seed_adds_nothing_when_the_data_is_already_there(monkeypatch):
    session = SeedSession(admin=object(), order=object(),
                          topics=[doc["topic"] for doc in seed_data.DEFAULT_KNOWLEDGE_DOCS])

    embed = run_seed(monkeypatch, session)

    assert session.added == [] and embed.await_count == 0


def test_seed_adds_only_the_missing_articles(monkeypatch):
    topics = [doc["topic"] for doc in seed_data.DEFAULT_KNOWLEDGE_DOCS]
    session = SeedSession(admin=object(), order=object(), topics=topics[1:])

    embed = run_seed(monkeypatch, session)

    assert [a.topic for a in session.added] == [topics[0]] and embed.await_count == 1


def test_migrate_stamps_an_old_database_before_upgrading(monkeypatch):
    calls = []
    monkeypatch.setattr(migrate.command, "stamp", lambda config, rev: calls.append(("stamp", rev)))
    monkeypatch.setattr(migrate.command, "upgrade", lambda config, rev: calls.append(("upgrade", rev)))

    monkeypatch.setattr(migrate, "_existing_tables", AsyncMock(return_value={"users", "orders"}))
    migrate.main()
    assert calls == [("stamp", migrate.BASELINE_REVISION), ("upgrade", "head")]

    calls.clear()
    monkeypatch.setattr(migrate, "_existing_tables", AsyncMock(return_value=set()))
    migrate.main()
    assert calls == [("upgrade", "head")]
