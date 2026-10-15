import csv
import time
from datetime import datetime
from .sender import send_email, wrap_in_html
from .verifier import verify
from .llm import generate_email, check_spam_score
from . import config


def load_contacts():
    with open(config.CONTACTS_FILE, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def ensure_log_file():
    import os
    if not os.path.exists(config.LOG_FILE):
        with open(config.LOG_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["timestamp", "email", "status", "subject"])


def log_send(email, status, subject=""):
    with open(config.LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([datetime.now().isoformat(), email, status, subject])


def main():
    config.validate_config()
    ensure_log_file()

    contacts = load_contacts()
    print("\nLoaded " + str(len(contacts)) + " contacts")
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
        success = send_email(email, subject, html, body)

        if success:
            print("    SENT -> " + email)
            log_send(email, "sent", subject)
            sent += 1
        else:
            print("    FAILED -> " + email)
            log_send(email, "failed", subject)
            failed += 1

        if i < len(contacts) and sent < config.DAILY_LIMIT:
            time.sleep(config.DELAY_BETWEEN_EMAILS)

    print("\n" + "=" * 45)
    print("Sent: " + str(sent) + "  |  Skipped: " + str(skipped) + "  |  Failed: " + str(failed))
    print("Log: " + config.LOG_FILE)
    print("=" * 45)


if __name__ == "__main__":
    main()