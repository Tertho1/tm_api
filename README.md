# TM API

A small FastAPI service that wraps the **BMDC doctor verification portal**
(`verify.bmdc.org.bd`). It starts a server-side session, returns the portal's
CAPTCHA image to the client, then submits the solved CAPTCHA and registration
number and returns the doctor profile as JSON.

This is a separate deployment of the same scraper as
[`Scrapper_API`](https://github.com/Tertho1/Scrapper_API). The two repositories
share the same behavior but are maintained and deployed independently.

- Live deployment: <https://tm-api-zeta.vercel.app>
- API docs: <https://tm-api-zeta.vercel.app/docs>

> **Status: work in progress.** The core two-step flow works, but the scraper
> is inherently brittle and sessions are stored in memory. See
> [Limitations](#limitations).

> **Disclaimer:** This project is unofficial and is not affiliated with the
> Bangladesh Medical & Dental Council. Use it only for lawful purposes and in
> line with the portal's terms of service. The CAPTCHA is intentionally solved
> by a human; this API does not bypass it.

## Features

- Two-step flow that preserves BMDC cookies and CSRF tokens per client.
- Returns the CAPTCHA as base64 so any frontend can display it.
- Parses name, registration number, status, registration year, validity,
  card number, DOB, blood group, parents' names, and profile image.
- Request timeouts, upstream error handling, and session TTL cleanup.
- Configurable CORS origins and request timeout.
- OpenAPI docs at `/docs`.

## Flow

```text
Client                         TM API                           BMDC
  │                                │                               │
  ├── GET /init-session ──────────►│                               │
  │                                ├── GET / ─────────────────────►│
  │                                │◄── CSRF + CAPTCHA HTML ───────┤
  │                                ├── GET captcha image ─────────►│
  │◄── session_id + base64 CAPTCHA ┤                               │
  │                                │                               │
  ├── POST /verify-doctor ────────►│                               │
  │   (session_id, reg no, text)   ├── POST /regfind ─────────────►│
  │                                │◄── result HTML ───────────────┤
  │◄── doctor profile JSON ────────┤                               │
```

The API keeps one `requests.Session` per client so cookies and tokens survive
between the two calls. A CAPTCHA is single-use, so the session is discarded
after `/verify-doctor` returns.

## API

Interactive documentation: `http://localhost:8000/docs`

### `GET /health`

```json
{ "status": "ok", "version": "0.1.0" }
```

### `GET /init-session`

Fetches the BMDC search page and returns a CAPTCHA.

```json
{
  "session_id": "8f1c...",
  "captcha_image": "iVBORw0KGgoAAAANSUhEUg...",
  "csrf_token": "...",
  "action_key": "..."
}
```

`captcha_image` is raw base64 without a `data:` prefix. `csrf_token` and
`action_key` are returned for compatibility; the server already stores them
with the session.

### `POST /verify-doctor`

```json
{
  "session_id": "8f1c...",
  "registration_number": "12345",
  "captcha_text": "abcd",
  "reg_student": 1
}
```

Successful response:

```json
{
  "doctor_image_base64": "...",
  "name": "Dr. Jane Doe",
  "registration_number": "BMDC-123",
  "status": "Valid",
  "reg_year": "2020",
  "valid_till": "2030",
  "card_number": "CARD-9",
  "dob": "1990-01-01",
  "blood_group": "A+",
  "father_name": "Father Name",
  "mother_name": "Mother Name"
}
```

Optional fields are omitted when the upstream page does not contain them.

### Errors

| Status | Meaning |
| --- | --- |
| `400` | Invalid CAPTCHA or registration number |
| `404` | Unknown/expired session; call `/init-session` again |
| `422` | Request body validation failed |
| `500` | Unexpected server error |
| `502` | BMDC unreachable or returned an unexpected page |

## Local development

### Prerequisites

- Python 3.10+
- Network access to `verify.bmdc.org.bd`

### Setup

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

On Linux/macOS use `source .venv/bin/activate`.

### Example request

```bash
curl http://localhost:8000/init-session
curl -X POST http://localhost:8000/verify-doctor \
  -H "Content-Type: application/json" \
  -d '{"session_id":"...","registration_number":"12345","captcha_text":"abcd"}'
```

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `ALLOWED_ORIGINS` | `*` | Comma-separated CORS origins, or `*` |
| `REQUEST_TIMEOUT_SECS` | `15` | Timeout for each BMDC request |
| `SESSION_TTL_SECS` | `300` | In-memory session lifetime |

## Testing

The tests mock `requests.Session`, so they do not contact BMDC.

```powershell
pip install -r requirements-dev.txt
pytest -q
```

## Deployment (Vercel)

Vercel auto-detects the `FastAPI` instance named `app` in `main.py`.
`vercel.json` only sets the function timeout:

```json
{
  "$schema": "https://openapi.vercel.sh/vercel.json",
  "functions": {
    "main.py": {
      "maxDuration": 30
    }
  }
}
```

Set environment variables in the Vercel dashboard, then deploy. The existing
deployment is <https://tm-api-zeta.vercel.app>.

> **Important:** Vercel Functions can run in multiple instances and may cold
> start between `/init-session` and `/verify-doctor`. In-memory sessions are
> therefore not reliable in production. Use a shared store such as Redis (or
> combine the two steps into one server-side flow) before relying on this
> deployment.

## Project structure

```text
tm_api/
├── main.py                 # FastAPI app, scraper, and parsers
├── requirements.txt
├── requirements-dev.txt
├── vercel.json
├── tests/
│   └── test_main.py
└── README.md
```

## Limitations

- The scraper depends on BMDC's current HTML structure; selector changes will
  break parsing.
- The CAPTCHA must be solved by a human. This project does not automate it.
- Sessions are stored in a process-local dictionary and expire after
  `SESSION_TTL_SECS`; they are not shared across workers or serverless
  instances.
- There is no authentication, rate limiting, or abuse protection. Put it
  behind a gateway before exposing it publicly.
- Doctor data is personal information; avoid logging responses and follow
  applicable privacy rules.

## Roadmap

- Move sessions to Redis (or another shared store).
- Add response models and more parser tests.
- Add a Dockerfile and CI.
- Add rate limiting and optional API-key auth.
- Add a LICENSE.

## License

No license has been added yet.
