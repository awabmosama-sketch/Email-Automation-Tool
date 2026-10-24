import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

# ---------------------------------------------------------------------------
# SMTP delivery settings
#
# SMTP_PROVIDER picks sensible defaults for the host, port, and security mode:
#   gmail  -> smtp.gmail.com:465 (SSL)
#   zoho   -> smtp.zoho.com:465 (SSL)
#   custom -> no defaults; SMTP_HOST, SMTP_PORT, and SMTP_SECURITY are required
#
# SMTP_USER / SMTP_PASSWORD are the credentials used to sign in and the address
# that appears in the From header. Legacy GMAIL_USER / GMAIL_APP_PASSWORD are
# still honoured, but only while SMTP_PROVIDER=gmail.
# ---------------------------------------------------------------------------
SMTP_PROVIDER = (os.getenv("SMTP_PROVIDER", "gmail").strip().lower() or "gmail")

_SMTP_PROVIDER_DEFAULTS = {
    "gmail": ("smtp.gmail.com", 465, "ssl"),
    "zoho": ("smtp.zoho.com", 465, "ssl"),
    "custom": ("", 587, "starttls"),
}
if SMTP_PROVIDER not in _SMTP_PROVIDER_DEFAULTS:
    SMTP_PROVIDER = "custom"

_DEFAULT_SMTP_HOST, _DEFAULT_SMTP_PORT, _DEFAULT_SMTP_SECURITY = _SMTP_PROVIDER_DEFAULTS[SMTP_PROVIDER]

# Backwards compatibility: the old Gmail-only variables keep working.
_legacy_gmail = SMTP_PROVIDER == "gmail"

SMTP_USER = os.getenv("SMTP_USER", "").strip() or (
    (os.getenv("GMAIL_USER") or "").strip() if _legacy_gmail else ""
)
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "").strip() or (
    (os.getenv("GMAIL_APP_PASSWORD") or "").strip() if _legacy_gmail else ""
)

# Kept for code that still refers to the legacy names.
GMAIL_USER = os.getenv("GMAIL_USER") or None
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD") or None

SMTP_HOST = os.getenv("SMTP_HOST", "").strip() or _DEFAULT_SMTP_HOST
SMTP_SECURITY = (os.getenv("SMTP_SECURITY", "").strip().lower() or _DEFAULT_SMTP_SECURITY)
if SMTP_SECURITY not in {"ssl", "starttls"}:
    SMTP_SECURITY = _DEFAULT_SMTP_SECURITY

try:
    SMTP_PORT = int(os.getenv("SMTP_PORT", "").strip() or _DEFAULT_SMTP_PORT)
except ValueError:
    SMTP_PORT = _DEFAULT_SMTP_PORT

SENDER_NAME = os.getenv("SENDER_NAME", "Team")
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "groq").strip().lower()
LLM_API_KEY_CONFIGURED = bool(
    os.getenv("ANTHROPIC_API_KEY") if LLM_PROVIDER == "anthropic" else os.getenv("GROQ_API_KEY")
)
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

# Dashboard sign-in. Auth is enforced when both APP_EMAIL and APP_PASSWORD are set.
APP_EMAIL = os.getenv("APP_EMAIL", "").strip().lower()
APP_PASSWORD = os.getenv("APP_PASSWORD", "").strip()
# Used to sign session cookies; falls back to the password when unset.
SESSION_SECRET = os.getenv("SESSION_SECRET", "").strip()


def auth_enabled() -> bool:
    # Allow explicit disabling for local development if needed
    if os.getenv("AUTH_DISABLED", "").strip().lower() in {"1", "true", "yes", "on"}:
        return False
    # On Vercel/serverless, enforce auth by default for security
    if os.getenv("VERCEL"):
        return True
    return bool(APP_EMAIL and APP_PASSWORD)

# Sending limits
DAILY_LIMIT = int(os.getenv("DAILY_LIMIT", "5"))
DELAY_BETWEEN_EMAILS = 15  # seconds — warmup ke liye

# SMTP server (resolved above from SMTP_PROVIDER + optional overrides)


def smtp_configured() -> bool:
    """True when the active provider has enough settings to attempt a send."""
    return bool(SMTP_HOST and SMTP_USER and SMTP_PASSWORD)

# Vercel's project directory is read-only. /tmp is writable but ephemeral;
# use DATA_DIR for the optional local file fallback or external storage adapter.
DEFAULT_DATA_DIR = Path("/tmp/email-automation-data") if os.getenv("VERCEL") else BASE_DIR / ".local-data"
DATA_DIR = Path(os.getenv("DATA_DIR", DEFAULT_DATA_DIR))
CONTACTS_FILE = Path(os.getenv("CONTACTS_FILE", DATA_DIR / "contacts.csv"))
LOG_FILE = Path(os.getenv("LOG_FILE", DATA_DIR / "sent_log.csv"))
COMPANIES_FILE = Path(os.getenv("COMPANIES_FILE", DATA_DIR / "companies.json"))
DOCUMENTS_FILE = Path(os.getenv("DOCUMENTS_FILE", DATA_DIR / "documents.json"))


def validate_config():
    """Check karta hai ke .env sahi se load hui hai ya nahi."""
    missing = []
    if not SMTP_HOST:
        missing.append("SMTP_HOST")
    if not SMTP_USER:
        missing.append("SMTP_USER" if SMTP_PROVIDER != "gmail" else "SMTP_USER (or GMAIL_USER)")
    if not SMTP_PASSWORD:
        missing.append("SMTP_PASSWORD" if SMTP_PROVIDER != "gmail" else "SMTP_PASSWORD (or GMAIL_APP_PASSWORD)")

    if missing:
        raise ValueError(
            f"❌ Missing in .env: {', '.join(missing)}. "
            "Please check your .env file."
        )
    print(f"✅ Config loaded. SMTP provider: {SMTP_PROVIDER}. Sender: {SMTP_USER}")