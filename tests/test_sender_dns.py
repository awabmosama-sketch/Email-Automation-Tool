"""Per-sender DNS settings: persistence, validation, and deployment secrets.

These tests exercise the live sender path (backend.outreach._clean_sender and the
SenderRequest model) without touching PostgreSQL, SMTP, or DNS.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from backend import config, outreach
from backend.app import SenderRequest
from backend.deliverability import recommended_records

PROJECT_ROOT = Path(__file__).resolve().parents[1]

SENDER = {
    "email": "info@getappdeon.com",
    "provider": "zoho",
    "password": "app-password",
    "host": "smtp.zoho.com",
}


def clean(payload=None, existing=None):
    return outreach._clean_sender(dict(payload or SENDER), existing)


def test_provider_default_spf_include_is_seeded():
    sender = clean()
    assert sender["spf_include"] == "include:zoho.com"
    assert sender["dmarc_policy"] == "none"


def test_gmail_default_include_with_underscore_is_accepted():
    # include:_spf.google.com is the app's own default, so validation must allow "_".
    sender = clean({**SENDER, "provider": "gmail"})
    assert sender["spf_include"] == "include:_spf.google.com"


def test_partial_update_preserves_dns_and_password():
    created = clean()
    created = clean({"spf_include": "include:example.net", "dmarc_policy": "reject"}, created)

    patched = clean({"timezone": "Europe/London"}, created)

    assert patched["timezone"] == "Europe/London"
    for key in ("spf_include", "dmarc_policy", "dkim_selector", "password", "host", "port", "security"):
        assert patched[key] == created[key], key


def test_explicit_null_means_unchanged():
    created = clean()
    patched = clean({"spf_include": None, "dmarc_policy": None, "timezone": "UTC"}, created)
    assert patched["spf_include"] == created["spf_include"]
    assert patched["dmarc_policy"] == created["dmarc_policy"]


def test_cleared_spf_survives_an_unrelated_patch():
    created = clean()
    cleared = clean({"spf_include": ""}, created)
    assert cleared["spf_include"] == ""

    patched = clean({"timezone": "Europe/London"}, cleared)
    assert patched["spf_include"] == ""


def test_stored_empty_dmarc_is_not_rewritten():
    """Contract test for direct API clients: the UI cannot submit an explicit empty
    DMARC (its dropdown is none/quarantine/reject), so do not "fix" this by
    normalizing "" to "none" — the stored value must survive a partial update."""
    created = clean({**SENDER, "dmarc_policy": ""})
    assert created["dmarc_policy"] == ""

    patched = clean({"timezone": "Europe/London"}, created)
    assert patched["dmarc_policy"] == ""


def test_invalid_dmarc_falls_back_to_none():
    assert clean({**SENDER, "dmarc_policy": "bogus"})["dmarc_policy"] == "none"


@pytest.mark.parametrize(
    "value",
    [
        "v=spf1 include:zoho.com ~all",
        "include:",
        "ip4:1.2.3.4",
        "include:two.example.com include:zoho.com",
        "include:bad host",
    ],
)
def test_invalid_spf_include_is_rejected(value):
    with pytest.raises(outreach.OutreachError) as error:
        clean({**SENDER, "spf_include": value})
    assert error.value.status_code == 422


def test_outlook_cannot_be_selected_for_a_new_sender():
    with pytest.raises(outreach.OutreachError) as error:
        clean({**SENDER, "provider": "outlook"})
    assert error.value.status_code == 422
    assert "OAuth" in error.value.message


def test_legacy_outlook_sender_remains_editable():
    stored = {
        "email": "info@getappdeon.com",
        "provider": "outlook",
        "host": "smtp.office365.com",
        "port": 587,
        "security": "starttls",
        "password": "ciphertext",
        "spf_include": "include:spf.protection.outlook.com",
        "dmarc_policy": "none",
    }
    patched = clean({"timezone": "Europe/London"}, stored)
    assert patched["provider"] == "outlook"
    assert patched["spf_include"] == stored["spf_include"]


def test_sender_request_omits_dns_fields_when_not_sent():
    payload = SenderRequest(email=SENDER["email"], timezone="Europe/London").model_dump(
        exclude_unset=True
    )
    assert "spf_include" not in payload
    assert "dmarc_policy" not in payload


def test_sender_request_accepts_null_and_explicit_values():
    payload = SenderRequest(
        **{"spf_include": None, "dmarc_policy": "reject"}
    ).model_dump(exclude_unset=True)
    assert payload["spf_include"] is None
    assert payload["dmarc_policy"] == "reject"


def test_recommended_records_use_sender_values():
    records = recommended_records(
        "getappdeon.com",
        "zoho",
        dkim_selector="zoho",
        spf_include="include:zoho.com",
        dmarc_policy="reject",
    )
    assert records["spf"]["value"] == "v=spf1 include:zoho.com ~all"
    assert "p=reject" in records["dmarc"]["value"]
    assert "adkim=s" in records["dmarc"]["value"]
    assert "aspf=s" in records["dmarc"]["value"]


def _run_config_import(env_overrides, remove=()):
    env = {**os.environ, **env_overrides}
    for key in remove:
        env.pop(key, None)
    return subprocess.run(
        [sys.executable, "-c", "import backend.config; print(backend.config.APP_ENV)"],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def test_missing_session_secret_fails_fast_outside_development():
    result = _run_config_import(
        {"VERCEL": "1", "SESSION_SECRET": "", "APP_PASSWORD": "set-in-env"},
        remove=("APP_ENV", "ENVIRONMENT", "VERCEL_ENV"),
    )
    assert result.returncode != 0
    assert "SESSION_SECRET" in (result.stderr + result.stdout)
    assert "RuntimeError" in (result.stderr + result.stdout)


def test_missing_app_password_fails_fast_outside_development():
    result = _run_config_import(
        {"VERCEL": "1", "APP_PASSWORD": "", "SESSION_SECRET": "set-in-env"},
        remove=("APP_ENV", "ENVIRONMENT", "VERCEL_ENV"),
    )
    assert result.returncode != 0
    assert "APP_PASSWORD" in (result.stderr + result.stdout)


def test_development_falls_back_to_a_local_secret():
    assert config.SESSION_SECRET
    assert config.IS_DEVELOPMENT or config.APP_ENV not in {"development", "dev", "local"}
