import httpx, secrets, os
from .base import AuthProvider
from dotenv import load_dotenv


load_dotenv()

class GoogleAuthProvider(AuthProvider):
    def __init__(self):
        self.client_id = os.environ["GOOGLE_CLIENT_ID"]
        self.client_secret = os.environ["GOOGLE_CLIENT_SECRET"]
        self.redirect_uri = os.environ["REDIRECT_URI"]

        self.auth_url = "https://accounts.google.com/o/oauth2/v2/auth"
        self.token_url = "https://oauth2.googleapis.com/token"
        self.user_url = "https://www.googleapis.com/oauth2/v3/userinfo"

        self._states = set()

    async def get_auth_url(self) -> str:
        state = secrets.token_urlsafe(16)
        self._states.add(state)

        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
        }

        query = "&".join(f"{k}={v}" for k, v in params.items())
        return f"{self.auth_url}?{query}"

    async def handle_callback(self, code: str, state: str) -> dict:
        if state not in self._states:
            raise Exception("Invalid state")

        self._states.remove(state)

        async with httpx.AsyncClient() as client:
            token_resp = await client.post(self.token_url, data={
                "code": code,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "redirect_uri": self.redirect_uri,
                "grant_type": "authorization_code",
            })

        access_token = token_resp.json()["access_token"]

        async with httpx.AsyncClient() as client:
            user_resp = await client.get(
                self.user_url,
                headers={"Authorization": f"Bearer {access_token}"}
            )

        return user_resp.json()
