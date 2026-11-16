from urllib.parse import parse_qs, urlparse

import httpx2
from fastapi import Depends, FastAPI
from pytest import fixture
from starlette.testclient import TestClient

from . import (
    AuthSettings,
    OAuthIdentity,
    SessionReader,
    SessionUser,
    create_access_token,
    create_auth_router,
    decode_token,
)


class MemoryStore:
    def __init__(self):
        self.users: dict[str, SessionUser] = {}
        self.refused: set[str] = set()

    def resolve_user(self, identity: OAuthIdentity):
        if identity.sub in self.refused:
            return None
        user = SessionUser(sub=identity.sub, name=identity.name, claims={"uid": 7})
        self.users.setdefault(identity.sub, user)
        return self.users[identity.sub]

    async def get_user(self, sub: str):
        return self.users.get(sub)


def provider(request: httpx2.Request) -> httpx2.Response:
    if request.url.path == "/oauth/token":
        if b"code=good" not in request.content:
            return httpx2.Response(400, json={"error": "invalid_grant"})
        return httpx2.Response(200, json={"access_token": "provider-token"})
    assert request.headers["Authorization"] == "Bearer provider-token"
    return httpx2.Response(
        200,
        json={
            "sub": "0000-0002-1825-0097",
            "given_name": "Josiah",
            "family_name": "Carberry",
        },
    )


@fixture
def settings():
    return AuthSettings(
        secret_key="test-secret-key-of-reasonable-length",
        oauth_client_id="APP-TEST",
        oauth_client_secret="shh",
        redirect_uri="https://maps.example.org/auth/callback",
        allowed_return_origins=["https://other.example.org"],
    )


@fixture
def store():
    return MemoryStore()


@fixture
def client(settings, store):
    app = FastAPI()
    app.include_router(
        create_auth_router(settings, store, transport=httpx2.MockTransport(provider))
    )
    reader = SessionReader(settings)

    @app.get("/private")
    async def private(session=Depends(reader.required)):
        return {"sub": session.sub}

    return TestClient(app, base_url="https://maps.example.org", follow_redirects=False)


def sign_in(client, return_url=None, code="good"):
    params = {"return_url": return_url} if return_url else {}
    res = client.get("/auth/login", params=params)
    assert res.status_code == 307
    state = parse_qs(urlparse(res.headers["location"]).query)["state"][0]
    return client.get("/auth/callback", params={"code": code, "state": state})


def test_login_redirects_to_provider(client, settings):
    res = client.get("/auth/login")
    location = urlparse(res.headers["location"])
    query = parse_qs(location.query)
    assert location.netloc == "orcid.org"
    assert query["redirect_uri"] == [settings.redirect_uri]
    assert settings.state_cookie in res.cookies


def test_sign_in_sets_session(client, settings):
    res = sign_in(client, "/projects/naukluft")
    assert res.status_code == 307
    assert res.headers["location"] == "/projects/naukluft"
    cookie = client.cookies[settings.access_cookie].strip('"')
    assert cookie.startswith("Bearer ")
    claims = decode_token(settings, cookie.removeprefix("Bearer "))
    assert claims["role"] == "web_user"
    assert claims["uid"] == 7
    me = client.get("/auth/me").json()
    assert me["name"] == "Josiah Carberry"
    assert client.get("/private").json() == {"sub": "0000-0002-1825-0097"}


def test_anonymous_is_refused(client):
    assert client.get("/auth/me").status_code == 401
    assert client.get("/private").status_code == 401


def test_state_must_match_cookie(client):
    res = client.get("/auth/login")
    state = parse_qs(urlparse(res.headers["location"]).query)["state"][0]
    client.cookies.clear()
    res = client.get("/auth/callback", params={"code": "good", "state": state})
    assert res.status_code == 400


def test_forged_state_is_refused(client):
    client.get("/auth/login")
    res = client.get("/auth/callback", params={"code": "good", "state": "nonsense"})
    assert res.status_code == 400


def test_bad_code_is_refused(client):
    assert sign_in(client, code="bad").status_code == 400


def test_store_may_refuse(client, store):
    store.refused.add("0000-0002-1825-0097")
    assert sign_in(client).status_code == 403


def test_return_url_is_validated(client, settings):
    assert sign_in(client, "https://evil.example.com/x").headers["location"] == "/"
    assert sign_in(client, "//evil.example.com").headers["location"] == "/"
    ok = "https://other.example.org/page"
    assert sign_in(client, ok).headers["location"] == ok


def test_refresh_and_logout(client, settings, store):
    sign_in(client)
    store.users["0000-0002-1825-0097"].role = "web_admin"
    assert client.post("/auth/refresh").status_code == 200
    assert client.get("/auth/me").json()["role"] == "web_admin"
    client.post("/auth/logout")
    assert client.get("/auth/me").status_code == 401


def test_refresh_token_is_not_a_session(client, settings):
    sign_in(client)
    refresh = client.cookies[settings.refresh_cookie]
    client.cookies.clear()
    res = client.get("/private", headers={"Authorization": f"Bearer {refresh}"})
    assert res.status_code == 401


def test_audience_is_issued_and_required(settings):
    user = SessionUser(sub="0000-0002-1825-0097", name="Josiah Carberry")
    mapboard = settings.model_copy(update={"audience": "mapboard"})
    other = settings.model_copy(update={"audience": "macrostrat"})
    token = create_access_token(mapboard, user)
    assert decode_token(mapboard, token)["aud"] == "mapboard"
    assert decode_token(other, token) is None
    assert decode_token(settings, token) is None
    assert decode_token(mapboard, create_access_token(settings, user)) is None
