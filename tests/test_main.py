"""Tests for the TM API routes and BMDC parsing helpers."""

import base64
import time

import pytest
from fastapi.testclient import TestClient

import main

client = TestClient(main.app)

CAPTCHA_BYTES = b"fake-captcha-png"
IMAGE_BYTES = b"fake-doctor-jpg"

INIT_HTML = """
<html><body>
  <form>
    <input name="bmdckyc_csrf_token" value="csrf-123">
    <input name="action_key" value="action-456">
    <img alt=" " src="/captcha.png">
  </form>
</body></html>
"""

RESULT_HTML = """
<html><body>
  <div class="form-items">
    <div class="profile mr-1 mb-2">
      <img class="rounded img-responsive mb-2" src="data:image/jpg;base64,AAAA">
    </div>
    <h3 class="mb-4 font-weight-bold text-center">Dr. Jane Doe</h3>
    <h3 class="badge badge-pill badge-success mt-1 mb-3 font-weight-bold d-block text-center text-white">BMDC-123</h3>
    <div class="form-group row mb-0">
      <span class="font-weight-bold">Valid</span>
      <h6>1990-01-01</h6>
      <h6>A+</h6>
    </div>
    <div class="form-group row text-center">
      <div class="col-md-4"><span>2020</span></div>
      <div class="col-md-4"><span>2030</span></div>
      <div class="col-md-4"><span>CARD-9</span></div>
    </div>
    <div class="form-group row mb-0"><h6>Father Name</h6></div>
    <div class="form-group row mb-0"><h6>Mother Name</h6></div>
  </div>
</body></html>
"""

RESULT_HTML_RELATIVE_IMAGE = RESULT_HTML.replace(
    "data:image/jpg;base64,AAAA", "/doctor.jpg"
)


class FakeResponse:
    """Minimal requests.Response stand-in."""

    def __init__(self, content: bytes = b"", status_code: int = 200):
        self.content = content
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise main.requests.HTTPError(f"{self.status_code} error")


class FakeSession:
    """Minimal requests.Session stand-in with queued responses."""

    def __init__(self, get_responses=None, post_responses=None):
        self.headers = {}
        self.get_responses = list(get_responses or [])
        self.post_responses = list(post_responses or [])
        self.get_calls = []
        self.post_calls = []
        self.closed = False

    def get(self, url, timeout=None):
        self.get_calls.append((url, timeout))
        return self.get_responses.pop(0)

    def post(self, url, data=None, timeout=None):
        self.post_calls.append((url, data, timeout))
        return self.post_responses.pop(0)

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def clear_sessions():
    """Keep the module-level session store isolated between tests."""
    main.sessions.clear()
    yield
    main.sessions.clear()


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_root_links_to_docs_and_health():
    response = client.get("/")
    assert response.status_code == 200
    assert response.json()["docs"] == "/docs"
    assert response.json()["health"] == "/health"


def test_init_session_returns_captcha(monkeypatch):
    fake = FakeSession(
        get_responses=[
            FakeResponse(INIT_HTML.encode()),
            FakeResponse(CAPTCHA_BYTES),
        ]
    )
    monkeypatch.setattr(main, "_new_session", lambda: fake)

    response = client.get("/init-session")

    assert response.status_code == 200
    body = response.json()
    assert body["csrf_token"] == "csrf-123"
    assert body["action_key"] == "action-456"
    assert base64.b64decode(body["captcha_image"]) == CAPTCHA_BYTES
    assert main.sessions[body["session_id"]]["session"] is fake
    assert fake.get_calls[1][0] == "https://verify.bmdc.org.bd/captcha.png"


def test_init_session_missing_token_returns_502(monkeypatch):
    fake = FakeSession(
        get_responses=[FakeResponse(b"<html><body>no token</body></html>")]
    )
    monkeypatch.setattr(main, "_new_session", lambda: fake)

    response = client.get("/init-session")

    assert response.status_code == 502
    assert "bmdckyc_csrf_token" in response.json()["detail"]
    assert fake.closed is True


def test_init_session_network_error_returns_502(monkeypatch):
    class BrokenSession(FakeSession):
        def get(self, url, timeout=None):
            raise main.requests.ConnectionError("upstream down")

    monkeypatch.setattr(main, "_new_session", BrokenSession)

    response = client.get("/init-session")

    assert response.status_code == 502
    assert "Could not reach" in response.json()["detail"]


def test_verify_doctor_returns_profile():
    fake = FakeSession(post_responses=[FakeResponse(RESULT_HTML.encode())])
    main.sessions["sid"] = {
        "session": fake,
        "csrf_token": "csrf-123",
        "action_key": "action-456",
        "created_at": time.time(),
    }

    response = client.post(
        "/verify-doctor",
        json={
            "session_id": "sid",
            "registration_number": "12345",
            "captcha_text": "abcd",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Dr. Jane Doe"
    assert body["registration_number"] == "BMDC-123"
    assert body["status"] == "Valid"
    assert body["reg_year"] == "2020"
    assert body["valid_till"] == "2030"
    assert body["card_number"] == "CARD-9"
    assert body["dob"] == "1990-01-01"
    assert body["blood_group"] == "A+"
    assert body["father_name"] == "Father Name"
    assert body["mother_name"] == "Mother Name"
    assert body["doctor_image_base64"] == "AAAA"

    assert fake.post_calls[0][1]["reg_ful_no"] == "12345"
    assert "sid" not in main.sessions
    assert fake.closed is True


def test_verify_doctor_invalid_captcha_returns_400():
    fake = FakeSession(
        post_responses=[FakeResponse(b"<html><body>invalid</body></html>")]
    )
    main.sessions["sid"] = {
        "session": fake,
        "csrf_token": "csrf-123",
        "action_key": "action-456",
        "created_at": time.time(),
    }

    response = client.post(
        "/verify-doctor",
        json={
            "session_id": "sid",
            "registration_number": "12345",
            "captcha_text": "wrong",
        },
    )

    assert response.status_code == 400
    assert "Invalid CAPTCHA" in response.json()["detail"]
    assert "sid" not in main.sessions


def test_verify_doctor_unknown_session_returns_404():
    response = client.post(
        "/verify-doctor",
        json={
            "session_id": "missing",
            "registration_number": "12345",
            "captcha_text": "abcd",
        },
    )

    assert response.status_code == 404
    assert "Session expired" in response.json()["detail"]


def test_verify_doctor_network_error_returns_502():
    class BrokenPostSession(FakeSession):
        def post(self, url, data=None, timeout=None):
            raise main.requests.ConnectionError("upstream down")

    fake = BrokenPostSession()
    main.sessions["sid"] = {
        "session": fake,
        "csrf_token": "csrf-123",
        "action_key": "action-456",
        "created_at": time.time(),
    }

    response = client.post(
        "/verify-doctor",
        json={
            "session_id": "sid",
            "registration_number": "12345",
            "captcha_text": "abcd",
        },
    )

    assert response.status_code == 502
    assert "Could not submit" in response.json()["detail"]


def test_verify_doctor_downloads_relative_image():
    fake = FakeSession(
        get_responses=[FakeResponse(IMAGE_BYTES)],
        post_responses=[FakeResponse(RESULT_HTML_RELATIVE_IMAGE.encode())],
    )
    main.sessions["sid"] = {
        "session": fake,
        "csrf_token": "csrf-123",
        "action_key": "action-456",
        "created_at": time.time(),
    }

    response = client.post(
        "/verify-doctor",
        json={
            "session_id": "sid",
            "registration_number": "12345",
            "captcha_text": "abcd",
        },
    )

    assert response.status_code == 200
    assert response.json()["doctor_image_base64"] == base64.b64encode(
        IMAGE_BYTES
    ).decode("ascii")
    assert fake.get_calls[-1][0] == "https://verify.bmdc.org.bd/doctor.jpg"
