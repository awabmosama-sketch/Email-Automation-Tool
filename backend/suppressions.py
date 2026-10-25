import csv
import threading
from datetime import datetime, timezone

from . import config, database

FIELDNAMES = ["email", "reason", "created_at"]
_lock = threading.Lock()


def _normalize(email: str) -> str:
    return (email or "").strip().lower()


def _load_local() -> list[dict[str, str]]:
    if not config.SUPPRESSIONS_FILE.exists():
        return []
    with config.SUPPRESSIONS_FILE.open(newline="", encoding="utf-8") as file:
        return [
            {field: (row.get(field) or "").strip() for field in FIELDNAMES}
            for row in csv.DictReader(file)
        ]


def is_suppressed(email: str) -> bool:
    email = _normalize(email)
    if not email:
        return False
    if database.is_configured():
        return database.is_suppressed(email)
    with _lock:
        return any(row["email"] == email for row in _load_local())


def add(email: str, reason: str = "unsubscribe") -> None:
    email = _normalize(email)
    if not email:
        return
    if database.is_configured():
        database.add_suppression(email, reason)
        return

    with _lock:
        rows = _load_local()
        if any(row["email"] == email for row in rows):
            return
        config.SUPPRESSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        needs_header = not config.SUPPRESSIONS_FILE.exists() or config.SUPPRESSIONS_FILE.stat().st_size == 0
        with config.SUPPRESSIONS_FILE.open("a", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=FIELDNAMES)
            if needs_header:
                writer.writeheader()
            writer.writerow(
                {
                    "email": email,
                    "reason": reason,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            )
