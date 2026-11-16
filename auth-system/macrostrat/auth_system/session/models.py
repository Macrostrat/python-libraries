from typing import Any, Awaitable, Protocol

from pydantic import BaseModel


class OAuthIdentity(BaseModel):
    """What the identity provider says about the person who signed in."""

    sub: str
    name: str = ""
    email: str | None = None


class SessionUser(BaseModel):
    """An application's account, reduced to what goes into a session token."""

    sub: str
    name: str = ""
    # The Postgres role PostgREST assumes for this session.
    role: str = "web_user"
    claims: dict[str, Any] = {}


class SessionData(BaseModel):
    """The claims read back out of an access token."""

    sub: str
    name: str = ""
    role: str | None = None
    claims: dict[str, Any] = {}


class UserStore(Protocol):
    """The application's account storage. Methods may be sync or async."""

    def resolve_user(
        self, identity: OAuthIdentity
    ) -> SessionUser | None | Awaitable[SessionUser | None]:
        """Find or create the account for a sign-in; None refuses it."""
        ...

    def get_user(self, sub: str) -> SessionUser | None | Awaitable[SessionUser | None]:
        """Look an account up again when a session is refreshed."""
        ...
