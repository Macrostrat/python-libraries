"""ORCID (OAuth) sign-in with JWT session cookies, for any FastAPI service.

The application supplies a `UserStore`; this module owns the OAuth exchange,
the cookies and the token format. The access token doubles as a PostgREST JWT.
"""

from .cookies import CookieParams, clear_session_cookies, set_session_cookies
from .dependencies import SessionReader
from .models import OAuthIdentity, SessionData, SessionUser, UserStore
from .router import create_auth_router
from .settings import AuthSettings
from .tokens import create_access_token, create_refresh_token, decode_token

__all__ = [
    "AuthSettings",
    "CookieParams",
    "OAuthIdentity",
    "SessionData",
    "SessionReader",
    "SessionUser",
    "UserStore",
    "clear_session_cookies",
    "create_access_token",
    "create_auth_router",
    "create_refresh_token",
    "decode_token",
    "set_session_cookies",
]
