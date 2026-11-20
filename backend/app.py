import base64
import csv
import hashlib
import hmac
from html import escape as html_escape
import json
import logging
import re
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from psycopg.errors import UniqueViolation
from starlette.concurrency import run_in_threadpool

from . import config
from . import database
from . import documents as document_store
from . import outreach
from . import sheets
from . import suppressions
from .deliverability import check_domain, recommended_records
from .unsubscribe import build_unsubscribe_url, verify_token
from .companies import (
    add_or_update_company_profile,
    load_company_profiles,
    save_all_company_profiles,
)
from .documents import DocumentError
from .sender import (
    send_account_email,
    send_email,
    sending_domain,
    test_account,
    wrap_in_html,
)
from .verifier import is_blocked_domain, is_valid_format, verify


BASE_DIR = Path(__file__).resolve().parent.parent
CONTACTS_FILE = config.CONTACTS_FILE
FRONTEND_DIR = BASE_DIR / "frontend"
FRONTEND_FILE = FRONTEND_DIR / "index.html"
CAMPAIGNS_FILE = FRONTEND_DIR / "campaigns.html"
LOGIN_FILE = FRONTEND_DIR / "login.html"

SESSION_COOKIE = "ea_session"
SESSION_MAX_AGE = 7 * 24 * 60 * 60  # 7 days
LOGIN_MAX_ATTEMPTS = 10
LOGIN_WINDOW_SECONDS = 5 * 60
PUBLIC_PATHS = {"/login", "/unsubscribe", "/api/auth/login", "/api/auth/me", "/api/auth/logout"}
PUBLIC_ASSETS = {"/frontend/login.css", "/frontend/login.js", "/favicon.ico"}

_login_attempts: dict[str, list[float]] = {}
_rate_buckets: dict[str, list[float]] = {}

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
MAX_JSON_BODY_BYTES = 2 * 1024 * 1024
MAX_UPLOAD_BODY_BYTES = document_store.MAX_FILE_BYTES + 1024 * 1024
SECURITY_CSP = (
    "default-src 'self'; base-uri 'self'; frame-ancestors 'none'; "
    "form-action 'self'; object-src 'none'; img-src 'self' data:; "
    "font-src 'self' https://fonts.gstatic.com; "
    "style-src 'self' https://fonts.googleapis.com; "
    "script-src 'self'; connect-src 'self'"
)

CONTACTS_FIELDNAMES = [
    "name",
    "email",
    "phone",
    "company_name",
    "target_role",
    "target_industry",
    "details",
]

logger = logging.getLogger(__name__)

app = FastAPI(title="Email Automation API", version="1.0.0")
app.mount("/frontend", StaticFiles(directory=FRONTEND_DIR), name="frontend")


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception) -> JSONResponse:
    error_id = secrets.token_hex(4)
    logger.exception(
        "Unhandled error %s on %s %s", error_id, request.method, request.url.path
    )
    response = JSONResponse(
        {
            "detail": (
                f"Internal server error ({type(exc).__name__}) on "
                f"{request.method} {request.url.path}. Reference: {error_id}."
            )
        },
        status_code=500,
    )
    if _is_https(request):
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


@app.exception_handler(outreach.OutreachError)
async def outreach_error(request: Request, exc: outreach.OutreachError) -> JSONResponse:
    return JSONResponse({"detail": exc.message}, status_code=exc.status_code)


@app.exception_handler(sheets.SheetsError)
async def sheets_error(request: Request, exc: sheets.SheetsError) -> JSONResponse:
    return JSONResponse({"detail": str(exc)}, status_code=422)


def _session_key() -> bytes:
    secret = (config.SESSION_SECRET or config.APP_PASSWORD) + ":" + config.APP_PASSWORD
    return hashlib.sha256(("email-automation-session:" + secret).encode("utf-8")).digest()


def _create_session(email: str) -> str:
    payload = json.dumps(
        {"email": email, "exp": int(time.time()) + SESSION_MAX_AGE},
        separators=(",", ":"),
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    signature = hmac.new(_session_key(), encoded.encode("ascii"), hashlib.sha256).hexdigest()
    return encoded + "." + signature


def _read_session(token: str | None) -> str | None:
    if not token:
        return None
    try:
        encoded, signature = token.split(".", 1)
        expected = hmac.new(_session_key(), encoded.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        padded = encoded + "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        if int(payload.get("exp", 0)) < time.time():
            return None
        return str(payload.get("email") or "") or None
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def _session_email(request: Request) -> str | None:
    return _read_session(request.cookies.get(SESSION_COOKIE))


def _is_https(request: Request) -> bool:
    forwarded = request.headers.get("x-forwarded-proto", "").lower()
    return request.url.scheme == "https" or forwarded == "https"


_HOST_PATTERN = re.compile(r"^[A-Za-z0-9.\-:\[\]]+$")


def _public_base_url(request: Request) -> str:
    if config.APP_BASE_URL:
        return config.APP_BASE_URL
    host = (request.headers.get("host") or request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "").split(",")[0].strip().lower()
    if not host or not _HOST_PATTERN.fullmatch(host) or proto not in {"http", "https"}:
        return ""
    return f"{proto}://{host}"


def _safe_equal(left: str, right: str) -> bool:
    return secrets.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _scheduler_authorized(request: Request) -> bool:
    """True when a cron/webhook presents the configured shared secret."""
    expected = config.QUEUE_TICK_SECRET
    if not expected:
        return False
    provided = (request.headers.get("x-queue-secret") or "").strip()
    authorization = (request.headers.get("authorization") or "").strip()
    if not provided and authorization.lower().startswith("bearer "):
        provided = authorization[7:].strip()
    if not provided:
        provided = (request.query_params.get("secret") or "").strip()
    return bool(provided) and _safe_equal(provided, expected)


def _login_allowed(client_ip: str) -> bool:
    now = time.time()
    if len(_login_attempts) > 5000:
        for key in [key for key, stamps in _login_attempts.items() if not stamps or max(stamps) < now - LOGIN_WINDOW_SECONDS]:
            _login_attempts.pop(key, None)
    recent = [stamp for stamp in _login_attempts.get(client_ip, []) if now - stamp < LOGIN_WINDOW_SECONDS]
    _login_attempts[client_ip] = recent
    return len(recent) < LOGIN_MAX_ATTEMPTS


@app.middleware("http")
async def require_session(request: Request, call_next):
    """Redirects anonymous page requests to /login and rejects API calls."""
    if not config.auth_enabled():
        return await call_next(request)

    path = request.url.path
    # Normalize trailing slash
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")

    if path in PUBLIC_PATHS or path in PUBLIC_ASSETS or path.startswith("/frontend/"):
        return await call_next(request)

    # Cron and provider webhooks authenticate with a shared secret instead of
    # a browser session. Both are read-only in effect and never expose data.
    if path in {"/api/queue/tick", "/api/outreach/webhook"} and _scheduler_authorized(request):
        return await call_next(request)

    if _session_email(request):
        return await call_next(request)

    if path.startswith("/api/") or path.startswith("/pitch/"):
        return JSONResponse({"detail": "Authentication required."}, status_code=401)

    next_path = request.url.path + ("?" + request.url.query if request.url.query else "")
    return RedirectResponse("/login?next=" + quote(next_path, safe=""), status_code=303)


def _rate_limited(bucket: str, identity: str, limit: int, window_seconds: int) -> bool:
    key = f"{bucket}:{identity}"
    now = time.time()
    if len(_rate_buckets) > 5000:
        for stale_key in [
            candidate
            for candidate, stamps in _rate_buckets.items()
            if not stamps or max(stamps) < now - max(window_seconds, 300)
        ]:
            _rate_buckets.pop(stale_key, None)
    recent = [stamp for stamp in _rate_buckets.get(key, []) if now - stamp < window_seconds]
    _rate_buckets[key] = recent
    if len(recent) >= limit:
        return True
    recent.append(now)
    return False


def enforce_rate_limit(request: Request, bucket: str, limit: int, window_seconds: int = 300) -> None:
    identity = request.client.host if request.client else "unknown"
    if _rate_limited(bucket, identity, limit, window_seconds):
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Please wait a few minutes and try again.",
        )


def _origin_allowed(request: Request, origin: str) -> bool:
    origin = origin.rstrip("/").lower()
    host_values = [
        request.headers.get("host", ""),
        request.headers.get("x-forwarded-host", ""),
    ]
    hosts = {
        value.split(",")[0].strip().lower()
        for value in host_values
        if value and value.strip()
    }
    schemes = {
        value.split(",")[0].strip().lower()
        for value in (request.url.scheme, request.headers.get("x-forwarded-proto", ""))
        if value and value.strip()
    }
    return any(origin == f"{scheme}://{host}" for scheme in ("http", "https") if scheme in schemes for host in hosts)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Applies body limits, same-origin checks, and hardening headers."""
    path = request.url.path

    content_length = request.headers.get("content-length", "")
    if content_length.isdigit():
        allowed = MAX_UPLOAD_BODY_BYTES if path == "/api/documents" else MAX_JSON_BODY_BYTES
        if int(content_length) > allowed:
            return JSONResponse({"detail": "Request is too large."}, status_code=413)

    # /unsubscribe accepts RFC 8058 one-click POSTs from mail clients, which are
    # cross-origin by nature; the signed token in the URL is the capability.
    if request.method in UNSAFE_METHODS and path.rstrip("/") != "/unsubscribe":
        if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
            return JSONResponse({"detail": "Cross-origin request blocked."}, status_code=403)
        origin = request.headers.get("origin")
        if origin and not _origin_allowed(request, origin):
            return JSONResponse({"detail": "Cross-origin request blocked."}, status_code=403)

    response = await call_next(request)

    headers = response.headers
    headers.setdefault("X-Content-Type-Options", "nosniff")
    headers.setdefault("X-Frame-Options", "DENY")
    headers.setdefault("Referrer-Policy", "no-referrer")
    headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if not path.startswith(("/docs", "/redoc", "/openapi.json")):
        headers.setdefault("Content-Security-Policy", SECURITY_CSP)
    if _is_https(request):
        headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    if path.startswith("/api/") or path.startswith("/pitch/") or path in {"/login", "/", "/unsubscribe"}:
        headers.setdefault("Cache-Control", "no-store")

    return response


class ContactRequest(BaseModel):
    name: str = Field(default="there", max_length=100)
    email: str = Field(..., max_length=320)
    phone: str = Field(default="", max_length=50)
    company_name: str = Field(default="", max_length=100)
    target_role: str = Field(default="", max_length=100)
    target_industry: str = Field(default="", max_length=100)
    details: str = Field(default="", max_length=1000)
    context: str = Field(default="", max_length=1000)


class SendRequest(BaseModel):
    name: str = Field(default="there", max_length=100)
    email: str = Field(..., max_length=320)
    context: str = Field(default="", max_length=1000)
    subject: str = Field(..., min_length=1, max_length=200)
    body: str = Field(..., min_length=1, max_length=10000)
    smtp_provider: str | None = Field(default=None, max_length=20)


class PitchGenerateRequest(BaseModel):
    name: str = Field(default="", max_length=100)
    email: str = Field(default="", max_length=320)
    phone: str = Field(default="", max_length=50)
    company_name: str = Field(default="", max_length=100)
    target_role: str = Field(default="", max_length=100)
    target_industry: str = Field(default="", max_length=100)
    details: str = Field(default="", max_length=1000)


class PitchSendRequest(BaseModel):
    recipient_email: str = Field(default="", max_length=320)
    subject: str = Field(default="", max_length=300)
    body: str = Field(default="", max_length=20000)
    company_name: str = Field(default="", max_length=100)
    spam_score: int = Field(default=0, ge=0, le=10)
    confirm_high_spam: bool = False
    smtp_provider: str | None = Field(default=None, max_length=20)
    smtp_user: str | None = Field(default=None, max_length=320)
    smtp_password: str | None = Field(default=None, max_length=200)
    sender_name: str | None = Field(default=None, max_length=100)


class CompanyCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    services: list[str] = Field(..., min_length=1)
    value_proposition: str = Field(default="", max_length=500)
    tone: str = Field(default="friendly-professional", max_length=100)
    signature: str = Field(default="", max_length=1000)


class SenderRequest(BaseModel):
    email: str = Field(default="", max_length=320)
    name: str = Field(default="", max_length=100)
    provider: str = Field(default="smtp", max_length=20)
    host: str = Field(default="", max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    security: str = Field(default="", max_length=20)
    username: str = Field(default="", max_length=320)
    password: str = Field(default="", max_length=500)
    brand: str = Field(default="", max_length=100)
    dkim_selector: str = Field(default="", max_length=100)
    spf_include: str | None = Field(default=None, max_length=255)
    dmarc_policy: str | None = Field(default=None, max_length=20)
    timezone: str = Field(default="UTC", max_length=80)
    hourly_limit: int | None = Field(default=None, ge=0, le=1000)
    daily_limit: int | None = Field(default=None, ge=0, le=10000)


class SenderTestRequest(BaseModel):
    recipient: str = Field(default="", max_length=320)


class BrandRequest(BaseModel):
    name: str = Field(default="", max_length=100)
    domain: str = Field(default="", max_length=200)
    website: str = Field(default="", max_length=300)
    description: str = Field(default="", max_length=1000)
    signature: str = Field(default="", max_length=2000)


class TemplateRequest(BaseModel):
    name: str = Field(default="", max_length=100)
    brand_id: str = Field(default="", max_length=40)
    subject: str = Field(default="", max_length=300)
    body: str = Field(default="", max_length=20000)
    prompt: str = Field(default="", max_length=5000)


class CampaignRequest(BaseModel):
    name: str = Field(default="", max_length=120)
    sheet_url: str = Field(default="", max_length=1000)
    sheet_gid: str = Field(default="0", max_length=20)
    sheet_tab: str = Field(default="", max_length=100)
    sender_id: str = Field(default="", max_length=40)
    template_id: str = Field(default="", max_length=40)
    brand_id: str = Field(default="", max_length=40)
    timezone: str = Field(default="UTC", max_length=80)
    hourly_limit: int | None = Field(default=None, ge=1, le=100)
    daily_limit: int | None = Field(default=None, ge=1, le=500)
    delay_min_minutes: float | None = Field(default=None, ge=1, le=240)
    delay_max_minutes: float | None = Field(default=None, ge=1, le=240)
    work_start_hour: int | None = Field(default=None, ge=0, le=23)
    work_end_hour: int | None = Field(default=None, ge=1, le=24)
    weekdays_only: bool = False
    use_ai: bool = False
    ai_prompt: str = Field(default="", max_length=5000)
    auto_complete: bool = True
    unsubscribe_base_url: str = Field(default="", max_length=500)
    mapping: dict[str, str] = Field(default_factory=dict)


class SheetPreviewRequest(BaseModel):
    sheet_url: str = Field(default="", max_length=1000)
    gid: str = Field(default="", max_length=20)
    tab: str = Field(default="", max_length=100)
    mapping: dict[str, str] = Field(default_factory=dict)


class RecipientStatusRequest(BaseModel):
    status: str = Field(default="", max_length=20)


class OutreachWebhookRequest(BaseModel):
    email: str = Field(default="", max_length=320)
    status: str = Field(default="", max_length=20)
    campaign_id: str = Field(default="", max_length=40)


class LoginRequest(BaseModel):
    email: str = Field(default="", max_length=320)
    password: str = Field(default="", max_length=200)


def load_contacts() -> list[dict[str, str]]:
    if database.is_configured():
        return database.load_contacts()
    if not CONTACTS_FILE.exists():
        return []
    with CONTACTS_FILE.open(newline="", encoding="utf-8") as contacts_file:
        contacts = []
        for row in csv.DictReader(contacts_file):
            item = {field: (row.get(field) or "").strip() for field in CONTACTS_FIELDNAMES}
            if not item["details"] and row.get("context"):
                item["details"] = (row.get("context") or "").strip()
            item["context"] = item["details"]
            contacts.append(item)
        return contacts


def normalize_contact(contact: ContactRequest) -> dict[str, str]:
    name = contact.name.strip()
    email = contact.email.strip().lower()
    details = contact.details.strip() or contact.context.strip()

    if not name:
        raise HTTPException(status_code=422, detail="Please provide a name.")
    if not is_valid_format(email):
        raise HTTPException(status_code=422, detail="Please provide a valid email address.")
    if is_blocked_domain(email):
        raise HTTPException(status_code=422, detail="Disposable email domains are not allowed.")

    return {
        "name": name,
        "email": email,
        "phone": contact.phone.strip(),
        "company_name": contact.company_name.strip(),
        "target_role": contact.target_role.strip(),
        "target_industry": contact.target_industry.strip(),
        "details": details,
    }


def write_contacts(contacts: list[dict[str, str]]) -> None:
    CONTACTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with CONTACTS_FILE.open("w", newline="", encoding="utf-8") as contacts_file:
        writer = csv.DictWriter(contacts_file, fieldnames=CONTACTS_FIELDNAMES)
        writer.writeheader()
        writer.writerows({field: row.get(field, "") for field in CONTACTS_FIELDNAMES} for row in contacts)


def save_contact(contact: ContactRequest) -> dict[str, str]:
    new_contact = normalize_contact(contact)

    if database.is_configured():
        try:
            database.insert_contact(new_contact)
        except UniqueViolation as error:
            raise HTTPException(
                status_code=409,
                detail="A contact with this email already exists.",
            ) from error
        new_contact["context"] = new_contact["details"]
        return new_contact

    existing_emails = {row.get("email", "").strip().lower() for row in load_contacts()}
    if new_contact["email"] in existing_emails:
        raise HTTPException(status_code=409, detail="A contact with this email already exists.")

    contacts = load_contacts()
    contacts.append(new_contact)
    write_contacts(contacts)

    new_contact["context"] = new_contact["details"]
    return new_contact


def update_contact(original_email: str, contact: ContactRequest) -> dict[str, str]:
    new_contact = normalize_contact(contact)
    original_email = original_email.strip().lower()

    if database.is_configured():
        try:
            updated = database.update_contact(original_email, new_contact)
        except UniqueViolation as error:
            raise HTTPException(
                status_code=409,
                detail="Another contact already uses this email.",
            ) from error
        if not updated:
            raise HTTPException(status_code=404, detail="Contact not found.")
        if original_email != new_contact["email"]:
            document_store.move_documents("contact", original_email, new_contact["email"])
        new_contact["context"] = new_contact["details"]
        return new_contact

    contacts = load_contacts()
    contact_index = next(
        (index for index, row in enumerate(contacts) if row.get("email", "").lower() == original_email),
        None,
    )
    if contact_index is None:
        raise HTTPException(status_code=404, detail="Contact not found.")

    duplicate_exists = any(
        index != contact_index and row.get("email", "").lower() == new_contact["email"]
        for index, row in enumerate(contacts)
    )
    if duplicate_exists:
        raise HTTPException(status_code=409, detail="Another contact already uses this email.")

    contacts[contact_index] = new_contact
    write_contacts(contacts)
    if original_email != new_contact["email"]:
        document_store.move_documents("contact", original_email, new_contact["email"])
    new_contact["context"] = new_contact["details"]
    return new_contact


def remove_contact(email: str) -> None:
    email = email.strip().lower()
    if database.is_configured():
        if not database.delete_contact(email):
            raise HTTPException(status_code=404, detail="Contact not found.")
        document_store.delete_documents_for("contact", email)
        return

    contacts = load_contacts()
    remaining = [row for row in contacts if row.get("email", "").lower() != email]
    if len(remaining) == len(contacts):
        raise HTTPException(status_code=404, detail="Contact not found.")
    write_contacts(remaining)
    document_store.delete_documents_for("contact", email)


def build_preview(contact: ContactRequest) -> dict[str, str]:
    try:
        from .llm import generate_email
    except ValueError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error

    reference = document_store.build_reference_text("contact", contact.email.strip().lower())
    try:
        generated = generate_email(
            contact.name.strip() or "there",
            contact.email.strip(),
            contact.context.strip() or contact.details.strip(),
            documents=reference,
        )
    except Exception as error:
        raise HTTPException(status_code=502, detail=f"AI preview failed: {error}") from error

    return {
        "subject": generated["subject"],
        "body": generated["body"],
        "mode": "ai_preview",
    }


@app.get("/", include_in_schema=False)
def frontend() -> FileResponse:
    return FileResponse(FRONTEND_FILE)


@app.get("/campaigns", include_in_schema=False)
@app.get("/campaigns/", include_in_schema=False)
def campaigns_page() -> FileResponse:
    return FileResponse(CAMPAIGNS_FILE)


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "favicon.svg", media_type="image/svg+xml")


@app.get("/login", include_in_schema=False)
@app.get("/login/", include_in_schema=False)
def login_page(request: Request):
    if not config.auth_enabled():
        return RedirectResponse("/", status_code=303)
    if _session_email(request):
        next_url = request.query_params.get("next") or "/"
        if next_url.startswith("//") or not next_url.startswith("/"):
            next_url = "/"
        return RedirectResponse(next_url, status_code=303)
    return FileResponse(LOGIN_FILE)


def _unsubscribe_page(
    title: str,
    message: str,
    *,
    email: str = "",
    token: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    email_block = f"<p class='email'>{html_escape(email)}</p>" if email else ""
    form = ""
    if token:
        form = (
            "<form method='post' action='/unsubscribe'>"
            f"<input type='hidden' name='token' value='{html_escape(token)}'>"
            "<button type='submit'>Unsubscribe</button>"
            "</form>"
        )
    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta name="robots" content="noindex">
  <title>{html_escape(title)} · Email Automation</title>
  <link rel="stylesheet" href="/frontend/unsubscribe.css">
</head>
<body>
  <main class="card">
    <span class="brand-mark">EA</span>
    <h1>{html_escape(title)}</h1>
    <p>{html_escape(message)}</p>
    {email_block}
    {form}
  </main>
</body>
</html>"""
    return HTMLResponse(page, status_code=status_code)


@app.get("/unsubscribe", include_in_schema=False)
def unsubscribe_page(token: str = "") -> HTMLResponse:
    email = verify_token(token)
    if not email:
        return _unsubscribe_page(
            "Invalid unsubscribe link",
            "This link is invalid. Reply to the email and ask to be removed instead.",
            status_code=400,
        )
    try:
        already_unsubscribed = suppressions.is_suppressed(email)
    except Exception:
        logger.exception("Unsubscribe lookup failed for %s", email)
        return _unsubscribe_page(
            "Temporarily unavailable",
            "We could not process this request right now. Please try again shortly.",
            status_code=503,
        )
    if already_unsubscribed:
        return _unsubscribe_page(
            "Already unsubscribed",
            "This address is already on the do-not-email list.",
            email=email,
        )
    return _unsubscribe_page(
        "Unsubscribe",
        "Stop future emails to this address?",
        email=email,
        token=token,
    )


@app.post("/unsubscribe", include_in_schema=False)
async def unsubscribe_submit(request: Request, token: str = "") -> HTMLResponse:
    if not token:
        form = await request.form()
        token = str(form.get("token") or "")
    email = verify_token(token)
    if not email:
        return _unsubscribe_page(
            "Invalid unsubscribe link",
            "This link is invalid. Reply to the email and ask to be removed instead.",
            status_code=400,
        )
    try:
        suppressions.add(email)
    except Exception:
        logger.exception("Unsubscribe write failed for %s", email)
        return _unsubscribe_page(
            "Temporarily unavailable",
            "We could not process this request right now. Please try again shortly.",
            status_code=503,
        )
    return _unsubscribe_page(
        "You're unsubscribed",
        "You will not receive further emails from us.",
        email=email,
    )


@app.get("/api/auth/me")
def auth_me(request: Request) -> dict[str, Any]:
    email = _session_email(request)
    return {
        "authenticated": bool(email),
        "email": email or "",
        "auth_enabled": config.auth_enabled(),
    }


@app.post("/api/auth/login")
def auth_login(payload: LoginRequest, request: Request):
    if not config.auth_enabled():
        return {"email": "", "auth_enabled": False}

    client_ip = request.client.host if request.client else "unknown"
    if not _login_allowed(client_ip):
        raise HTTPException(
            status_code=429,
            detail="Too many sign-in attempts. Please wait a few minutes and try again.",
        )

    email = payload.email.strip().lower()
    valid = _safe_equal(email, config.APP_EMAIL) & _safe_equal(payload.password, config.APP_PASSWORD)
    if not valid:
        _login_attempts.setdefault(client_ip, []).append(time.time())
        raise HTTPException(status_code=401, detail="Incorrect email or password.")

    _login_attempts.pop(client_ip, None)
    response = JSONResponse({"email": config.APP_EMAIL, "auth_enabled": True})
    response.set_cookie(
        SESSION_COOKIE,
        _create_session(config.APP_EMAIL),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=_is_https(request),
        path="/",
    )
    return response


@app.post("/api/auth/logout")
def auth_logout() -> JSONResponse:
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "email-automation",
        "database_configured": database.is_configured(),
        "llm_provider": config.LLM_PROVIDER,
        "llm_configured": config.LLM_API_KEY_CONFIGURED,
        "smtp_configured": config.smtp_configured(),
        "smtp_provider": config.SMTP_PROVIDER,
        "sender_domain": sending_domain(),
        "app_base_url_configured": bool(config.APP_BASE_URL),
        "auth_enabled": config.auth_enabled(),
        "google_sheets_configured": bool(config.GOOGLE_SHEETS_API_KEY),
        "queue_worker_enabled": config.QUEUE_WORKER_ENABLED,
        "cron_secret_configured": bool(config.QUEUE_TICK_SECRET),
    }


@app.get("/api/deliverability")
def deliverability(
    request: Request,
    provider: str = "",
    domain: str = "",
    dkim_selector: str = "",
    refresh: bool = False,
) -> dict[str, Any]:
    enforce_rate_limit(request, "deliverability:check", limit=30, window_seconds=60)
    requested = provider.strip().lower()
    if requested and requested not in {"zoho", "gmail", "custom", "smtp", "outlook"}:
        raise HTTPException(status_code=400, detail="Unknown SMTP provider.")

    requested_domain = domain.strip().lower()
    if requested_domain:
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", requested_domain):
            raise HTTPException(status_code=400, detail="Enter a valid sending domain.")
        target = requested_domain
    else:
        target = sending_domain(requested or None)
    if not target:
        raise HTTPException(
            status_code=400,
            detail=(
                "No sending address is configured for this provider. "
                "Add SMTP_USER (or GMAIL_USER for Gmail) first."
            ),
        )
    try:
        return check_domain(
            target,
            requested or None,
            refresh=refresh,
            dkim_selector=dkim_selector,
        )
    except Exception as error:
        logger.exception("Deliverability check failed for %s", target)
        raise HTTPException(status_code=502, detail=f"DNS check failed: {error}") from error


@app.get("/companies")
@app.get("/api/companies")
def get_companies() -> dict[str, Any]:
    profiles = load_company_profiles()
    return {
        "companies": list(profiles.keys()),
        "profiles": profiles,
    }


@app.post("/companies", status_code=201)
@app.post("/api/companies", status_code=201)
def create_company(request: CompanyCreateRequest) -> dict[str, Any]:
    name = request.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Company name cannot be blank.")
    clean_services = [s.strip() for s in request.services if s.strip()]
    if not clean_services:
        raise HTTPException(status_code=400, detail="Please provide at least one service.")

    try:
        profile = add_or_update_company_profile(
            name=name,
            services=clean_services,
            value_proposition=request.value_proposition,
            tone=request.tone,
            signature=request.signature,
        )
    except Exception as err:
        raise HTTPException(status_code=400, detail=str(err))

    return {
        "message": f"Company '{name}' added successfully.",
        "company": {"name": name, **profile},
        "companies": list(load_company_profiles().keys()),
    }


@app.delete("/companies/{company_name}")
@app.delete("/api/companies/{company_name}")
def delete_company(company_name: str) -> dict[str, Any]:
    company_name = company_name.strip()
    if database.is_configured():
        if not database.delete_company_profile(company_name):
            raise HTTPException(status_code=404, detail="Company profile not found.")
        document_store.delete_documents_for("company", company_name)
        profiles = database.load_company_profiles()
        return {"message": f"Company '{company_name}' removed.", "companies": list(profiles.keys())}

    profiles = load_company_profiles()
    if company_name not in profiles:
        raise HTTPException(status_code=404, detail="Company profile not found.")
    del profiles[company_name]
    save_all_company_profiles(profiles)
    document_store.delete_documents_for("company", company_name)
    return {"message": f"Company '{company_name}' removed.", "companies": list(profiles.keys())}


@app.get("/api/documents")
def get_documents(owner_type: str, owner_key: str) -> dict[str, Any]:
    owner_type, owner_key = ensure_owner_exists(owner_type, owner_key)
    return {
        "documents": document_store.list_documents(owner_type, owner_key),
        "max_documents": document_store.MAX_DOCUMENTS_PER_OWNER,
        "max_file_mb": document_store.MAX_FILE_BYTES // (1024 * 1024),
    }


@app.post("/api/documents", status_code=201)
async def add_document(
    request: Request,
    owner_type: str = Form(...),
    owner_key: str = Form(...),
    file: UploadFile = File(...),
) -> dict[str, Any]:
    enforce_rate_limit(request, "documents:upload", limit=20)
    owner_type, owner_key = ensure_owner_exists(owner_type, owner_key)

    data = await file.read(document_store.MAX_FILE_BYTES + 1)
    if len(data) > document_store.MAX_FILE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Files must be {document_store.MAX_FILE_BYTES // (1024 * 1024)} MB or smaller.",
        )

    try:
        document = await run_in_threadpool(
            document_store.add_document,
            owner_type,
            owner_key,
            file.filename or "document",
            data,
        )
    except DocumentError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    return {
        "message": f"Added '{document['filename']}'.",
        "document": document,
    }


@app.delete("/api/documents/{document_id}")
def remove_document(document_id: str, request: Request) -> dict[str, str]:
    enforce_rate_limit(request, "documents:delete", limit=30)
    if not document_store.delete_document(document_id):
        raise HTTPException(status_code=404, detail="Document not found.")
    return {"message": "Document removed."}


def ensure_owner_exists(owner_type: str, owner_key: str) -> tuple[str, str]:
    try:
        owner_type, owner_key = document_store.normalize_owner(owner_type, owner_key)
    except DocumentError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    if owner_type == "contact":
        emails = {row.get("email", "").strip().lower() for row in load_contacts()}
        if owner_key not in emails:
            raise HTTPException(
                status_code=404,
                detail="Save the contact first, then attach documents to it.",
            )
    else:
        if owner_key not in load_company_profiles():
            raise HTTPException(
                status_code=404,
                detail="Create the company first, then attach documents to it.",
            )
    return owner_type, owner_key


@app.get("/api/contacts")
def contacts() -> dict[str, Any]:
    rows = load_contacts()
    return {"contacts": rows, "count": len(rows)}


@app.post("/api/contacts", status_code=201)
def add_contact(contact: ContactRequest) -> dict[str, Any]:
    saved_contact = save_contact(contact)
    return {"contact": saved_contact, "message": "Contact added successfully."}


@app.put("/api/contacts/{original_email}")
def edit_contact(original_email: str, contact: ContactRequest) -> dict[str, Any]:
    updated_contact = update_contact(original_email, contact)
    return {"contact": updated_contact, "message": "Contact changes saved."}


@app.delete("/api/contacts/{email}")
def delete_contact(email: str) -> dict[str, str]:
    remove_contact(email)
    return {"message": "Contact removed."}


def log_send(email: str, status: str, subject: str) -> None:
    if database.is_configured():
        database.log_send(email, status, subject)
        return

    log_file = config.LOG_FILE
    log_file.parent.mkdir(parents=True, exist_ok=True)
    file_exists = log_file.exists() and log_file.stat().st_size > 0
    with log_file.open("a", newline="", encoding="utf-8") as log:
        writer = csv.writer(log)
        if not file_exists:
            writer.writerow(["timestamp", "email", "status", "subject"])
        writer.writerow([datetime.now().isoformat(), email, status, subject])


SENT_STATUSES = {"sent", "pitch:sent"}


def sent_today_count() -> int:
    if database.is_configured():
        return database.count_sent_today()

    log_file = config.LOG_FILE
    if not log_file.exists():
        return 0
    today = datetime.now().date().isoformat()
    with log_file.open(newline="", encoding="utf-8") as log:
        return sum(
            1
            for row in csv.DictReader(log)
            if row.get("status", "") in SENT_STATUSES
            and (row.get("timestamp") or "").startswith(today)
        )


def reject_suppressed(email: str) -> None:
    try:
        suppressed = suppressions.is_suppressed(email)
    except Exception as error:
        logger.exception("Suppression lookup failed for %s", email)
        raise HTTPException(
            status_code=503,
            detail="Could not check the suppression list. Please try again.",
        ) from error
    if suppressed:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{email} has unsubscribed from your emails. "
                "Remove the address from the suppression list before sending."
            ),
        )


def enforce_daily_limit() -> None:
    sent_today = sent_today_count()
    if sent_today >= config.DAILY_LIMIT:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Daily send limit reached ({sent_today}/{config.DAILY_LIMIT}). "
                "Increase DAILY_LIMIT to send more today."
            ),
        )


def score_spam(body: str, subject: str = "") -> dict[str, Any]:
    try:
        from .llm import check_spam_score
    except ValueError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error

    try:
        return check_spam_score(body, subject)
    except Exception as error:
        raise HTTPException(status_code=502, detail=f"Spam check failed: {error}") from error


def enforce_spam_threshold(body: str, subject: str = "") -> None:
    spam = score_spam(body, subject)
    if spam["score"] >= 7:
        raise HTTPException(
            status_code=422,
            detail=f"Email blocked because its spam score is {spam['score']}/10: {spam['issues']}",
        )


@app.post("/api/send")
def send_contact(message: SendRequest, request: Request) -> dict[str, Any]:
    enforce_rate_limit(request, "email:send", limit=10)
    email = message.email.strip()
    if not is_valid_format(email):
        raise HTTPException(status_code=422, detail="Please provide a valid email address.")
    if is_blocked_domain(email):
        raise HTTPException(status_code=422, detail="Disposable email domains are not allowed.")
    reject_suppressed(email)

    enforce_daily_limit()
    enforce_spam_threshold(message.body, message.subject)
    unsubscribe_url = build_unsubscribe_url(email, _public_base_url(request))
    html = wrap_in_html(message.body, config.SENDER_NAME, unsubscribe_url=unsubscribe_url or None)
    success, send_error = send_email(
        email,
        message.subject.strip(),
        html,
        message.body,
        smtp_provider=message.smtp_provider,
        unsubscribe_url=unsubscribe_url or None,
    )
    log_send(email, "sent" if success else "failed", message.subject.strip())
    if not success:
        raise HTTPException(
            status_code=502,
            detail=f"Email could not be sent: {send_error}",
        )
    return {"message": f"Email sent to {email}.", "email": email, "subject": message.subject.strip()}


@app.post("/api/verify")
def verify_contact(contact: ContactRequest, request: Request, check_dns: bool = False) -> dict[str, Any]:
    enforce_rate_limit(request, "email:verify", limit=30)
    email = contact.email.strip()
    if not is_valid_format(email):
        return {"email": email, "valid": False, "reason": "invalid_format"}
    if is_blocked_domain(email):
        return {"email": email, "valid": False, "reason": "blocked_domain"}
    if not check_dns:
        return {"email": email, "valid": True, "reason": "format_valid"}
    valid, reason = verify(email)
    return {"email": email, "valid": valid, "reason": reason}


@app.post("/api/preview")
def preview(contact: ContactRequest, request: Request) -> dict[str, Any]:
    enforce_rate_limit(request, "llm:preview", limit=20)
    if not is_valid_format(contact.email.strip()):
        raise HTTPException(status_code=422, detail="Please provide a valid email address.")
    return build_preview(contact)


@app.post("/pitch/generate")
@app.post("/api/pitch/generate")
def generate_pitch(request: PitchGenerateRequest, http_request: Request) -> dict[str, Any]:
    enforce_rate_limit(http_request, "llm:pitch", limit=20)
    try:
        from .llm import generate_pitch_email
    except ValueError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error

    name = request.name.strip()
    email = request.email.strip()
    company_name = request.company_name.strip()
    target_role = request.target_role.strip()
    target_industry = request.target_industry.strip()
    phone = request.phone.strip()
    details = request.details.strip()

    missing = []
    if not name:
        missing.append("Target Name (name)")
    if not email:
        missing.append("Target Email (email)")
    if not company_name:
        missing.append("Company Name (company_name)")
    if not target_role:
        missing.append("Target Role (target_role)")
    if not target_industry:
        missing.append("Target Industry (target_industry)")

    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Missing required field(s): {', '.join(missing)}",
        )

    valid, reason = verify(email)
    if not valid:
        raise HTTPException(
            status_code=400,
            detail=f"Email verification failed ({reason}). Please check the target email address.",
        )

    try:
        pitch = generate_pitch_email(
            recipient_name=name,
            recipient_email=email,
            recipient_phone=phone,
            company_name=company_name,
            target_role=target_role,
            target_industry=target_industry,
            details=details,
            recipient_documents=document_store.build_reference_text(
                "contact", email.lower()
            ),
            company_documents=document_store.build_reference_text(
                "company", company_name
            ),
        )
    except Exception as err:
        raise HTTPException(
            status_code=502,
            detail=f"Pitch generation failed: {err}",
        ) from err

    return {
        "subject": pitch["subject"],
        "body": pitch["body"],
        "spam_score": pitch["spam_score"],
    }


@app.post("/pitch/send")
@app.post("/api/pitch/send")
def send_pitch(request: PitchSendRequest, http_request: Request) -> dict[str, Any]:
    enforce_rate_limit(http_request, "email:pitch-send", limit=10)
    recipient_email = request.recipient_email.strip()
    subject = request.subject.strip()
    body = request.body.strip()
    company_name = request.company_name.strip()

    missing = []
    if not recipient_email:
        missing.append("recipient_email")
    if not subject:
        missing.append("subject")
    if not body:
        missing.append("body")

    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Missing required field(s): {', '.join(missing)}",
        )

    if not is_valid_format(recipient_email) or is_blocked_domain(recipient_email):
        raise HTTPException(
            status_code=400,
            detail="Invalid recipient email format or domain is blocked.",
        )
    reject_suppressed(recipient_email)

    enforce_daily_limit()
    spam = score_spam(body, subject)
    server_score = int(spam.get("score", 0) or 0)
    if server_score >= 7 and not request.confirm_high_spam:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Pitch blocked because its spam score is {server_score}/10: "
                f"{spam.get('issues', 'unknown')}. Explicit confirmation is required to send it."
            ),
        )

    sender_name = request.sender_name.strip() if request.sender_name and request.sender_name.strip() else (company_name or config.SENDER_NAME)
    unsubscribe_url = build_unsubscribe_url(recipient_email, _public_base_url(http_request))
    html = wrap_in_html(body, sender_name, unsubscribe_url=unsubscribe_url or None)
    success, send_error = send_email(
        recipient_email,
        subject,
        html,
        body,
        smtp_provider=request.smtp_provider,
        smtp_user=request.smtp_user,
        smtp_password=request.smtp_password,
        sender_name=sender_name,
        unsubscribe_url=unsubscribe_url or None,
    )
    log_send(recipient_email, "pitch:sent" if success else "pitch:failed", subject)

    if success:
        return {
            "status": "sent",
            "message": f"Pitch email successfully sent to {recipient_email}.",
            "spam_score": server_score,
        }

    raise HTTPException(
        status_code=502,
        detail=f"Pitch email delivery failed: {send_error}",
    )

# ---------------------------------------------------------------------------
# Outreach: senders, brands, templates, campaigns, Google Sheets, and queue
# ---------------------------------------------------------------------------


def _sender_list() -> list[dict[str, Any]]:
    state = outreach.load_state()
    return [outreach.public_sender(sender) for sender in state["senders"]]


def _sync_campaign_from_sheet(campaign_id: str) -> dict[str, Any]:
    campaign = outreach.get_campaign(campaign_id)
    if not campaign.get("sheet_url"):
        raise HTTPException(
            status_code=422,
            detail="Add a Google Sheet URL to this campaign before syncing.",
        )
    fetched = sheets.fetch_leads(
        campaign["sheet_url"],
        gid=campaign.get("sheet_gid", ""),
        tab=campaign.get("sheet_tab", ""),
        mapping=campaign.get("mapping"),
        suppress=suppressions.all_emails(),
    )
    result = outreach.add_leads(campaign_id, fetched["leads"])
    result["sheet"] = {
        "headers": fetched.get("headers", []),
        "total_rows": fetched.get("total_rows", 0),
        "usable_leads": len(fetched.get("leads", [])),
        "skipped": fetched.get("skipped", {}),
    }
    return result


@app.get("/api/outreach/overview")
def outreach_overview() -> dict[str, Any]:
    return outreach.overview()


@app.get("/api/senders")
def list_senders() -> dict[str, Any]:
    return {"senders": _sender_list()}


@app.post("/api/senders", status_code=201)
def add_sender(payload: SenderRequest, request: Request) -> dict[str, Any]:
    enforce_rate_limit(request, "outreach:sender-write", limit=20)
    return outreach.create_sender(payload.model_dump(exclude_unset=True))


@app.put("/api/senders/{sender_id}")
def edit_sender(sender_id: str, payload: SenderRequest, request: Request) -> dict[str, Any]:
    enforce_rate_limit(request, "outreach:sender-write", limit=20)
    return outreach.update_sender(sender_id, payload.model_dump(exclude_unset=True))


@app.delete("/api/senders/{sender_id}")
def remove_sender(sender_id: str, request: Request) -> dict[str, Any]:
    enforce_rate_limit(request, "outreach:sender-write", limit=20)
    return outreach.delete_sender(sender_id)


@app.post("/api/senders/{sender_id}/test")
def test_sender(sender_id: str, payload: SenderTestRequest, request: Request) -> dict[str, Any]:
    enforce_rate_limit(request, "outreach:sender-test", limit=20)
    account = outreach.get_sender_account(sender_id)
    recipient = payload.recipient.strip()
    if recipient:
        if not is_valid_format(recipient) or is_blocked_domain(recipient):
            raise HTTPException(status_code=422, detail="Enter a valid test recipient address.")
        html = wrap_in_html(
            "This is a test message from your Email Automation sender account.\n\n"
            f"SMTP host: {account.get('host')}:{account.get('port')}\n"
            f"From: {account.get('email')}",
            account.get("name") or config.SENDER_NAME,
        )
        sent, error, _ = send_account_email(
            account,
            recipient,
            f"SMTP test from {account.get('email')}",
            html,
            "This is a test message from your Email Automation sender account.",
        )
        return {
            "ok": sent,
            "message": (
                f"Test email sent to {recipient}."
                if sent
                else f"Test email failed: {error}"
            ),
        }
    ok, message = test_account(account)
    return {"ok": ok, "message": message}


@app.get("/api/senders/{sender_id}/deliverability")
def sender_deliverability(
    sender_id: str, request: Request, refresh: bool = False
) -> dict[str, Any]:
    enforce_rate_limit(request, "deliverability:check", limit=30, window_seconds=60)
    account = outreach.get_sender_account(sender_id)
    domain = (account.get("email") or "").rsplit("@", 1)[-1].lower()
    if not domain:
        raise HTTPException(status_code=422, detail="Sender account has no domain.")
    provider = account.get("provider") or "smtp"
    result = check_domain(
        domain,
        provider,
        refresh=refresh,
        dkim_selector=account.get("dkim_selector", ""),
    )
    # Show the records this specific sending domain should publish, using the
    # sender's own SPF include, DKIM selector, and DMARC policy.
    result["setup"] = recommended_records(
        domain,
        provider,
        dkim_selector=account.get("dkim_selector", ""),
        spf_include=account.get("spf_include", ""),
        dmarc_policy=account.get("dmarc_policy", ""),
    )
    result["sender_id"] = sender_id
    result["sender_email"] = account.get("email", "")
    return result


@app.get("/api/brands")
def list_brands() -> dict[str, Any]:
    state = outreach.load_state()
    return {"brands": state["brands"]}


@app.post("/api/brands", status_code=201)
def add_brand(payload: BrandRequest, request: Request) -> dict[str, Any]:
    enforce_rate_limit(request, "outreach:brand-write", limit=30)
    return outreach.create_brand(payload.model_dump(exclude_unset=True))


@app.put("/api/brands/{brand_id}")
def edit_brand(brand_id: str, payload: BrandRequest, request: Request) -> dict[str, Any]:
    enforce_rate_limit(request, "outreach:brand-write", limit=30)
    return outreach.update_brand(brand_id, payload.model_dump(exclude_unset=True))


@app.delete("/api/brands/{brand_id}")
def remove_brand(brand_id: str, request: Request) -> dict[str, Any]:
    enforce_rate_limit(request, "outreach:brand-write", limit=30)
    return outreach.delete_brand(brand_id)


@app.get("/api/templates")
def list_templates() -> dict[str, Any]:
    state = outreach.load_state()
    brands = {brand["id"]: brand.get("name", "") for brand in state["brands"]}
    templates = []
    for template in state["templates"]:
        view = dict(template)
        view["brand_name"] = brands.get(template.get("brand_id", ""), "")
        templates.append(view)
    return {"templates": templates}


@app.post("/api/templates", status_code=201)
def add_template(payload: TemplateRequest, request: Request) -> dict[str, Any]:
    enforce_rate_limit(request, "outreach:template-write", limit=30)
    return outreach.create_template(payload.model_dump(exclude_unset=True))


@app.put("/api/templates/{template_id}")
def edit_template(
    template_id: str, payload: TemplateRequest, request: Request
) -> dict[str, Any]:
    enforce_rate_limit(request, "outreach:template-write", limit=30)
    return outreach.update_template(template_id, payload.model_dump(exclude_unset=True))


@app.delete("/api/templates/{template_id}")
def remove_template(template_id: str, request: Request) -> dict[str, Any]:
    enforce_rate_limit(request, "outreach:template-write", limit=30)
    return outreach.delete_template(template_id)


@app.get("/api/campaigns")
def list_campaigns() -> dict[str, Any]:
    return {"campaigns": outreach.list_campaigns()}


@app.post("/api/campaigns", status_code=201)
def add_campaign(payload: CampaignRequest, request: Request) -> dict[str, Any]:
    enforce_rate_limit(request, "outreach:campaign-write", limit=30)
    return outreach.create_campaign(payload.model_dump(exclude_unset=True))


@app.get("/api/campaigns/{campaign_id}")
def get_campaign(campaign_id: str, limit: int = 100) -> dict[str, Any]:
    return outreach.campaign_report(campaign_id, limit=limit)


@app.put("/api/campaigns/{campaign_id}")
def edit_campaign(
    campaign_id: str, payload: CampaignRequest, request: Request
) -> dict[str, Any]:
    enforce_rate_limit(request, "outreach:campaign-write", limit=30)
    return outreach.update_campaign(campaign_id, payload.model_dump(exclude_unset=True))


@app.delete("/api/campaigns/{campaign_id}")
def remove_campaign(campaign_id: str, request: Request) -> dict[str, Any]:
    enforce_rate_limit(request, "outreach:campaign-write", limit=30)
    return outreach.delete_campaign(campaign_id)


@app.post("/api/campaigns/{campaign_id}/start")
def start_campaign(campaign_id: str, request: Request) -> dict[str, Any]:
    enforce_rate_limit(request, "outreach:campaign-action", limit=30)
    campaign = outreach.get_campaign(campaign_id)
    if campaign["stats"]["total"] == 0 and campaign.get("sheet_url"):
        _sync_campaign_from_sheet(campaign_id)
    return outreach.start_campaign(campaign_id)


@app.post("/api/campaigns/{campaign_id}/pause")
def pause_campaign(campaign_id: str, request: Request) -> dict[str, Any]:
    enforce_rate_limit(request, "outreach:campaign-action", limit=30)
    return outreach.pause_campaign(campaign_id)


@app.post("/api/campaigns/{campaign_id}/requeue")
def requeue_campaign(campaign_id: str, request: Request) -> dict[str, Any]:
    enforce_rate_limit(request, "outreach:campaign-action", limit=30)
    return outreach.requeue(campaign_id)


@app.post("/api/campaigns/{campaign_id}/sync")
def sync_campaign(campaign_id: str, request: Request) -> dict[str, Any]:
    enforce_rate_limit(request, "outreach:sync", limit=10, window_seconds=300)
    return _sync_campaign_from_sheet(campaign_id)


@app.get("/api/campaigns/{campaign_id}/recipients")
def campaign_recipients(
    campaign_id: str, status: str = "", limit: int = 100
) -> dict[str, Any]:
    report = outreach.campaign_report(campaign_id, limit=500)
    recipients = report["recipients"]
    if status:
        recipients = [
            recipient for recipient in recipients if recipient.get("status") == status
        ]
    return {
        "recipients": recipients[: max(1, min(500, limit))],
        "counts": report["counts"],
        "deliverability": report["deliverability"],
    }


@app.post("/api/campaigns/{campaign_id}/recipients/{recipient_id}/status")
def set_recipient_status(
    campaign_id: str,
    recipient_id: str,
    payload: RecipientStatusRequest,
    request: Request,
) -> dict[str, Any]:
    enforce_rate_limit(request, "outreach:recipient-status", limit=60)
    return outreach.mark_recipient(recipient_id, payload.status.strip().lower())


@app.post("/api/sheets/preview")
def preview_sheet(payload: SheetPreviewRequest, request: Request) -> dict[str, Any]:
    enforce_rate_limit(request, "outreach:sheet-preview", limit=20, window_seconds=300)
    return sheets.preview_sheet(
        payload.sheet_url,
        gid=payload.gid,
        tab=payload.tab,
        mapping=payload.mapping or None,
    )


@app.get("/api/queue/tick")
@app.post("/api/queue/tick")
def queue_tick(request: Request) -> dict[str, Any]:
    if (
        config.auth_enabled()
        and not _scheduler_authorized(request)
        and not _session_email(request)
    ):
        raise HTTPException(status_code=401, detail="Authentication required.")
    return outreach.process_queue(max_sends=config.QUEUE_MAX_PER_TICK)


@app.post("/api/outreach/webhook")
def outreach_webhook(
    payload: OutreachWebhookRequest, request: Request
) -> dict[str, Any]:
    if not _scheduler_authorized(request):
        raise HTTPException(status_code=401, detail="Invalid webhook secret.")
    return outreach.mark_by_email(
        payload.email, payload.status.strip().lower(), payload.campaign_id.strip()
    )


@app.on_event("startup")
def _start_outreach_worker() -> None:
    # Runs once per process at ASGI startup, not per request. start_worker()
    # audits credentials first (read-only, so a legacy-key warning still appears
    # where QUEUE_WORKER_ENABLED is false) and only then guards the in-process
    # queue, so a Vercel deploy without DATABASE_URL degrades to "worker
    # disabled" instead of failing every route; cron ticks are guarded in
    # outreach.process_queue().
    outreach.start_worker()


@app.on_event("shutdown")
def _stop_outreach_worker() -> None:
    outreach.stop_worker()
