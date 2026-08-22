import pytest
from app.auth import get_password_hash, verify_password, create_access_token, jwt
from app.config import settings

def test_password_hashing():
    password = "super_secure_password_2026"
    hashed = get_password_hash(password)
    
    assert hashed != password
    assert verify_password(password, hashed) is True
    assert verify_password("wrong_password", hashed) is False

def test_jwt_token_generation_and_decoding():
    payload_data = {"sub": "user-12345", "role": "admin"}
    token = create_access_token(payload_data)
    
    assert isinstance(token, str)
    decoded = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    
    assert decoded["sub"] == "user-12345"
    assert decoded["role"] == "admin"
    assert "exp" in decoded
