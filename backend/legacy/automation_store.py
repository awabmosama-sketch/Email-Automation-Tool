"""Durable JSON-document store for campaign automation.

The rest of the platform already supports Neon PostgreSQL with a local file
fallback. Campaign objects (senders, brands, templates, campaigns, leads, jobs,
events) use the same pattern here:

* PostgreSQL: one ``automation_docs`` table with a JSONB document per row. This
  gives us atomic job claims with ``FOR UPDATE SKIP LOCKED`` without locking the
  whole application into a schema migration for every new field.
* Local development: one JSON file per collection under ``DATA_DIR``.

Only this module knows where campaign data lives; callers use the small CRUD
and queue helpers below.
"""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import psycopg
from psycopg.types.json import Jsonb

from .. import config, database

_LOCK = threading.RLock()
_FIELD_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
VALID_COLLECTIONS = {
    "senders",
    "brands",
    "templates",
    "campaigns",
    "leads",
    "jobs",
    "events",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    import secrets

    return f"{prefix}_{secrets.token_hex(8)}"


def _check_collection(collection: str) -> str:
    if collection not in VALID_COLLECTIONS:
        raise ValueError(f"Unknown automation collection: {collection}")
    return collection


def _check_field(field: str) -> str:
    if not _FIELD_PATTERN.fullmatch(field or ""):
        raise ValueError(f"Invalid document field: {field!r}")
    return field


def _local_path(collection: str) -> Path:
    return Path(config.DATA_DIR) / f"automation_{collection}.json"


def _read_local(collection: str) -> list[dict[str, Any]]:
    path = _local_path(collection)
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def _write_local(collection: str, docs: Iterable[dict[str, Any]]) -> None:
    path = _local_path(collection)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(list(docs), ensure_ascii=False, indent=2)
    # Write to a sibling temp file first so a crash cannot truncate the store.
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=path.name + ".",
        suffix=".tmp",
        delete=False,
    ) as handle:
        handle.write(payload)
        temp_name = handle.name
    os.replace(temp_name, path)


def _db_where(
    collection: str,
    equals: dict[str, Any] | None,
    gte: dict[str, Any] | None,
    lte: dict[str, Any] | None,
) -> tuple[str, list[Any]]:
    clauses = ["collection = %s"]
    params: list[Any] = [collection]
    for field, value in (equals or {}).items():
        _check_field(field)
        clauses.append(f"doc ->> '{field}' = %s")
        params.append(str(value))
    for field, value in (gte or {}).items():
        _check_field(field)
        clauses.append(f"doc ->> '{field}' >= %s")
        params.append(str(value))
    for field, value in (lte or {}).items():
        _check_field(field)
        clauses.append(f"doc ->> '{field}' <= %s")
        params.append(str(value))
    return " AND ".join(clauses), params


def list_docs(collection: str) -> list[dict[str, Any]]:
    collection = _check_collection(collection)
    if database.is_configured():
        with database._connect() as connection:
            rows = connection.execute(
                "SELECT doc FROM automation_docs WHERE collection = %s ORDER BY updated_at, id",
                (collection,),
            ).fetchall()
        return [copy.deepcopy(row["doc"]) for row in rows]
    with _LOCK:
        return copy.deepcopy(_read_local(collection))


def get_doc(collection: str, doc_id: str) -> dict[str, Any] | None:
    collection = _check_collection(collection)
    if database.is_configured():
        with database._connect() as connection:
            row = connection.execute(
                "SELECT doc FROM automation_docs WHERE collection = %s AND id = %s",
                (collection, doc_id),
            ).fetchone()
        return copy.deepcopy(row["doc"]) if row else None
    with _LOCK:
        return next((copy.deepcopy(item) for item in _read_local(collection) if item.get("id") == doc_id), None)


def query_docs(
    collection: str,
    *,
    equals: dict[str, Any] | None = None,
    gte: dict[str, Any] | None = None,
    lte: dict[str, Any] | None = None,
    order_by: str | None = None,
    descending: bool = False,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    collection = _check_collection(collection)
    if database.is_configured():
        where, params = _db_where(collection, equals, gte, lte)
        sql = f"SELECT doc FROM automation_docs WHERE {where}"
        if order_by:
            _check_field(order_by)
            direction = "DESC" if descending else "ASC"
            sql += f" ORDER BY (doc ->> '{order_by}') {direction} NULLS LAST, id"
        if limit:
            sql += " LIMIT %s"
            params.append(max(1, int(limit)))
        with database._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [copy.deepcopy(row["doc"]) for row in rows]

    with _LOCK:
        docs = _read_local(collection)
        result = []
        for item in docs:
            if equals and any(str(item.get(key, "")) != str(value) for key, value in equals.items()):
                continue
            if gte and any(str(item.get(key, "")) < str(value) for key, value in gte.items()):
                continue
            if lte and any(str(item.get(key, "")) > str(value) for key, value in lte.items()):
                continue
            result.append(copy.deepcopy(item))
        if order_by:
            result.sort(key=lambda item: str(item.get(order_by) or ""), reverse=descending)
        if limit:
            result = result[: max(1, int(limit))]
        return result


def count_docs(
    collection: str,
    *,
    equals: dict[str, Any] | None = None,
    gte: dict[str, Any] | None = None,
    lte: dict[str, Any] | None = None,
) -> int:
    collection = _check_collection(collection)
    if database.is_configured():
        where, params = _db_where(collection, equals, gte, lte)
        with database._connect() as connection:
            row = connection.execute(
                f"SELECT COUNT(*) AS count FROM automation_docs WHERE {where}", params
            ).fetchone()
        return int(row["count"]) if row else 0
    return len(query_docs(collection, equals=equals, gte=gte, lte=lte))


def upsert_doc(collection: str, doc: dict[str, Any]) -> dict[str, Any]:
    collection = _check_collection(collection)
    return upsert_docs(collection, [doc])[0]


def upsert_docs(collection: str, docs: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    collection = _check_collection(collection)
    prepared: list[dict[str, Any]] = []
    stamp = now_iso()
    for doc in docs:
        item = copy.deepcopy(doc)
        if not item.get("id"):
            item["id"] = new_id(collection[:-1] if collection.endswith("s") else collection)
        item.setdefault("created_at", stamp)
        item["updated_at"] = stamp
        prepared.append(item)

    if not prepared:
        return []

    if database.is_configured():
        with database._connect() as connection:
            for item in prepared:
                connection.execute(
                    """
                    INSERT INTO automation_docs (collection, id, doc)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (collection, id)
                    DO UPDATE SET doc = EXCLUDED.doc, updated_at = NOW()
                    """,
                    (collection, item["id"], Jsonb(item)),
                )
        return prepared

    with _LOCK:
        existing = _read_local(collection)
        index = {item.get("id"): position for position, item in enumerate(existing)}
        for item in prepared:
            position = index.get(item["id"])
            if position is None:
                index[item["id"]] = len(existing)
                existing.append(item)
            else:
                existing[position] = item
        _write_local(collection, existing)
    return prepared


def delete_doc(collection: str, doc_id: str) -> bool:
    collection = _check_collection(collection)
    if database.is_configured():
        with database._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM automation_docs WHERE collection = %s AND id = %s",
                (collection, doc_id),
            )
            return cursor.rowcount == 1
    with _LOCK:
        docs = _read_local(collection)
        remaining = [item for item in docs if item.get("id") != doc_id]
        if len(remaining) == len(docs):
            return False
        _write_local(collection, remaining)
        return True


def delete_docs(collection: str, equals: dict[str, Any]) -> int:
    collection = _check_collection(collection)
    if database.is_configured():
        where, params = _db_where(collection, equals, None, None)
        with database._connect() as connection:
            cursor = connection.execute(
                f"DELETE FROM automation_docs WHERE {where}", params
            )
            return cursor.rowcount or 0
    with _LOCK:
        docs = _read_local(collection)
        remaining = [
            item
            for item in docs
            if not all(str(item.get(key, "")) == str(value) for key, value in equals.items())
        ]
        removed = len(docs) - len(remaining)
        if removed:
            _write_local(collection, remaining)
        return removed


def claim_due_job(campaign_id: str | None = None, now: str | None = None) -> dict[str, Any] | None:
    """Atomically claims one due queue job and marks it running."""
    stamp = now or now_iso()
    if database.is_configured():
        campaign_filter = campaign_id or ""
        with database._connect() as connection:
            row = connection.execute(
                """
                UPDATE automation_docs AS d
                SET doc = jsonb_set(
                              jsonb_set(d.doc, '{status}', '"running"'),
                              '{started_at}', to_jsonb(%s::text)
                          )
                          || jsonb_build_object(
                              'attempts',
                              COALESCE((d.doc ->> 'attempts')::int, 0) + 1
                          ),
                    updated_at = NOW()
                FROM (
                    SELECT id
                    FROM automation_docs
                    WHERE collection = 'jobs'
                      AND doc ->> 'status' = 'queued'
                      AND doc ->> 'scheduled_at' <= %s
                      AND (%s = '' OR doc ->> 'campaign_id' = %s)
                    ORDER BY doc ->> 'scheduled_at'
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                ) AS candidate
                WHERE d.collection = 'jobs' AND d.id = candidate.id
                RETURNING d.doc
                """,
                (stamp, stamp, campaign_filter, campaign_filter),
            ).fetchone()
        return copy.deepcopy(row["doc"]) if row else None

    with _LOCK:
        docs = _read_local("jobs")
        due = [
            item
            for item in docs
            if item.get("status") == "queued"
            and str(item.get("scheduled_at") or "") <= stamp
            and (not campaign_id or item.get("campaign_id") == campaign_id)
        ]
        if not due:
            return None
        due.sort(key=lambda item: str(item.get("scheduled_at") or ""))
        chosen = due[0]
        chosen["status"] = "running"
        chosen["started_at"] = stamp
        chosen["attempts"] = int(chosen.get("attempts") or 0) + 1
        chosen["updated_at"] = stamp
        _write_local("jobs", docs)
        return copy.deepcopy(chosen)


def recover_stale_jobs(timeout_seconds: int = 900, max_attempts: int = 3) -> int:
    """Requeues or fails jobs left running by a crashed worker."""
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds)).isoformat()
    running = query_docs("jobs", equals={"status": "running"})
    recovered = 0
    for job in running:
        started = str(job.get("started_at") or "")
        if started and started >= cutoff:
            continue
        attempts = int(job.get("attempts") or 0)
        if attempts >= max_attempts:
            job["status"] = "failed"
            job["error"] = job.get("error") or "Worker stopped repeatedly before finishing this job."
        else:
            job["status"] = "queued"
            job["scheduled_at"] = now_iso()
            job["error"] = "Requeued after a worker timeout."
        upsert_doc("jobs", job)
        recovered += 1
    return recovered


def clear_collection(collection: str) -> None:
    """Test/development helper: removes every document in a collection."""
    for doc in list_docs(collection):
        delete_doc(collection, doc["id"])


def pool_health() -> dict[str, Any]:
    return {
        "backend": "postgres" if database.is_configured() else "json",
        "data_dir": str(config.DATA_DIR),
    }


__all__ = [
    "claim_due_job",
    "clear_collection",
    "count_docs",
    "delete_doc",
    "delete_docs",
    "get_doc",
    "list_docs",
    "new_id",
    "now_iso",
    "pool_health",
    "query_docs",
    "recover_stale_jobs",
    "upsert_doc",
    "upsert_docs",
]


# psycopg is imported for the type checker / connection helpers above; keep the
# reference explicit so linters do not flag it as unused if query code moves.
_ = psycopg
