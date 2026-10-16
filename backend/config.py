import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

# Gmail SMTP settings
GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
SENDER_NAME = os.getenv("SENDER_NAME", "Team")
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "groq").strip().lower()
LLM_API_KEY_CONFIGURED = bool(
    os.getenv("ANTHROPIC_API_KEY") if LLM_PROVIDER == "anthropic" else os.getenv("GROQ_API_KEY")
)
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

# Sending limits
DAILY_LIMIT = int(os.getenv("DAILY_LIMIT", 5))
DELAY_BETWEEN_EMAILS = 15  # seconds — warmup ke liye

# SMTP server
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465  # SSL port

# Vercel's project directory is read-only. /tmp is writable but ephemeral;
# use DATA_DIR for the optional local file fallback or external storage adapter.
DEFAULT_DATA_DIR = Path("/tmp/email-automation-data") if os.getenv("VERCEL") else BASE_DIR / ".local-data"
DATA_DIR = Path(os.getenv("DATA_DIR", DEFAULT_DATA_DIR))
CONTACTS_FILE = Path(os.getenv("CONTACTS_FILE", DATA_DIR / "contacts.csv"))
LOG_FILE = Path(os.getenv("LOG_FILE", DATA_DIR / "sent_log.csv"))
COMPANIES_FILE = Path(os.getenv("COMPANIES_FILE", DATA_DIR / "companies.json"))


def validate_config():
    """Check karta hai ke .env sahi se load hui hai ya nahi."""
    missing = []
    if not GMAIL_USER:
        missing.append("GMAIL_USER")
    if not GMAIL_APP_PASSWORD:
        missing.append("GMAIL_APP_PASSWORD")

    if missing:
        raise ValueError(
            f"❌ Missing in .env: {', '.join(missing)}. "
            "Please check your .env file."
        )
    print(f"✅ Config loaded. Sender: {GMAIL_USER}")