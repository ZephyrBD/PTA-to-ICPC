import base64

import pytest
from fastapi.testclient import TestClient

from pta2icpc.app import create_app
from pta2icpc.auth import API_USERNAME, check_basic_auth, generate_api_password
from pta2icpc.store import Store

ALLOWED = set("abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789")
CONFUSING = set("0O1lI")


def _basic(user: str, pw: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()


def _client(api_password=None) -> TestClient:
    return TestClient(create_app(Store(), None, "c1",
                                 api_password=api_password))


# --- generate_api_password -------------------------------------------------

def test_generate_api_password_length_and_charset():
    for _ in range(200):
        pw = generate_api_password()
        assert len(pw) == 8
        assert set(pw) <= ALLOWED


def test_generate_api_password_never_uses_lookalikes():
    for _ in range(200):
        assert not (set(generate_api_password()) & CONFUSING)


def test_username_is_admin():
    assert API_USERNAME == "admin"


# --- check_basic_auth ------------------------------------------------------

def test_check_basic_auth_accepts_correct_credentials():
    assert check_basic_auth(_basic("admin", "s3cret"), "s3cret") is True


@pytest.mark.parametrize("header", [
    None,
    "",
    "Bearer x",
    "Basic",
    "Basic ",
    "Basic !!!not-base64",
    "Basic " + base64.b64encode(b"nocolon").decode(),
    "Basic " + base64.b64encode(b"admin:wrong").decode(),
    "Basic " + base64.b64encode(b"root:s3cret").decode(),
    "Basic " + base64.b64encode("admin:密码".encode()).decode(),
    "Basic " + base64.b64encode(b"\xff\xfe").decode(),
])
def test_check_basic_auth_rejects_bad_input(header):
    assert check_basic_auth(header, "s3cret") is False


# --- require_api_auth / create_app integration -----------------------------

def test_api_requires_auth_when_password_set():
    r = _client("s3cret").get("/api/v4/")
    assert r.status_code == 401
    assert r.json() == {"code": 401, "message": "authentication required"}
    assert r.headers["WWW-Authenticate"] == 'Basic realm="pta2icpc"'


def test_api_accepts_correct_credentials():
    r = _client("s3cret").get("/api/v4/", auth=("admin", "s3cret"))
    assert r.status_code == 200


def test_api_rejects_wrong_credentials():
    c = _client("s3cret")
    assert c.get("/api/v4/", auth=("admin", "nope")).status_code == 401
    assert c.get("/api/v4/", auth=("root", "s3cret")).status_code == 401


def test_healthz_stays_open_without_auth():
    assert _client("s3cret").get("/healthz").status_code == 200


def test_legacy_redirect_stays_open_without_auth():
    r = _client("s3cret").get("/contests", follow_redirects=False)
    assert r.status_code == 301


def test_no_password_leaves_api_open():
    assert _client(None).get("/api/v4/").status_code == 200
    assert _client("").get("/api/v4/").status_code == 200


def test_bare_api_v4_requires_auth_when_password_set():
    assert _client("s3cret").get("/api/v4",
                                 follow_redirects=False).status_code == 401


def test_bare_api_v4_redirects_with_credentials():
    r = _client("s3cret").get("/api/v4", auth=("admin", "s3cret"),
                              follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"].endswith("/api/v4/")


def test_bare_api_v4_redirects_when_open():
    r = _client(None).get("/api/v4", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"].endswith("/api/v4/")


def test_event_feed_requires_auth():
    r = _client("s3cret").get("/api/v4/contests/c1/event-feed",
                              follow_redirects=False)
    assert r.status_code == 401
    assert r.headers["WWW-Authenticate"] == 'Basic realm="pta2icpc"'
