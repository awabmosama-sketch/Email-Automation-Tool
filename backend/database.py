import json
import os
import threading
from typing import Any, Callable

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from . import config


SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    email TEXT PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    phone VARCHAR(50) NOT NULL DEFAULT '',
    company_name VARCHAR(100) NOT NULL DEFAULT '',
    target_role VARCHAR(100) NOT NULL DEFAULT '',
    target_industry VARCHAR(100) NOT NULL DEFAULT '',
    details TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS company_profiles (
    name VARCHAR(100) PRIMARY KEY,
    profile JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS sent_log (
    id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    email TEXT NOT NULL,
    status TEXT NOT NULL,
    subject TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS app_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    owner_type VARCHAR(20) NOT NULL,
    owner_key TEXT NOT NULL,
    filename TEXT NOT NULL,
    mime_type VARCHAR(120) NOT NULL DEFAULT '',
    size_bytes INTEGER NOT NULL DEFAULT 0,
    content_text TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS documents_owner_idx
    ON documents (owner_type, owner_key);

CREATE TABLE IF NOT EXISTS suppressions (
    email TEXT PRIMARY KEY,
    reason TEXT NOT NULL DEFAULT 'unsubscribe',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Campaign automation (senders, brands, templates, campaigns, leads, jobs,
-- events) is stored as JSON documents. This keeps the schema flexible while
-- still allowing atomic queue claims and indexed lookups in PostgreSQL.
CREATE TABLE IF NOT EXISTS automation_docs (
    collection TEXT NOT NULL,
    id TEXT NOT NULL,
    doc JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (collection, id)
);

CREATE INDEX IF NOT EXISTS automation_docs_collection_idx
    ON automation_docs (collection);

CREATE INDEX IF NOT EXISTS automation_docs_doc_gin
    ON automation_docs USING GIN (doc jsonb_path_ops);

CREATE INDEX IF NOT EXISTS automation_docs_jobs_due_idx
    ON automation_docs ((doc ->> 'scheduled_at'))
    WHERE collection = 'jobs';

CREATE INDEX IF NOT EXISTS automation_docs_events_campaign_idx
    ON automation_docs ((doc ->> 'campaign_id'), (doc ->> 'created_at'))
    WHERE collection = 'events';

-- Generic document store for campaigns, senders, templates, leads, and
-- events. The JSONB payload keeps the PostgreSQL and local-file backends
-- interchangeable while the volume stays small (thousands of rows).
CREATE TABLE IF NOT EXISTS records (
    collection TEXT NOT NULL,
    id TEXT NOT NULL,
    data JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (collection, id)
);

CREATE INDEX IF NOT EXISTS records_collection_idx
    ON records (collection, created_at);
CREATE INDEX IF NOT EXISTS records_campaign_idx
    ON records (collection, (data ->> 'campaign_id'));
"""

_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()


def is_configured() -> bool:
    return bool(config.DATABASE_URL)


def _get_pool() -> ConnectionPool:
    """Builds the connection pool once per process and applies the schema once."""
    global _pool
    if _pool is not None:
        return _pool

    with _pool_lock:
        if _pool is not None:
            return _pool
        if not config.DATABASE_URL:
            raise RuntimeError("DATABASE_URL is not configured.")

        pool = ConnectionPool(
            conninfo=config.DATABASE_URL,
            min_size=0,
            max_size=max(1, int(os.getenv("DB_POOL_SIZE", "4"))),
            kwargs={"row_factory": dict_row},
            open=False,
        )
        try:
            pool.open(wait=True, timeout=15)
            with pool.connection() as connection:
                connection.execute(SCHEMA)
        except Exception:
            pool.close()
            raise
        _pool = pool
        return pool


def _connect():
    """Returns a pooled connection context manager (commits on clean exit)."""
    return _get_pool().connection()


def close_pool() -> None:
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.close()
            _pool = None


def load_contacts() -> list[dict[str, str]]:
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT name, email, phone, company_name, target_role,
                   target_industry, details
            FROM contacts
            ORDER BY created_at, email
            """
        ).fetchall()
    return [{key: str(row.get(key) or "") for key in (
        "name", "email", "phone", "company_name", "target_role",
        "target_industry", "details",
    )} for row in rows]


def seed_contacts(contacts: list[dict[str, str]]) -> None:
    if not contacts:
        return
    with _connect() as connection:
        for contact in contacts:
            connection.execute(
                """
                INSERT INTO contacts
                    (email, name, phone, company_name, target_role,
                     target_industry, details)
                VALUES (%(email)s, %(name)s, %(phone)s, %(company_name)s,
                        %(target_role)s, %(target_industry)s, %(details)s)
                ON CONFLICT (email) DO NOTHING
                """,
                contact,
            )


def insert_contact(contact: dict[str, str]) -> None:
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO contacts
                (email, name, phone, company_name, target_role,
                 target_industry, details)
            VALUES (%(email)s, %(name)s, %(phone)s, %(company_name)s,
                    %(target_role)s, %(target_industry)s, %(details)s)
            """,
            contact,
        )


def update_contact(original_email: str, contact: dict[str, str]) -> bool:
    with _connect() as connection:
        cursor = connection.execute(
            """
            UPDATE contacts
            SET email = %(email)s,
                name = %(name)s,
                phone = %(phone)s,
                company_name = %(company_name)s,
                target_role = %(target_role)s,
                target_industry = %(target_industry)s,
                details = %(details)s,
                updated_at = NOW()
            WHERE email = %(original_email)s
            """,
            {**contact, "original_email": original_email},
        )
        return cursor.rowcount == 1


def delete_contact(email: str) -> bool:
    with _connect() as connection:
        cursor = connection.execute("DELETE FROM contacts WHERE email = %s", (email,))
        return cursor.rowcount == 1


def load_company_profiles() -> dict[str, dict[str, Any]]:
    with _connect() as connection:
        rows = connection.execute("SELECT name, profile FROM company_profiles").fetchall()
    return {row["name"]: row["profile"] for row in rows}


def save_company_profiles(profiles: dict[str, dict[str, Any]]) -> None:
    with _connect() as connection:
        connection.execute("DELETE FROM company_profiles")
        for name, profile in profiles.items():
            connection.execute(
                "INSERT INTO company_profiles (name, profile) VALUES (%s, %s)",
                (name, Jsonb(profile)),
            )


def delete_company_profile(name: str) -> bool:
    with _connect() as connection:
        cursor = connection.execute("DELETE FROM company_profiles WHERE name = %s", (name,))
        return cursor.rowcount == 1


def get_meta(key: str) -> str | None:
    with _connect() as connection:
        row = connection.execute("SELECT value FROM app_meta WHERE key = %s", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(key: str, value: str) -> None:
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO app_meta (key, value) VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """,
            (key, value),
        )


def mutate_meta(key: str, mutator: Callable[[dict[str, Any]], Any]) -> Any:
    """Atomically reads, mutates, and saves a JSON document stored in app_meta.

    The row is locked with SELECT ... FOR UPDATE, so concurrent workers on
    different processes cannot claim the same queued recipient.
    """
    with _connect() as connection:
        connection.execute(
            "INSERT INTO app_meta (key, value) VALUES (%s, '{}') "
            "ON CONFLICT (key) DO NOTHING",
            (key,),
        )
        row = connection.execute(
            "SELECT value FROM app_meta WHERE key = %s FOR UPDATE", (key,)
        ).fetchone()
        raw = row["value"] if row else ""
        try:
            state = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            state = {}
        result = mutator(state)
        connection.execute(
            "UPDATE app_meta SET value = %s WHERE key = %s",
            (json.dumps(state), key),
        )
        return result


def load_suppressions() -> set[str]:
    with _connect() as connection:
        rows = connection.execute("SELECT email FROM suppressions").fetchall()
    return {str(row["email"]).strip().lower() for row in rows}


def log_send(email: str, status: str, subject: str) -> None:
    with _connect() as connection:
        connection.execute(
            "INSERT INTO sent_log (email, status, subject) VALUES (%s, %s, %s)",
            (email, status, subject),
        )


def is_suppressed(email: str) -> bool:
    with _connect() as connection:
        row = connection.execute(
            "SELECT 1 FROM suppressions WHERE email = %s",
            (email.strip().lower(),),
        ).fetchone()
    return row is not None


def add_suppression(email: str, reason: str = "unsubscribe") -> None:
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO suppressions (email, reason)
            VALUES (%s, %s)
            ON CONFLICT (email) DO NOTHING
            """,
            (email.strip().lower(), reason),
        )


def count_sent_today() -> int:
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM sent_log
            WHERE timestamp >= date_trunc('day', NOW())
              AND status IN ('sent', 'pitch:sent')
            """
        ).fetchone()
    return int(row["count"]) if row else 0


# ---------------------------------------------------------------------------
# Generic campaign document store
# ---------------------------------------------------------------------------


def _record_filters(filters: dict[str, Any] | None) -> tuple[str, list[Any]]:
    clause = ""
    params: list[Any] = []
    for key, value in (filters or {}).items():
        if value is None:
            continue
        clause += " AND data ->> %s = %s"
        params.extend([str(key), str(value)])
    return clause, params


def list_records(collection: str, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    clause, params = _record_filters(filters)
    with _connect() as connection:
        rows = connection.execute(
            "SELECT data FROM records WHERE collection = %s" + clause + " ORDER BY created_at, id",
            [collection, *params],
        ).fetchall()
    return [row["data"] for row in rows]


def get_record(collection: str, record_id: str) -> dict[str, Any] | None:
    with _connect() as connection:
        row = connection.execute(
            "SELECT data FROM records WHERE collection = %s AND id = %s",
            (collection, record_id),
        ).fetchone()
    return row["data"] if row else None


def save_record(collection: str, record_id: str, data: dict[str, Any]) -> None:
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO records (collection, id, data)
            VALUES (%s, %s, %s)
            ON CONFLICT (collection, id)
            DO UPDATE SET data = EXCLUDED.data, updated_at = NOW()
            """,
            (collection, record_id, Jsonb(data)),
        )


def save_records(collection: str, records: list[dict[str, Any]]) -> None:
    """Upserts many records in one round trip (id lives in 'id')."""
    if not records:
        return
    with _connect() as connection:
        with connection.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO records (collection, id, data)
                VALUES (%s, %s, %s)
                ON CONFLICT (collection, id)
                DO UPDATE SET data = EXCLUDED.data, updated_at = NOW()
                """,
                [(collection, record["id"], Jsonb(record)) for record in records],
            )


def delete_record(collection: str, record_id: str) -> bool:
    with _connect() as connection:
        cursor = connection.execute(
            "DELETE FROM records WHERE collection = %s AND id = %s",
            (collection, record_id),
        )
        return cursor.rowcount > 0


def delete_records(collection: str, filters: dict[str, Any] | None = None) -> int:
    clause, params = _record_filters(filters)
    with _connect() as connection:
        cursor = connection.execute(
            "DELETE FROM records WHERE collection = %s" + clause,
            [collection, *params],
        )
        return cursor.rowcount


def claim_record_status(
    collection: str,
    record_id: str,
    field: str,
    expected: str,
    new_value: str,
) -> dict[str, Any] | None:
    """Atomically moves a record from one status to another.

    Used by the send queue so two worker ticks can never send the same lead.
    Returns the updated record, or None if another worker claimed it first.
    """
    with _connect() as connection:
        row = connection.execute(
            """
            UPDATE records
            SET data = jsonb_set(data, %s::text[], to_jsonb(%s::text), true),
                updated_at = NOW()
            WHERE collection = %s
              AND id = %s
              AND data ->> %s = %s
            RETURNING data
            """,
            (f"{{{field}}}", new_value, collection, record_id, field, expected),
        ).fetchone()
    return row["data"] if row else None
