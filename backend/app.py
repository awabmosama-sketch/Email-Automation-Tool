import csv
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import config
from .companies import (
    add_or_update_company_profile,
    load_company_profiles,
    save_all_company_profiles,
)
from .sender import send_email, wrap_in_html
from .verifier import is_blocked_domain, is_valid_format, verify


BASE_DIR = Path(__file__).resolve().parent.parent
CONTACTS_FILE = config.CONTACTS_FILE
FRONTEND_DIR = BASE_DIR / "frontend"
FRONTEND_FILE = FRONTEND_DIR / "index.html"

CONTACTS_FIELDNAMES = [
    "name",
    "email",
    "phone",
    "company_name",
    "target_role",
    "target_industry",
    "details",
]

app = FastAPI(title="Email Automation API", version="1.0.0")
app.mount("/frontend", StaticFiles(directory=FRONTEND_DIR), name="frontend")


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


class CompanyCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    services: list[str] = Field(..., min_length=1)
    value_proposition: str = Field(default="", max_length=500)
    tone: str = Field(default="friendly-professional", max_length=100)
    signature: str = Field(default="", max_length=1000)


def load_contacts() -> list[dict[str, str]]:
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
    new_contact["context"] = new_contact["details"]
    return new_contact


def remove_contact(email: str) -> None:
    email = email.strip().lower()
    contacts = load_contacts()
    remaining = [row for row in contacts if row.get("email", "").lower() != email]
    if len(remaining) == len(contacts):
        raise HTTPException(status_code=404, detail="Contact not found.")
    write_contacts(remaining)


def build_preview(contact: ContactRequest) -> dict[str, str]:
    try:
        from .llm import generate_email
    except ValueError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error

    try:
        generated = generate_email(
            contact.name.strip() or "there",
            contact.email.strip(),
            contact.context.strip() or contact.details.strip(),
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


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "email-automation",
        "contacts_loaded": len(load_contacts()),
        "llm_provider": config.LLM_PROVIDER,
        "llm_configured": config.LLM_API_KEY_CONFIGURED,
        "smtp_configured": bool(config.GMAIL_USER and config.GMAIL_APP_PASSWORD),
    }


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
    profiles = load_company_profiles()
    if company_name not in profiles:
        raise HTTPException(status_code=404, detail="Company profile not found.")
    del profiles[company_name]
    save_all_company_profiles(profiles)
    return {"message": f"Company '{company_name}' removed.", "companies": list(profiles.keys())}


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
    log_file = config.LOG_FILE
    log_file.parent.mkdir(parents=True, exist_ok=True)
    file_exists = log_file.exists() and log_file.stat().st_size > 0
    with log_file.open("a", newline="", encoding="utf-8") as log:
        writer = csv.writer(log)
        if not file_exists:
            writer.writerow(["timestamp", "email", "status", "subject"])
        writer.writerow([datetime.now().isoformat(), email, status, subject])


def enforce_spam_threshold(body: str) -> None:
    try:
        from .llm import check_spam_score
    except ValueError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error

    try:
        spam = check_spam_score(body)
    except Exception as error:
        raise HTTPException(status_code=502, detail=f"Spam check failed: {error}") from error

    if spam["score"] >= 7:
        raise HTTPException(
            status_code=422,
            detail=f"Email blocked because its spam score is {spam['score']}/10: {spam['issues']}",
        )


@app.post("/api/send")
def send_contact(message: SendRequest) -> dict[str, Any]:
    email = message.email.strip()
    if not is_valid_format(email):
        raise HTTPException(status_code=422, detail="Please provide a valid email address.")
    if is_blocked_domain(email):
        raise HTTPException(status_code=422, detail="Disposable email domains are not allowed.")

    enforce_spam_threshold(message.body)
    html = wrap_in_html(message.body, config.SENDER_NAME)
    success = send_email(email, message.subject.strip(), html, message.body)
    log_send(email, "sent" if success else "failed", message.subject.strip())
    if not success:
        raise HTTPException(status_code=502, detail="Email could not be sent. Check SMTP settings.")
    return {"message": f"Email sent to {email}.", "email": email, "subject": message.subject.strip()}


@app.post("/api/verify")
def verify_contact(contact: ContactRequest, check_dns: bool = False) -> dict[str, Any]:
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
def preview(contact: ContactRequest) -> dict[str, Any]:
    if not is_valid_format(contact.email.strip()):
        raise HTTPException(status_code=422, detail="Please provide a valid email address.")
    return build_preview(contact)


@app.post("/pitch/generate")
@app.post("/api/pitch/generate")
def generate_pitch(request: PitchGenerateRequest) -> dict[str, Any]:
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
def send_pitch(request: PitchSendRequest) -> dict[str, Any]:
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
    if request.spam_score >= 7:
        raise HTTPException(
            status_code=422,
            detail=f"Pitch blocked because its spam score is {request.spam_score}/10.",
        )

    sender_name = company_name or config.SENDER_NAME
    html = wrap_in_html(body, sender_name)
    success = send_email(recipient_email, subject, html, body)
    log_send(recipient_email, "pitch:sent" if success else "pitch:failed", subject)

    if success:
        return {
            "status": "sent",
            "message": f"Pitch email successfully sent to {recipient_email}.",
        }
    else:
        return {
            "status": "failed",
            "message": "Pitch email delivery failed. Please check SMTP settings.",
        }
