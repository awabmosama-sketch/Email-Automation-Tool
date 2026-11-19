"""Generic JSON document store for the campaign workspace.

PostgreSQL is used when ``DATABASE_URL`` is configured; otherwise records are
kept in ``.local-data/campaigns.json``. Both backends expose the same small
API so campaign code never has to branch on storage.
"""

import json
import logging
import os
import secrets
import threading
from datetime import datetime, timezone
from typing import Any, Iterable

from . import config, database

logger = logging.getLogger(__name__)

COLLECTIONS = (
    "brand",
    "sender",
    "template",
    "campaign",
    "lead",
    "event",
    "meta",
)

_local_lock = threading.RLock()
_local_cache: dict[str, dict[str, dict[str, Any]]] | None = None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(value: datetime | None = None) -> str:
    return (value or utc_now()).astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


# ---------------------------------------------------------------------------
# Local fallback
# ---------------------------------------------------------------------------


def _empty_store() -> dict[str, dict[str, dict[str, Any]]]:
    return {collection: {} for collection in COLLECTIONS}


def _local_path():
    return config.CAMPAIGNS_FILE


def _load_local() -> dict[str, dict[str, dict[str, Any]]]:
    global _local_cache
    if _local_cache is not None:
        return _local_cache

    path = _local_path()
    store = _empty_store()
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as handle:
                raw = json.load(handle)
            if isinstance(raw, dict):
                for collection in COLLECTIONS:
                    values = raw.get(collection)
                    if isinstance(values, dict):
                        store[collection] = {
                            str(key): value for key, value in values.items() if isinstance(value, dict)
                        }
        except Exception:
            logger.exception("Could not read %s; starting with an empty campaign store.", path)
    _local_cache = store
    return store


def _save_local(store: dict[str, dict[str, dict[str, Any]]]) -> None:
    path = _local_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(store, handle, ensure_ascii=False, separators=(",", ":"))
    os.replace(temporary, path)


def _matches(record: dict[str, Any], filters: dict[str, Any] | None) -> bool:
    for key, value in (filters or {}).items():
        if value is None:
            continue
        if str(record.get(key, "")) != str(value):
            return False
    return True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def create(collection: str, data: dict[str, Any], prefix: str | None = None) -> dict[str, Any]:
    if collection not in COLLECTIONS:
        raise ValueError(f"Unknown collection: {collection}")
    record = dict(data)
    record.setdefault("id", new_id(prefix or collection[:3]))
    record.setdefault("created_at", to_iso())
    record["updated_at"] = to_iso()

    if database.is_configured():
        database.save_record(collection, record["id"], record)
        return record

    with _local_lock:
        store = _load_local()
        store[collection][record["id"]] = record
        _save_local(store)
    return record


def update(collection: str, record_id: str, changes: dict[str, Any]) -> dict[str, Any] | None:
    record = get(collection, record_id)
    if record is None:
        return None
    merged = {**record, **changes, "id": record_id, "updated_at": to_iso()}
    save(collection, merged)
    return merged


def save(collection: str, record: dict[str, Any]) -> dict[str, Any]:
    if not record.get("id"):
        raise ValueError("Record is missing an id.")
    if database.is_configured():
        database.save_record(collection, record["id"], record)
        return record
    with _local_lock:
        store = _load_local()
        store[collection][record["id"]] = record
        _save_local(store)
    return record


def save_many(collection: str, records: Iterable[dict[str, Any]]) -> None:
    items = [record for record in records if record.get("id")]
    if not items:
        return
    if database.is_configured():
        database.save_records(collection, items)
        return
    with _local_lock:
        store = _load_local()
        for record in items:
            store[collection][record["id"]] = record
        _save_local(store)


def get(collection: str, record_id: str) -> dict[str, Any] | None:
    if not record_id:
        return None
    if database.is_configured():
        return database.get_record(collection, record_id)
    with _local_lock:
        return _load_local()[collection].get(record_id)


def delete(collection: str, record_id: str) -> bool:
    if database.is_configured():
        return database.delete_record(collection, record_id)
    with _local_lock:
        store = _load_local()
        if record_id not in store[collection]:
            return False
        del store[collection][record_id]
        _save_local(store)
    return True


def delete_where(collection: str, filters: dict[str, Any] | None = None) -> int:
    if database.is_configured():
        return database.delete_records(collection, filters)
    with _local_lock:
        store = _load_local()
        doomed = [
            record_id
            for record_id, record in store[collection].items()
            if _matches(record, filters)
        ]
        for record_id in doomed:
            del store[collection][record_id]
        if doomed:
            _save_local(store)
    return len(doomed)


def list_all(
    collection: str,
    filters: dict[str, Any] | None = None,
    *,
    order_by: str | None = "created_at",
    descending: bool = False,
) -> list[dict[str, Any]]:
    if database.is_configured():
        records = database.list_records(collection, filters)
    else:
        with _local_lock:
            records = [
                dict(record)
                for record in _load_local()[collection].values()
                if _matches(record, filters)
            ]

    if order_by:
        records.sort(
            key=lambda record: (
                str(record.get(order_by) or ""),
                str(record.get("id") or ""),
            ),
            reverse=descending,
        )
    return records


def find_one(collection: str, filters: dict[str, Any] | None = None) -> dict[str, Any] | None:
    matches = list_all(collection, filters)
    return matches[0] if matches else None


def claim(
    collection: str,
    record_id: str,
    field: str,
    expected: str,
    new_value: str,
) -> dict[str, Any] | None:
    """Atomically transitions a record field when it still has ``expected``."""
    if database.is_configured():
        return database.claim_record_status(collection, record_id, field, expected, new_value)

    with _local_lock:
        store = _load_local()
        record = store[collection].get(record_id)
        if not record or str(record.get(field, "")) != expected:
            return None
        record = {**record, field: new_value, "updated_at": to_iso()}
        store[collection][record_id] = record
        _save_local(store)
    return record


def meta_get(key: str, default: str | None = None) -> str | None:
    record = get("meta", key)
    return str(record.get("value")) if record and record.get("value") is not None else default


def meta_set(key: str, value: str) -> None:
    create(
        "meta",
        {"id": key, "value": str(value), "created_at": to_iso(), "updated_at": to_iso()},
    )
