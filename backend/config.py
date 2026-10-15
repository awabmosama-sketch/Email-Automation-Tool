import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

# Gmail SMTP settings
GMAIL_USER = os.getenv("GMAIL_USER")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")
SENDER_NAME = os.getenv("SENDER_NAME", "Team")

# Sending limits
DAILY_LIMIT = int(os.getenv("DAILY_LIMIT", 5))
DELAY_BETWEEN_EMAILS = 15  # seconds — warmup ke liye

# SMTP server
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465  # SSL port

# File paths
CONTACTS_FILE = BASE_DIR / "data" / "contacts.csv"
LOG_FILE = BASE_DIR / "data" / "sent_log.csv"
COMPANIES_FILE = BASE_DIR / "data" / "companies.json"


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