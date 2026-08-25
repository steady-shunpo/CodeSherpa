# auth/service.py

from jose import jwt, JWTError
from datetime import datetime, timedelta
from fastapi import HTTPException

import os

class AuthService:
    def __init__(self, provider):
        self.provider = provider
        self.jwt_secret = os.environ["JWT_SECRET"]

    def create_jwt(self, user: dict):
        payload = {
            "sub": user["sub"],
            "email": user["email"],
            "exp": datetime.utcnow() + timedelta(hours=24),
        }
        return jwt.encode(payload, self.jwt_secret, algorithm="HS256")

    def decode_jwt(self, token: str) -> dict:
        try:
            return jwt.decode(token, self.jwt_secret, algorithms=["HS256"])
        except JWTError:
            raise HTTPException(status_code=401, detail="Invalid or expired token")