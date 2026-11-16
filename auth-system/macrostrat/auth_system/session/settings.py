from os import environ
from typing import Mapping
from urllib.parse import urlparse

from pydantic import BaseModel

ORCID_BASE_URL = "https://orcid.org/oauth"


class AuthSettings(BaseModel):
    """Everything the session system needs; no global or environment reads."""

    secret_key: str
    algorithm: str = "HS256"
    # Names the service tokens are for, so another service's tokens are refused.
    audience: str | None = None

    oauth_client_id: str
    oauth_client_secret: str
    oauth_authorization_url: str = f"{ORCID_BASE_URL}/authorize"
    oauth_token_url: str = f"{ORCID_BASE_URL}/token"
    oauth_userinfo_url: str = f"{ORCID_BASE_URL}/userinfo"
    oauth_scope: str = "openid"
    redirect_uri: str

    access_cookie: str = "access_token"
    refresh_cookie: str = "refresh_token"
    state_cookie: str = "oauth_state"
    access_token_minutes: int = 1440
    refresh_token_days: int = 7
    state_minutes: int = 10

    # Origins, besides the service's own, that a sign-in may return to.
    allowed_return_origins: list[str] = []
    default_return_url: str = "/"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **overrides):
        """Read the variable names Macrostrat's API v3 already uses."""
        env = environ if env is None else env
        names = {
            "secret_key": "SECRET_KEY",
            "algorithm": "JWT_ENCRYPTION_ALGORITHM",
            "audience": "JWT_AUDIENCE",
            "oauth_client_id": "OAUTH_CLIENT_ID",
            "oauth_client_secret": "OAUTH_CLIENT_SECRET",
            "oauth_authorization_url": "OAUTH_AUTHORIZATION_URL",
            "oauth_token_url": "OAUTH_TOKEN_URL",
            "oauth_userinfo_url": "OAUTH_USERINFO_URL",
            "oauth_scope": "OAUTH_SCOPE",
            "redirect_uri": "REDIRECT_URI",
        }
        values = {k: env[v] for k, v in names.items() if env.get(v)}
        origins = env.get("AUTH_RETURN_ORIGINS")
        if origins:
            values["allowed_return_origins"] = [
                o.strip().rstrip("/") for o in origins.split(",") if o.strip()
            ]
        values.update(overrides)
        return cls(**values)

    @property
    def origin(self) -> str:
        parsed = urlparse(self.redirect_uri)
        return f"{parsed.scheme}://{parsed.netloc}"

    @property
    def secure(self) -> bool:
        return urlparse(self.redirect_uri).scheme == "https"

    def safe_return_url(self, url: str | None) -> str:
        """A same-site path or an allowed origin; anything else is discarded."""
        if not url:
            return self.default_return_url
        if url.startswith("/") and not url.startswith(("//", "/\\")):
            return url
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin == self.origin or origin in self.allowed_return_origins:
            return url
        return self.default_return_url
