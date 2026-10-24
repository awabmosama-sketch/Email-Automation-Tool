import csv
import time
from datetime import datetime

from . import config, database
from .llm import check_spam_score, generate_email
from .sender import send_email, wrap_in_html
from .verifier import verify


def load_contacts() -> list[dict[str, str]]:
    """Uses Neon when configured, otherwise the local CSV fallback."""
    if database.is_configured():
        return database.load_contacts()
    if not config.CONTACTS_FILE.exists():
        print("No contacts file found at " + str(config.CONTACTS_FILE))
        return []
    with open(config.CONTACTS_FILE, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def ensure_log_file() -> None:
    if database.is_configured():
        return
    config.LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not config.LOG_FILE.exists():
        with open(config.LOG_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["timestamp", "email", "status", "subject"])


def log_send(email: str, status: str, subject: str = "") -> None:
    if database.is_configured():
        database.log_send(email, status, subject)
        return
    config.LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(config.LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([datetime.now().isoformat(), email, status, subject])


def main() -> None:
    config.validate_config()
    ensure_log_file()

    contacts = load_contacts()
    print("\nLoaded " + str(len(contacts)) + " contacts")
    print("Storage: " + ("Neon PostgreSQL" if database.is_configured() else str(config.CONTACTS_FILE)))
    print("Daily limit: " + str(config.DAILY_LIMIT))
    print("Delay: " + str(config.DELAY_BETWEEN_EMAILS) + "s\n")

    sent = 0
    skipped = 0
    failed = 0

    for i, contact in enumerate(contacts, start=1):
        if sent >= config.DAILY_LIMIT:
            print("\nDaily limit reached. Stopping.")
            break

        email = contact.get("email", "").strip()
        name = contact.get("name", "there").strip()
        context = contact.get("context", "").strip() or contact.get("details", "").strip()

        if not email:
            continue

        ok, reason = verify(email)
        if not ok:
            print("[" + str(i) + "] SKIP " + email + " -> " + reason)
            log_send(email, "invalid:" + reason)
            skipped += 1
            continue

        print("[" + str(i) + "] Generating email for " + name + "...")
        result = generate_email(name, email, context)
        subject = result["subject"]
        body = result["body"]

        spam = check_spam_score(body)
        print("    spam score: " + str(spam["score"]) + "/10 - " + spam["issues"])

        if spam["score"] >= 7:
            print("    too spammy, skipping")
            log_send(email, "spam_skip", subject)
            skipped += 1
            continue

        html = wrap_in_html(body, config.SENDER_NAME)
        success, send_error = send_email(email, subject, html, body)

        if success:
            print("    SENT -> " + email)
            log_send(email, "sent", subject)
            sent += 1
        else:
            print("    FAILED -> " + email + " (" + send_error + ")")
            log_send(email, "failed", subject)
            failed += 1

        if i < len(contacts) and sent < config.DAILY_LIMIT:
            time.sleep(config.DELAY_BETWEEN_EMAILS)

    print("\n" + "=" * 45)
    print("Sent: " + str(sent) + "  |  Skipped: " + str(skipped) + "  |  Failed: " + str(failed))
    print("Log: " + ("Neon sent_log table" if database.is_configured() else str(config.LOG_FILE)))
    print("=" * 45)


if __name__ == "__main__":
    main()
