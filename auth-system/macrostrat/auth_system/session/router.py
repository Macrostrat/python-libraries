import inspect
import secrets
from urllib.parse import urlencode

import httpx2
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from starlette.concurrency import run_in_threadpool

from .cookies import (
    clear_session_cookies,
    set_access_cookie,
    set_session_cookies,
    set_state_cookie,
)
from .dependencies import SessionReader
from .models import OAuthIdentity, SessionData, SessionUser, UserStore
from .settings import AuthSettings
from .tokens import (
    create_access_token,
    create_refresh_token,
    create_state_token,
    decode_token,
)


async def _call(fn, *args):
    if inspect.iscoroutinefunction(fn):
        return await fn(*args)
    return await run_in_threadpool(fn, *args)


async def fetch_identity(
    settings: AuthSettings, code: str, transport: httpx2.AsyncBaseTransport | None
) -> OAuthIdentity:
    """Exchange an authorization code for the signed-in person's identity."""
    data = {
        "grant_type": "authorization_code",
        "client_id": settings.oauth_client_id,
        "client_secret": settings.oauth_client_secret,
        "code": code,
        "redirect_uri": settings.redirect_uri,
    }
    async with httpx2.AsyncClient(transport=transport, timeout=20) as client:
        token_res = await client.post(
            settings.oauth_token_url, data=data, headers={"Accept": "application/json"}
        )
        if token_res.status_code != 200:
            raise HTTPException(status_code=400, detail="Sign-in code was not accepted")
        access_token = token_res.json().get("access_token")
        user_res = await client.get(
            settings.oauth_userinfo_url,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if user_res.status_code != 200:
            raise HTTPException(status_code=400, detail="Could not read user identity")
        info = user_res.json()

    names = [info.get("given_name"), info.get("family_name")]
    name = " ".join(n for n in names if n) or info.get("name") or ""
    return OAuthIdentity(sub=info["sub"], name=name, email=info.get("email"))


def create_auth_router(
    settings: AuthSettings,
    store: UserStore,
    *,
    prefix: str = "/auth",
    transport: httpx2.AsyncBaseTransport | None = None,
) -> APIRouter:
    """Login, callback, refresh, logout and `me` routes over a `UserStore`."""
    router = APIRouter(prefix=prefix, tags=["auth"])
    reader = SessionReader(settings)

    @router.get("/login")
    async def login(return_url: str | None = None):
        nonce = secrets.token_urlsafe(24)
        state = create_state_token(
            settings, nonce, settings.safe_return_url(return_url)
        )
        params = {
            "scope": settings.oauth_scope,
            "client_id": settings.oauth_client_id,
            "response_type": "code",
            "redirect_uri": settings.redirect_uri,
            "state": state,
        }
        response = RedirectResponse(
            f"{settings.oauth_authorization_url}?{urlencode(params)}"
        )
        set_state_cookie(settings, response, nonce)
        return response

    @router.get("/callback")
    async def callback(request: Request, code: str, state: str):
        claims = decode_token(settings, state)
        nonce = request.cookies.get(settings.state_cookie)
        if (
            claims is None
            or claims.get("type") != "state"
            or not nonce
            or not secrets.compare_digest(claims.get("nonce", ""), nonce)
        ):
            raise HTTPException(status_code=400, detail="Sign-in state did not match")

        identity = await fetch_identity(settings, code, transport)
        user: SessionUser | None = await _call(store.resolve_user, identity)
        if user is None:
            raise HTTPException(status_code=403, detail="This account may not sign in")

        response = RedirectResponse(settings.safe_return_url(claims.get("return_url")))
        set_session_cookies(
            settings,
            response,
            create_access_token(settings, user),
            create_refresh_token(settings, user),
        )
        response.delete_cookie(settings.state_cookie)
        return response

    @router.post("/refresh")
    async def refresh(request: Request, response: Response):
        claims = decode_token(
            settings, request.cookies.get(settings.refresh_cookie) or ""
        )
        if claims is None or claims.get("type") != "refresh" or not claims.get("sub"):
            clear_session_cookies(settings, response)
            response.status_code = 401
            return {"detail": "Refresh token invalid"}
        # Re-read the account so a changed role takes effect within a day.
        user: SessionUser | None = await _call(store.get_user, claims["sub"])
        if user is None:
            clear_session_cookies(settings, response)
            response.status_code = 401
            return {"detail": "Account not found"}
        set_access_cookie(settings, response, create_access_token(settings, user))
        return {"status": "refreshed"}

    @router.post("/logout")
    async def logout(response: Response):
        clear_session_cookies(settings, response)
        return {"status": "logged out"}

    @router.get("/me")
    async def me(session: SessionData | None = Depends(reader)):
        if session is None:
            raise HTTPException(status_code=401, detail="Not authenticated")
        return session

    return router
