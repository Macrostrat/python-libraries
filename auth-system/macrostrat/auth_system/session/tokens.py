from datetime import datetime, timedelta, timezone

import jwt

from .models import SessionData, SessionUser
from .settings import AuthSettings

RESERVED_CLAIMS = {"sub", "name", "role", "exp", "type", "aud"}


def _encode(settings: AuthSettings, claims: dict, lifetime: timedelta) -> str:
    payload = {**claims, "exp": datetime.now(timezone.utc) + lifetime}
    if settings.audience:
        payload["aud"] = settings.audience
    return jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)


def decode_token(settings: AuthSettings, token: str) -> dict | None:
    """The token's claims, or None if it is expired, forged or malformed."""
    try:
        # With no audience configured, PyJWT refuses any token that names one.
        return jwt.decode(
            token,
            settings.secret_key,
            algorithms=[settings.algorithm],
            audience=settings.audience,
        )
    except jwt.PyJWTError:
        return None


def create_access_token(settings: AuthSettings, user: SessionUser) -> str:
    extra = {k: v for k, v in user.claims.items() if k not in RESERVED_CLAIMS}
    claims = {**extra, "sub": user.sub, "name": user.name, "role": user.role}
    return _encode(settings, claims, timedelta(minutes=settings.access_token_minutes))


def create_refresh_token(settings: AuthSettings, user: SessionUser) -> str:
    claims = {"sub": user.sub, "type": "refresh"}
    return _encode(settings, claims, timedelta(days=settings.refresh_token_days))


def create_state_token(settings: AuthSettings, nonce: str, return_url: str) -> str:
    claims = {"type": "state", "nonce": nonce, "return_url": return_url}
    return _encode(settings, claims, timedelta(minutes=settings.state_minutes))


def read_session(settings: AuthSettings, token: str | None) -> SessionData | None:
    if not token:
        return None
    # The cookie holds "Bearer <jwt>" so a gateway can copy it into a header.
    token = token.removeprefix("Bearer ").strip()
    payload = decode_token(settings, token)
    if payload is None or payload.get("type") is not None or not payload.get("sub"):
        return None
    extra = {k: v for k, v in payload.items() if k not in RESERVED_CLAIMS}
    return SessionData(
        sub=payload["sub"],
        name=payload.get("name") or "",
        role=payload.get("role"),
        claims=extra,
    )
