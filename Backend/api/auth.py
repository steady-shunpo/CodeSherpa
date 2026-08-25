import os

from fastapi import HTTPException, Depends, APIRouter
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import RedirectResponse

from auth.google import GoogleAuthProvider
from auth.service import AuthService

router = APIRouter(prefix="/auth")
bearer_scheme = HTTPBearer()

FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:5173")

# IMPORTANT: this must be a singleton, not created fresh per-request.
# GoogleAuthProvider holds `_states` in memory to prevent CSRF -- the state
# created in /google must still be there when /google/callback checks it.
# Depends(get_auth_service) creates a NEW instance every request otherwise,
# so the state set is always empty by the time callback runs.
_provider = GoogleAuthProvider()
_auth_service = AuthService(_provider)


def get_auth_service() -> AuthService:
    return _auth_service


def get_current_user(
    creds: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    auth: AuthService = Depends(get_auth_service),
):
    return auth.decode_jwt(creds.credentials)


@router.get("/google")
async def login(auth: AuthService = Depends(get_auth_service)):
    url = await auth.provider.get_auth_url()
    return RedirectResponse(url)


@router.get("/google/callback")
async def callback(code: str, state: str, auth: AuthService = Depends(get_auth_service)):
    user = await auth.provider.handle_callback(code, state)
    token = auth.create_jwt(user)

    # This endpoint is hit by the BROWSER (via Google's redirect), not by
    # your frontend's JS. Returning JSON here just renders as raw text on
    # screen -- nothing in your React app ever sees it. We have to redirect
    # the browser back to the frontend, carrying the token along.
    return RedirectResponse(f"{FRONTEND_URL}/auth/success?token={token}")