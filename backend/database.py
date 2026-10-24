import os
import threading
from typing import Any

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


def log_send(email: str, status: str, subject: str) -> None:
    with _connect() as connection:
        connection.execute(
            "INSERT INTO sent_log (email, status, subject) VALUES (%s, %s, %s)",
            (email, status, subject),
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
