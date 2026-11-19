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
SMTP_PROVIDER = (os.getenv("SMTP_PROVIDER", "zoho").strip().lower() or "zoho")

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

# Raw values without the legacy Gmail fallback, so selecting Zoho can never
# pick up Gmail credentials by mistake.
SMTP_USER_ENV = os.getenv("SMTP_USER", "").strip()
SMTP_PASSWORD_ENV = os.getenv("SMTP_PASSWORD", "").strip()

SMTP_USER = SMTP_USER_ENV or (
    (os.getenv("GMAIL_USER") or "").strip() if _legacy_gmail else ""
)
SMTP_PASSWORD = SMTP_PASSWORD_ENV or (
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
# Public origin used for unsubscribe links in outgoing email. Derived from the
# request when unset; set it on serverless deployments if links look wrong.
APP_BASE_URL = os.getenv("APP_BASE_URL", "").strip().rstrip("/")
# Selector shown in the mail provider's DKIM settings (e.g. "zoho").
DKIM_SELECTOR = os.getenv("DKIM_SELECTOR", "").strip().lower()
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "groq").strip().lower()
LLM_API_KEY_CONFIGURED = bool(
    os.getenv("ANTHROPIC_API_KEY") if LLM_PROVIDER == "anthropic" else os.getenv("GROQ_API_KEY")
)
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

# Deployment environment. Anything that is not explicitly a development/test
# environment is treated as production and must supply its own secrets.
def _app_environment() -> str:
    return (
        os.getenv("APP_ENV", "").strip()
        or os.getenv("ENVIRONMENT", "").strip()
        or os.getenv("VERCEL_ENV", "").strip()
        or ("production" if os.getenv("VERCEL") else "development")
    ).lower()


APP_ENV = _app_environment()
IS_DEVELOPMENT = APP_ENV in {"development", "dev", "local", "test", "testing"}

# Local development may run with login disabled (AUTH_DISABLED or no password);
# a real deployment must never start on built-in credentials.
APP_EMAIL = os.getenv("APP_EMAIL", "").strip().lower()
APP_PASSWORD = os.getenv("APP_PASSWORD", "").strip()

# Used to sign session cookies and as the fallback Fernet key for stored sender
# credentials, so a missing value would both forge sessions and decrypt secrets.
_session_secret_env = os.getenv("SESSION_SECRET", "").strip()

if not IS_DEVELOPMENT:
    _missing_secrets = [
        name
        for name, value in (("APP_PASSWORD", APP_PASSWORD), ("SESSION_SECRET", _session_secret_env))
        if not value
    ]
    if _missing_secrets:
        raise RuntimeError(
            "Missing required secret(s) for a non-development environment "
            f"({APP_ENV}): {', '.join(_missing_secrets)}. "
            "Set them as environment variables; built-in defaults are not allowed outside development."
        )

SESSION_SECRET = _session_secret_env or "dev-only-session-secret-do-not-deploy"


def auth_enabled() -> bool:
    # Allow explicit disabling for local development if needed
    if os.getenv("AUTH_DISABLED", "").strip().lower() in {"1", "true", "yes", "on"}:
        return False
    # On Vercel/serverless, enforce auth by default for security
    if os.getenv("VERCEL"):
        return True
    return bool(APP_EMAIL and APP_PASSWORD)

def _env_int(name: str, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        value = int(os.getenv(name, "").strip() or default)
    except ValueError:
        value = default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# Sending limits. Never drop below 15 sends a day, even if an older .env
# or deployment still carries the previous 5/day value.
DAILY_LIMIT = max(15, int(os.getenv("DAILY_LIMIT", "15").strip() or "15"))
DELAY_BETWEEN_EMAILS = 15  # seconds — warmup ke liye

# ---------------------------------------------------------------------------
# Campaign queue
#
# The queue is durable (PostgreSQL JSONB when DATABASE_URL is set, otherwise a
# local JSON file) so it works on serverless deployments where a Celery/Redis
# worker cannot stay alive. Long-lived processes run a worker thread; on Vercel
# an external scheduler calls POST /api/queue/tick with CRON_SECRET.
# ---------------------------------------------------------------------------
QUEUE_WORKER_ENABLED = _env_bool("QUEUE_WORKER_ENABLED", not bool(os.getenv("VERCEL")))
QUEUE_TICK_SECONDS = _env_int("QUEUE_TICK_SECONDS", 30, minimum=5, maximum=3600)
QUEUE_MAX_PER_TICK = _env_int("QUEUE_MAX_PER_TICK", 10, minimum=1, maximum=100)
CRON_SECRET = os.getenv("CRON_SECRET", "").strip()
QUEUE_TICK_SECRET = os.getenv("QUEUE_TICK_SECRET", "").strip() or CRON_SECRET

# Campaign defaults: 5-10/hour and 30-50/day are the recommended warm-up
# ranges. Randomized 4-12 minute gaps make sending look human.
DEFAULT_HOURLY_LIMIT = _env_int(
    "DEFAULT_HOURLY_LIMIT", _env_int("CAMPAIGN_HOURLY_LIMIT", 8, 1, 120), minimum=1, maximum=120
)
CAMPAIGN_DEFAULT_HOURLY_LIMIT = DEFAULT_HOURLY_LIMIT
CAMPAIGN_DEFAULT_DAILY_LIMIT = _env_int(
    "CAMPAIGN_DAILY_LIMIT", _env_int("DEFAULT_DAILY_LIMIT", 40, 1, 500), minimum=1, maximum=500
)
DEFAULT_MIN_DELAY_MINUTES = _env_int("DEFAULT_MIN_DELAY_MINUTES", 4, minimum=1, maximum=240)
DEFAULT_MAX_DELAY_MINUTES = _env_int("DEFAULT_MAX_DELAY_MINUTES", 12, minimum=1, maximum=240)
CAMPAIGN_DEFAULT_DELAY_MIN = max(
    1, _env_int("CAMPAIGN_DELAY_MIN_MINUTES", DEFAULT_MIN_DELAY_MINUTES, 1, 240)
)
CAMPAIGN_DEFAULT_DELAY_MAX = max(
    CAMPAIGN_DEFAULT_DELAY_MIN,
    _env_int("CAMPAIGN_DELAY_MAX_MINUTES", DEFAULT_MAX_DELAY_MINUTES, 1, 240),
)
DEFAULT_WINDOW_START = _env_int("DEFAULT_WINDOW_START", 9, minimum=0, maximum=23)
DEFAULT_WINDOW_END = _env_int("DEFAULT_WINDOW_END", 18, minimum=1, maximum=24)
# Minutes east of UTC. 300 = PKT (UTC+5), 0 = UTC, -300 = EST.
DEFAULT_TIMEZONE_OFFSET_MINUTES = _env_int(
    "TIMEZONE_OFFSET_MINUTES", 300, minimum=-720, maximum=840
)
CAMPAIGN_TIMEZONE = os.getenv("CAMPAIGN_TIMEZONE", "UTC").strip() or "UTC"

# Google Sheets ingestion. Link-shared sheets are read through the public CSV
# export endpoint. An API key enables the Sheets API and tab metadata lookup;
# a service account can read sheets shared with its client email.
GOOGLE_SHEETS_API_KEY = (
    os.getenv("GOOGLE_SHEETS_API_KEY", "").strip() or os.getenv("GOOGLE_API_KEY", "").strip()
)
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
GOOGLE_SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
GOOGLE_SHEETS_TIMEOUT = 20

# Key material used to encrypt stored SMTP/IMAP/OAuth credentials at rest.
# Falls back to SESSION_SECRET so a deployment needs no extra variable, but a
# dedicated value is safer.
SENDER_CREDENTIAL_KEY = os.getenv("SENDER_CREDENTIAL_KEY", "").strip()
# Comma-separated prior secrets, used only to decrypt (and then re-encrypt)
# credentials written before the current key existed. Set for one deploy, watch
# the startup warning drain to zero, then unset.
SENDER_CREDENTIAL_KEY_LEGACY = os.getenv("SENDER_CREDENTIAL_KEY_LEGACY", "").strip()

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
SUPPRESSIONS_FILE = Path(os.getenv("SUPPRESSIONS_FILE", DATA_DIR / "suppressions.csv"))
CAMPAIGNS_FILE = Path(os.getenv("CAMPAIGNS_FILE", DATA_DIR / "campaigns.json"))
OUTREACH_FILE = Path(os.getenv("OUTREACH_FILE", DATA_DIR / "outreach.json"))


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