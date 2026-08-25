# auth/providers/base.py
from abc import ABC, abstractmethod

class AuthProvider(ABC):
    @abstractmethod
    async def get_auth_url(self) -> str:
        pass

    @abstractmethod
    async def handle_callback(self, code: str) -> dict:
        pass