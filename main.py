"""
TM API — FastAPI wrapper for the BMDC doctor verification portal.

The portal is stateful: the search page issues a CSRF token and a CAPTCHA,
then expects both to be submitted with the registration number. To preserve
cookies between those two calls, one ``requests.Session`` is kept per client
and referenced by a generated ``session_id``.

Endpoints
---------
``GET  /``              service metadata
``GET  /health``        liveness probe
``GET  /init-session``  start a verification session and return a CAPTCHA
``POST /verify-doctor`` submit a registration number and CAPTCHA text

Sessions live in memory. That is fine for local development and a single
process, but serverless or multi-instance deployments need shared storage
(for example Redis) and a server-side CAPTCHA flow.
"""

import base64
import binascii
import logging
import os
import time
import uuid
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from bs4.element import Tag
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

BASE_URL = "https://verify.bmdc.org.bd/"
API_VERSION = "0.1.0"


def _env_float(name: str, default: float) -> float:
    """Read a float environment variable, falling back when invalid."""
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _parse_origins() -> list[str]:
    """Parse comma-separated CORS origins; ``*`` allows every origin."""
    raw = os.getenv("ALLOWED_ORIGINS", "*").strip()
    if raw == "*":
        return ["*"]
    origins = [origin.strip() for origin in raw.split(",") if origin.strip()]
    return origins or ["*"]


REQUEST_TIMEOUT_SECS = _env_float("REQUEST_TIMEOUT_SECS", 15.0)
SESSION_TTL_SECS = _env_float("SESSION_TTL_SECS", 300.0)
ALLOWED_ORIGINS = _parse_origins()

app = FastAPI(
    title="TM API",
    description="BMDC doctor verification scraper exposed as a JSON API.",
    version=API_VERSION,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# session_id -> {
#     "session": requests.Session,
#     "csrf_token": str,
#     "action_key": str,
#     "created_at": float,
# }
sessions: dict[str, dict[str, Any]] = {}


class VerifyRequest(BaseModel):
    """Payload for the second step of the BMDC verification flow."""

    session_id: str = Field(..., min_length=1, description="ID from /init-session")
    registration_number: str = Field(
        ..., min_length=1, description="BMDC registration number"
    )
    captcha_text: str = Field(
        ..., min_length=1, description="Text shown in the CAPTCHA image"
    )
    reg_student: int = Field(1, description="BMDC form flag; usually 1")


# -- session helpers ---------------------------------------------------------


def _new_session() -> requests.Session:
    """Create a browser-like session for one verification flow."""
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": BASE_URL,
        }
    )
    return session


def _drop_session(session_id: str) -> None:
    """Remove a session and close its HTTP connection pool."""
    data = sessions.pop(session_id, None)
    if data:
        try:
            data["session"].close()
        except Exception:
            logger.debug("Failed to close session %s", session_id, exc_info=True)


def _prune_sessions() -> None:
    """Drop sessions older than ``SESSION_TTL_SECS``."""
    cutoff = time.time() - SESSION_TTL_SECS
    expired = [
        session_id
        for session_id, data in sessions.items()
        if data["created_at"] < cutoff
    ]
    for session_id in expired:
        _drop_session(session_id)


# -- HTTP helpers ------------------------------------------------------------


def _get(session: requests.Session, url: str) -> requests.Response:
    """GET a URL with a timeout, translating network errors to HTTP 502."""
    try:
        response = session.get(url, timeout=REQUEST_TIMEOUT_SECS)
        response.raise_for_status()
        return response
    except requests.RequestException as exc:
        logger.warning("GET %s failed: %s", url, exc)
        raise HTTPException(
            status_code=502, detail="Could not reach the BMDC portal."
        ) from exc


def _post(
    session: requests.Session, url: str, data: dict[str, str]
) -> requests.Response:
    """POST form data with a timeout, translating network errors to HTTP 502."""
    try:
        response = session.post(url, data=data, timeout=REQUEST_TIMEOUT_SECS)
        response.raise_for_status()
        return response
    except requests.RequestException as exc:
        logger.warning("POST %s failed: %s", url, exc)
        raise HTTPException(
            status_code=502, detail="Could not submit the BMDC form."
        ) from exc


# -- parsing helpers ---------------------------------------------------------


def _required_input(soup: BeautifulSoup, name: str) -> str:
    """Return a hidden input value or fail with a clear upstream error."""
    tag = soup.find("input", {"name": name})
    value = tag.get("value") if isinstance(tag, Tag) else None
    if not value:
        raise HTTPException(
            status_code=502, detail=f"BMDC page is missing '{name}'."
        )
    return str(value)


def _image_src(tag: Tag | None, label: str) -> str:
    """Return an image ``src`` or fail with a clear upstream error."""
    src = tag.get("src") if isinstance(tag, Tag) else None
    if not src:
        raise HTTPException(
            status_code=502, detail=f"BMDC page is missing the {label} image."
        )
    return str(src)


def _text(tag: Tag | None) -> str:
    """Return stripped text for a tag, or an empty string when absent."""
    return tag.get_text(strip=True) if isinstance(tag, Tag) else ""


def _required_text(tag: Tag | None, label: str) -> str:
    """Return required text or fail with a clear upstream error."""
    value = _text(tag)
    if not value:
        raise HTTPException(
            status_code=502, detail=f"BMDC result is missing {label}."
        )
    return value


def _load_image(session: requests.Session, src: str) -> bytes:
    """Decode a data URI or download an image URL."""
    if src.startswith("data:image/"):
        try:
            return base64.b64decode(src.split(",", 1)[1])
        except (IndexError, ValueError, binascii.Error) as exc:
            raise HTTPException(
                status_code=502, detail="BMDC returned an invalid image."
            ) from exc

    return _get(session, urljoin(BASE_URL, src)).content


def _fetch_captcha(session: requests.Session, soup: BeautifulSoup) -> bytes:
    """Find the CAPTCHA image on the search page and return its bytes."""
    tag = soup.find("img", {"alt": " "})
    if not isinstance(tag, Tag):
        # Fall back to any image whose src looks like a CAPTCHA.
        tag = next(
            (
                image
                for image in soup.find_all("img")
                if "captcha" in str(image.get("src", "")).lower()
            ),
            None,
        )

    return _load_image(session, _image_src(tag, "CAPTCHA"))


def _parse_doctor(doctor_info: Tag, session: requests.Session) -> dict[str, Any]:
    """Extract the doctor profile from the BMDC result page.

    The upstream markup is not versioned, so every optional block is guarded
    and missing fields are simply omitted from the response.
    """
    profile = doctor_info.find("div", {"class": "profile mr-1 mb-2"})
    image_tag = (
        profile.find("img", {"class": "rounded img-responsive mb-2"})
        if isinstance(profile, Tag)
        else None
    )
    image_bytes = _load_image(session, _image_src(image_tag, "doctor profile"))

    status_div = doctor_info.find("div", {"class": "form-group row mb-0"})
    status_span = (
        status_div.find_next("span", {"class": "font-weight-bold"})
        if isinstance(status_div, Tag)
        else None
    )

    result: dict[str, Any] = {
        "doctor_image_base64": base64.b64encode(image_bytes).decode("ascii"),
        "name": _required_text(
            doctor_info.find("h3", {"class": "mb-4 font-weight-bold text-center"}),
            "the doctor name",
        ),
        "registration_number": _required_text(
            doctor_info.find(
                "h3",
                {
                    "class": (
                        "badge badge-pill badge-success mt-1 mb-3 "
                        "font-weight-bold d-block text-center text-white"
                    )
                },
            ),
            "the registration number",
        ),
        "status": _text(status_span),
    }

    # Registration year, validity, and card number.
    additional = doctor_info.find("div", {"class": "form-group row text-center"})
    if isinstance(additional, Tag):
        fields = additional.find_all("div", {"class": "col-md-4"})
        if len(fields) >= 3:
            result.update(
                {
                    "reg_year": _text(fields[0].find("span")),
                    "valid_till": _text(fields[1].find("span")),
                    "card_number": _text(fields[2].find("span")),
                }
            )

    # Personal details: DOB/blood group, father, and mother.
    rows = doctor_info.find_all("div", {"class": "form-group row mb-0"})
    if rows:
        details = rows[0].find_all("h6")
        if len(details) >= 2:
            result["dob"] = _text(details[0])
            result["blood_group"] = _text(details[1])
        if len(rows) > 1:
            result["father_name"] = _text(rows[1].find("h6"))
        if len(rows) > 2:
            result["mother_name"] = _text(rows[2].find("h6"))

    return result


# -- routes ------------------------------------------------------------------


@app.get("/", include_in_schema=False)
def root() -> dict[str, str]:
    """Small landing payload so the deployed root is not a 404."""
    return {
        "name": "TM API",
        "version": API_VERSION,
        "docs": "/docs",
        "health": "/health",
    }


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness probe for Vercel and uptime monitors."""
    return {"status": "ok", "version": API_VERSION}


@app.get("/init-session", summary="Start a verification session")
def init_session() -> dict[str, str]:
    """Fetch the BMDC search page and return a CAPTCHA for the client to solve."""
    _prune_sessions()

    session = _new_session()
    try:
        page = _get(session, BASE_URL)
        soup = BeautifulSoup(page.content, "html.parser")

        csrf_token = _required_input(soup, "bmdckyc_csrf_token")
        action_key = _required_input(soup, "action_key")
        captcha_bytes = _fetch_captcha(session, soup)
    except Exception:
        session.close()
        raise

    session_id = uuid.uuid4().hex
    sessions[session_id] = {
        "session": session,
        "csrf_token": csrf_token,
        "action_key": action_key,
        "created_at": time.time(),
    }

    return {
        "session_id": session_id,
        "captcha_image": base64.b64encode(captcha_bytes).decode("ascii"),
        "csrf_token": csrf_token,
        "action_key": action_key,
    }


@app.post("/verify-doctor", summary="Verify a doctor registration")
def verify_doctor(request: VerifyRequest) -> dict[str, Any]:
    """Submit the registration number and CAPTCHA using the stored session."""
    session_data = sessions.get(request.session_id)
    if not session_data:
        raise HTTPException(
            status_code=404, detail="Session expired. Call /init-session again."
        )

    session: requests.Session = session_data["session"]
    form_data = {
        "bmdckyc_csrf_token": session_data["csrf_token"],
        "reg_ful_no": request.registration_number,
        "reg_student": str(request.reg_student),
        "captcha_code": request.captcha_text,
        "action_key": session_data["action_key"],
        "action_flag": "1",
    }

    try:
        response = _post(session, urljoin(BASE_URL, "regfind"), form_data)
        soup = BeautifulSoup(response.content, "html.parser")
        doctor_info = soup.find("div", {"class": "form-items"})

        if not isinstance(doctor_info, Tag):
            raise HTTPException(
                status_code=400, detail="Invalid CAPTCHA or registration number."
            )

        return _parse_doctor(doctor_info, session)
    except HTTPException:
        # Preserve 400/404/502 responses instead of wrapping them as 500.
        raise
    except Exception as exc:
        logger.exception("Unexpected error while verifying registration")
        raise HTTPException(
            status_code=500, detail="Unexpected server error."
        ) from exc
    finally:
        # A CAPTCHA is single-use, so the session cannot be reused afterwards.
        _drop_session(request.session_id)
