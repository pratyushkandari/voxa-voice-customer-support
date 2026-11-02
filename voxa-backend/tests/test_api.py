"""
REST endpoints and the auth checks behind them, with the database replaced by a scripted
session: each query returns the next prepared result, in order.
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.auth import create_access_token, decode_access_token, get_password_hash
from app.database import get_db
from app.models import User

PASSWORD = "secret123"
PASSWORD_HASH = get_password_hash(PASSWORD)  # bcrypt is slow, so hash once


class ScriptedDB:
    """Stands in for an AsyncSession. ``results`` are returned by execute() in order."""
    def __init__(self, *results):
        self.results = list(results)
        self.added = []

    async def execute(self, stmt):
        value = self.results.pop(0)
        result = MagicMock()
        result.scalar_one_or_none.return_value = value
        result.scalar.return_value = value
        result.scalars.return_value.all.return_value = value
        return result

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        pass

    async def refresh(self, obj):
        obj.id = obj.id or uuid.uuid4()
        obj.created_at = obj.created_at or datetime.now(timezone.utc)


def make_user(role="user", email="riya@example.com"):
    return User(id=uuid.uuid4(), email=email, hashed_password=PASSWORD_HASH, role=role,
                created_at=datetime.now(timezone.utc))


def bearer(user, **claims):
    token = create_access_token({"sub": str(user.id), "role": user.role, **claims})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def api(monkeypatch):
    """A client whose requests use ``api.db`` as the database session; rate limits always pass."""
    monkeypatch.setattr(main.rate_limiter, "check_rate_limit", AsyncMock(return_value=True))
    state = SimpleNamespace(db=ScriptedDB())

    async def scripted_db():
        yield state.db

    main.app.dependency_overrides[get_db] = scripted_db
    state.client = TestClient(main.app)
    yield state
    main.app.dependency_overrides.clear()


# ----------------------------- Sign-up, login, guest -----------------------------

def test_signup_creates_a_user_and_returns_their_token(api):
    api.db = ScriptedDB(None)  # no user with this email yet

    res = api.client.post("/v1/auth/signup", json={"email": "new@example.com", "password": PASSWORD})

    assert res.status_code == 200, res.text
    body = res.json()
    created = api.db.added[0]
    assert body["user"]["email"] == "new@example.com" and body["user"]["role"] == "user"
    assert created.hashed_password != PASSWORD  # stored hashed
    assert decode_access_token(body["access_token"])["sub"] == str(created.id)


def test_signup_rejects_an_email_that_is_already_registered(api):
    api.db = ScriptedDB(make_user(email="taken@example.com"))

    res = api.client.post("/v1/auth/signup", json={"email": "taken@example.com", "password": PASSWORD})

    assert res.status_code == 400 and res.json()["detail"] == "Email already registered"
    assert api.db.added == []


@pytest.mark.parametrize("payload", [
    {"email": "not-an-email", "password": PASSWORD},
    {"email": "new@example.com", "password": "12345"},       # shorter than 6
    {"email": "new@example.com", "password": "x" * 129},     # longer than 128
    {"email": "new@example.com"},                            # missing password
], ids=["bad-email", "short-password", "long-password", "missing-password"])
def test_signup_validates_the_email_and_password(api, payload):
    assert api.client.post("/v1/auth/signup", json=payload).status_code == 422


def test_login_returns_a_token_for_the_right_password(api):
    user = make_user()
    api.db = ScriptedDB(user)

    res = api.client.post("/v1/auth/login", json={"email": user.email, "password": PASSWORD})

    assert res.status_code == 200
    assert decode_access_token(res.json()["access_token"])["sub"] == str(user.id)


@pytest.mark.parametrize("known_user", [True, False], ids=["wrong-password", "unknown-email"])
def test_login_gives_the_same_error_for_a_wrong_password_and_an_unknown_email(api, known_user):
    api.db = ScriptedDB(make_user() if known_user else None)

    res = api.client.post("/v1/auth/login", json={"email": "riya@example.com", "password": "wrong-password"})

    assert res.status_code == 401 and res.json()["detail"] == "Invalid email or password"


def test_guest_demo_creates_a_guest_account(api):
    api.db = ScriptedDB()

    res = api.client.post("/v1/auth/guest-demo")

    assert res.status_code == 200
    guest = api.db.added[0]
    assert guest.email.startswith("guest_") and guest.email.endswith("@voxa.demo") and guest.role == "user"
    assert decode_access_token(res.json()["access_token"])["sub"] == str(guest.id)


def test_credential_endpoints_return_429_with_retry_after_when_limited(api, monkeypatch):
    monkeypatch.setattr(main.rate_limiter, "check_rate_limit", AsyncMock(return_value=False))

    res = api.client.post("/v1/auth/guest-demo")

    assert res.status_code == 429 and res.headers["Retry-After"] == "60"


# ----------------------------- Admin access -----------------------------

def test_admin_endpoints_need_a_bearer_token(api):
    for path in ("/v1/admin/metrics", "/v1/admin/tickets"):
        assert api.client.get(path).status_code in (401, 403)  # FastAPI versions differ on missing credentials


@pytest.mark.parametrize("headers", [
    {"Authorization": "Bearer not-a-jwt"},
    {"Authorization": "Bearer " + create_access_token({"sub": str(uuid.uuid4()), "role": "admin"},
                                                      expires_delta=timedelta(seconds=-1))},
], ids=["invalid-token", "expired-token"])
def test_admin_endpoints_reject_invalid_and_expired_tokens(api, headers):
    res = api.client.get("/v1/admin/metrics", headers=headers)
    assert res.status_code == 401 and res.headers["WWW-Authenticate"] == "Bearer"


def test_admin_endpoints_reject_a_token_whose_user_no_longer_exists(api):
    api.db = ScriptedDB(None)  # user lookup finds nothing
    assert api.client.get("/v1/admin/metrics", headers=bearer(make_user("admin"))).status_code == 401


def test_admin_endpoints_are_forbidden_to_regular_users(api):
    user = make_user("user")
    api.db = ScriptedDB(user)
    res = api.client.get("/v1/admin/tickets", headers=bearer(user))
    assert res.status_code == 403 and res.json()["detail"] == "Admin access required"


def test_admin_metrics_combine_database_and_redis_figures(api, monkeypatch):
    admin = make_user("admin")
    # user lookup, then conversations, messages, average TTFT and p95 latency
    api.db = ScriptedDB(admin, 4, 9, 2412.345, 3050.0)
    monkeypatch.setattr(main, "_redis_operational_stats", AsyncMock(return_value=(0.5, 3, 2)))

    res = api.client.get("/v1/admin/metrics", headers=bearer(admin))

    assert res.status_code == 200
    assert res.json() == {
        "active_connections": 0, "queue_depth": 2, "cache_hit_ratio": 0.5, "avg_ttft_ms": 2412.3,
        "p95_latency_ms": 3050.0, "active_workers": 3, "total_conversations": 4, "total_messages": 9,
    }


def test_admin_metrics_still_answer_when_redis_is_down(api, monkeypatch):
    admin = make_user("admin")
    api.db = ScriptedDB(admin, 0, 0, None, None)  # nothing saved yet
    monkeypatch.setattr(main, "_redis_operational_stats", AsyncMock(side_effect=ConnectionError("redis down")))

    body = api.client.get("/v1/admin/metrics", headers=bearer(admin)).json()

    assert (body["cache_hit_ratio"], body["active_workers"], body["queue_depth"]) == (0.0, 0, 0)
    assert body["avg_ttft_ms"] == 0.0 and body["p95_latency_ms"] == 0.0


def test_admin_tickets_lists_the_latest_tickets(api):
    admin = make_user("admin")
    ticket = SimpleNamespace(id=uuid.uuid4(), issue_summary="I want to talk to a human", priority="urgent",
                             status="open", created_at=datetime.now(timezone.utc))
    api.db = ScriptedDB(admin, [ticket])

    res = api.client.get("/v1/admin/tickets", headers=bearer(admin))

    assert res.status_code == 200
    assert [(t["issue_summary"], t["priority"]) for t in res.json()] == [("I want to talk to a human", "urgent")]


def test_redis_stats_count_hits_heartbeats_and_unacknowledged_work(monkeypatch):
    """Backlog is pending + undelivered entries per consumer group, not the stream length."""
    async def heartbeats(match, count):
        for key in ("worker:heartbeat:stt:a", "worker:heartbeat:llm:b"):
            yield key

    async def groups(stream):
        if stream == main.settings.STREAM_LLM_INFERENCE:
            raise ConnectionError("no such stream yet")
        return [{"name": "other_group", "pending": 50, "lag": 50}, {"name": "stt_group", "pending": 2, "lag": 3}]

    redis = MagicMock()
    redis.get = AsyncMock(side_effect=lambda key: {"metric:semantic_cache_hit_total": "3",
                                                   "metric:semantic_cache_miss_total": "1"}[key])
    redis.scan_iter = heartbeats
    redis.xinfo_groups = AsyncMock(side_effect=groups)
    monkeypatch.setattr(main.queue_manager, "redis", redis)

    assert asyncio.run(main._redis_operational_stats()) == (0.75, 2, 5)
