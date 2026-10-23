"""
JWT authentication, password hashing and role-based access dependencies.

Hashing uses bcrypt (via passlib), which is deliberately CPU-expensive (~100-300ms).
Callers on the event loop must use the ``*_async`` helpers so a login burst cannot
stall every other WebSocket and HTTP request served by the same process.
"""
import asyncio
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import User

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
security = HTTPBearer()


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


async def verify_password_async(plain_password: str, hashed_password: str) -> bool:
    """Runs bcrypt verification in a worker thread to keep the event loop responsive."""
    return await asyncio.to_thread(verify_password, plain_password, hashed_password)


async def get_password_hash_async(password: str) -> str:
    """Runs bcrypt hashing in a worker thread to keep the event loop responsive."""
    return await asyncio.to_thread(get_password_hash, password)


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    """Signs ``data`` (expects ``sub`` and ``role``) as an HS256 JWT with an ``exp`` claim."""
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_access_token(token: str) -> Optional[dict]:
    """Verifies signature and expiry; returns the claims, or ``None`` for any invalid token."""
    try:
        return jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    except JWTError:
        return None


def user_id_from_token(token: Optional[str]) -> Optional[uuid.UUID]:
    """Extracts the user UUID from a valid token; ``None`` if absent, invalid or malformed."""
    if not token:
        return None
    payload = decode_access_token(token)
    if not payload or not payload.get("sub"):
        return None
    try:
        return uuid.UUID(str(payload["sub"]))
    except ValueError:
        return None


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: AsyncSession = Depends(get_db)
) -> User:
    """FastAPI dependency: resolves the bearer token to an existing user or raises 401."""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    user_id = user_id_from_token(credentials.credentials)
    if user_id is None:
        raise credentials_exception

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise credentials_exception
    return user


async def get_current_admin(
    current_user: User = Depends(get_current_user)
) -> User:
    """FastAPI dependency: like ``get_current_user`` but additionally requires role ``admin`` (403)."""
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required"
        )
    return current_user


async def create_guest_user(db: AsyncSession) -> tuple[User, str]:
    """
    Creates an ephemeral guest account and returns ``(user, access_token)``.

    The password is random and never returned: guests authenticate only via the token,
    so nobody can log into another guest's account by guessing a shared password.
    """
    guest_id = uuid.uuid4()
    guest_user = User(
        id=guest_id,
        email=f"guest_{guest_id.hex[:12]}@voxa.demo",
        hashed_password=await get_password_hash_async(secrets.token_urlsafe(24)),
        role="user"
    )
    db.add(guest_user)
    await db.commit()
    await db.refresh(guest_user)

    token = create_access_token(data={"sub": str(guest_user.id), "role": guest_user.role})
    return guest_user, token
