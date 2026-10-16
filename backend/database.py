import json
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

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
"""


def is_configured() -> bool:
    return bool(config.DATABASE_URL)


def _connect() -> psycopg.Connection:
    if not config.DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured.")
    connection = psycopg.connect(config.DATABASE_URL, row_factory=dict_row)
    connection.execute(SCHEMA)
    return connection


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


def log_send(email: str, status: str, subject: str) -> None:
    with _connect() as connection:
        connection.execute(
            "INSERT INTO sent_log (email, status, subject) VALUES (%s, %s, %s)",
            (email, status, subject),
        )
