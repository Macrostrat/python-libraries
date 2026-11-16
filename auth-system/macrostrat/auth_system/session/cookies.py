from urllib.parse import urlparse

from pydantic import BaseModel
from starlette.responses import Response

from .settings import AuthSettings


class CookieParams(BaseModel):
    domain: str | None
    samesite: str
    secure: bool


def cookie_params(settings: AuthSettings) -> CookieParams:
    """Host-only when deployed; parent-domain and cross-site under `*.local`."""
    hostname = urlparse(settings.redirect_uri).hostname or ""
    domain = None
    samesite = "lax"
    if hostname.endswith(".local") and hostname.count(".") > 1:
        domain = ".".join(hostname.split(".")[-2:])
        # SameSite=None is only honoured on Secure cookies.
        if settings.secure:
            samesite = "none"
    return CookieParams(domain=domain, samesite=samesite, secure=settings.secure)


def _set(settings: AuthSettings, response: Response, key: str, value: str, age: int):
    params = cookie_params(settings)
    response.set_cookie(
        key,
        value,
        max_age=age,
        domain=params.domain,
        httponly=True,
        samesite=params.samesite,
        secure=params.secure,
    )


def set_access_cookie(settings: AuthSettings, response: Response, token: str):
    age = settings.access_token_minutes * 60
    _set(settings, response, settings.access_cookie, f"Bearer {token}", age)


def set_session_cookies(
    settings: AuthSettings, response: Response, access_token: str, refresh_token: str
):
    set_access_cookie(settings, response, access_token)
    age = settings.refresh_token_days * 24 * 60 * 60
    _set(settings, response, settings.refresh_cookie, refresh_token, age)


def set_state_cookie(settings: AuthSettings, response: Response, nonce: str):
    # Always Lax: the callback is a top-level navigation back from the provider.
    response.set_cookie(
        settings.state_cookie,
        nonce,
        max_age=settings.state_minutes * 60,
        httponly=True,
        samesite="lax",
        secure=settings.secure,
    )


def clear_session_cookies(settings: AuthSettings, response: Response):
    params = cookie_params(settings)
    for key in (settings.access_cookie, settings.refresh_cookie):
        response.delete_cookie(key, domain=params.domain)
        if params.domain is not None:
            response.delete_cookie(key)
