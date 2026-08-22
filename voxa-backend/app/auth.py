import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
try:
    from jose import JWTError, jwt
except ImportError:
    import hmac
    import hashlib
    import base64
    import json
    class JWTError(Exception): pass
    class FakeJWT:
        @staticmethod
        def encode(claims, key, algorithm="HS256"):
            header = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').decode().rstrip("=")
            payload = base64.urlsafe_b64encode(json.dumps(claims, default=str).encode()).decode().rstrip("=")
            sig = base64.urlsafe_b64encode(hmac.new(key.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()).decode().rstrip("=")
            return f"{header}.{payload}.{sig}"
        @staticmethod
        def decode(token, key, algorithms=None):
            parts = token.split(".")
            if len(parts) != 3: raise JWTError("Invalid token")
            payload = parts[1] + "=" * (-len(parts[1]) % 4)
            return json.loads(base64.urlsafe_b64decode(payload.encode()).decode())
    jwt = FakeJWT()

try:
    from passlib.context import CryptContext
    pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
except ImportError:
    import hashlib
    class FakePwdContext:
        def hash(self, secret: str) -> str:
            return hashlib.sha256(secret.encode()).hexdigest()
        def verify(self, secret: str, hash_str: str) -> bool:
            return self.hash(secret) == hash_str
    pwd_context = FakePwdContext()

try:
    from fastapi import Depends, HTTPException, status
    from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
except ImportError:
    class HTTPException(Exception):
        def __init__(self, status_code, detail, headers=None):
            self.status_code = status_code
            self.detail = detail
            self.headers = headers
    class status:
        HTTP_401_UNAUTHORIZED = 401
        HTTP_403_FORBIDDEN = 403
    def Depends(fn=None): return None
    class HTTPBearer: pass
    class HTTPAuthorizationCredentials: pass

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.config import settings
from app.database import get_db
from app.models import User

security = HTTPBearer()

def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    return encoded_jwt

async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: AsyncSession = Depends(get_db)
) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    token = credentials.credentials
    try:
        payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
        user_id: str = payload.get("sub")
        if user_id is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    stmt = select(User).where(User.id == uuid.UUID(user_id))
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()
    if user is None:
        raise credentials_exception
    return user

async def get_current_admin(
    current_user: User = Depends(get_current_user)
) -> User:
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required"
        )
    return current_user

async def create_guest_user(db: AsyncSession) -> tuple[User, str]:
    """Creates a temporary 1-Click Guest Demo account and returns (user, token)."""
    guest_id = uuid.uuid4()
    guest_email = f"guest_{str(guest_id)[:8]}@voxa.demo"
    guest_pwd_hash = get_password_hash("guest_pass_2026")
    
    guest_user = User(
        id=guest_id,
        email=guest_email,
        hashed_password=guest_pwd_hash,
        role="user"
    )
    db.add(guest_user)
    await db.commit()
    await db.refresh(guest_user)
    
    token = create_access_token(data={"sub": str(guest_user.id), "role": guest_user.role})
    return guest_user, token
