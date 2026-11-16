from fastapi import HTTPException, Request

from .models import SessionData
from .settings import AuthSettings
from .tokens import read_session


class SessionReader:
    """FastAPI dependency: the signed-in session, from the cookie or a bearer header.

    `Depends(reader)` yields None for an anonymous request; `Depends(reader.required)`
    answers 401 instead.
    """

    def __init__(self, settings: AuthSettings):
        self.settings = settings

    def read(self, request: Request) -> SessionData | None:
        token = request.cookies.get(self.settings.access_cookie)
        if token is None:
            header = request.headers.get("Authorization", "")
            if header.lower().startswith("bearer "):
                token = header
        return read_session(self.settings, token)

    async def __call__(self, request: Request) -> SessionData | None:
        return self.read(request)

    async def required(self, request: Request) -> SessionData:
        session = self.read(request)
        if session is None:
            raise HTTPException(status_code=401, detail="Not authenticated")
        return session
