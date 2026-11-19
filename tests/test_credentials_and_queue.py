"""Credential key rotation, stale-claim phases, and the production queue guard."""

import hashlib
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from backend import config, credential_crypto, outreach
from backend.credential_crypto import CredentialDecryptError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OLD_KEY = "old-hardcoded-fallback"
NEW_KEY = "new-session-secret"
UNRELATED_KEY = "unrelated-key"


@pytest.fixture
def rotated_keys(monkeypatch):
    """Current key is NEW_KEY; OLD_KEY is only available as a legacy key."""
    monkeypatch.setattr(config, "SENDER_CREDENTIAL_KEY", "")
    monkeypatch.setattr(config, "APP_PASSWORD", "")
    monkeypatch.setattr(config, "SESSION_SECRET", NEW_KEY)
    monkeypatch.setattr(config, "SENDER_CREDENTIAL_KEY_LEGACY", OLD_KEY)


def encrypt_under(material, value):
    original = config.SESSION_SECRET
    config.SESSION_SECRET = material
    try:
        return credential_crypto.encrypt(value)
    finally:
        config.SESSION_SECRET = original


# ---------------------------------------------------------------------------
# Item 1: legacy-key fallback, lazy re-encryption, explicit failure
# ---------------------------------------------------------------------------


def test_legacy_credential_is_reencrypted_under_current_key(rotated_keys):
    stored = encrypt_under(OLD_KEY, "app-password")

    plaintext, migrated = credential_crypto.decrypt_and_migrate(stored, "snd_1")

    assert plaintext == "app-password"
    assert migrated and migrated != stored
    # Once persisted, the value no longer needs the legacy key at all.
    config.SENDER_CREDENTIAL_KEY_LEGACY = ""
    # Read-only assertions (item 3, option b): tests check the migrated value,
    # they never persist it.
    assert credential_crypto.decrypt(migrated, "snd_1") == "app-password"
    assert credential_crypto.is_encrypted(migrated)


def test_decrypt_sender_account_persists_the_new_ciphertext(rotated_keys, monkeypatch):
    stored = encrypt_under(OLD_KEY, "app-password")
    saved = {}

    def fake_mutate(mutator):
        state = {"senders": [{"id": "snd_1", "password": stored, "updated_at": ""}]}
        mutator(state)
        saved.update(state["senders"][0])

    monkeypatch.setattr(outreach, "_mutate", fake_mutate)
    account = outreach.decrypt_sender_account({"id": "snd_1", "password": stored})

    assert account["password"] == "app-password"
    assert saved["password"] not in (stored, "")
    config.SENDER_CREDENTIAL_KEY_LEGACY = ""
    assert credential_crypto.decrypt(saved["password"], "snd_1") == "app-password"


def test_unrelated_key_raises_with_the_sender_id(rotated_keys):
    stored = encrypt_under(UNRELATED_KEY, "app-password")

    with pytest.raises(CredentialDecryptError) as error:
        credential_crypto.decrypt(stored, "snd_42")

    assert "snd_42" in str(error.value)
    assert error.value.sender_id == "snd_42"


def test_legacy_custom_session_secret_is_reencrypted(monkeypatch):
    """The intended migration path: a rotated private secret needs no public key."""
    old_secret = "old-custom-secret-xyz"
    monkeypatch.setattr(config, "SENDER_CREDENTIAL_KEY", "")
    monkeypatch.setattr(config, "APP_PASSWORD", "")
    monkeypatch.setattr(config, "SESSION_SECRET", old_secret)
    monkeypatch.setattr(config, "SENDER_CREDENTIAL_KEY_LEGACY", "")
    stored = credential_crypto.encrypt("app-password")

    monkeypatch.setattr(config, "SESSION_SECRET", NEW_KEY)
    monkeypatch.setattr(config, "SENDER_CREDENTIAL_KEY_LEGACY", old_secret)
    plaintext, migrated = credential_crypto.decrypt_and_migrate(stored, "snd_custom")

    assert plaintext == "app-password"
    assert migrated and migrated != stored
    # The migrated row stands alone under the current key.
    monkeypatch.setattr(config, "SENDER_CREDENTIAL_KEY_LEGACY", "")
    assert credential_crypto.decrypt(migrated, "snd_custom") == "app-password"
    assert not credential_crypto.legacy_key_is_public()


def test_startup_audit_flags_a_public_legacy_key(monkeypatch):
    # The real fallback value is deliberately absent from every tracked file; the
    # check only compares sha256 digests, so a stand-in proves the mechanism.
    material = "a-publicly-known-test-secret"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    monkeypatch.setattr(credential_crypto, "_PUBLIC_LEGACY_FINGERPRINTS", {digest: "test"})
    monkeypatch.setattr(config, "SENDER_CREDENTIAL_KEY_LEGACY", material)
    assert credential_crypto.legacy_key_is_public()

    monkeypatch.setattr(config, "SENDER_CREDENTIAL_KEY_LEGACY", "old-custom-secret-xyz")
    assert not credential_crypto.legacy_key_is_public()


def test_startup_audit_names_senders_on_the_legacy_key(rotated_keys):
    legacy = encrypt_under(OLD_KEY, "a")
    current = credential_crypto.encrypt("b")

    assert credential_crypto.legacy_encrypted_ids(
        [("snd_old", legacy), ("snd_new", current)]
    ) == ["snd_old"]
    assert credential_crypto.undecryptable_ids([("snd_old", legacy)]) == []


# ---------------------------------------------------------------------------
# Item 2: two-phase stale claims + retry exclusion
# ---------------------------------------------------------------------------

QUEUE_STATE_TEMPLATE = {
    "senders": [
        {
            "id": "snd_1",
            "email": "sender@example.com",
            "provider": "smtp",
            "host": "smtp.example.com",
            "password": "x",
            "hourly_limit": 10,
            "daily_limit": 10,
        }
    ],
    "templates": [{"id": "tpl_1", "subject": "Hi", "body": "Body"}],
    "brands": [],
    "campaigns": [
        {
            "id": "cmp_1",
            "status": "running",
            "sender_id": "snd_1",
            "template_id": "tpl_1",
            "timezone": "UTC",
            "hourly_limit": 10,
            "daily_limit": 10,
            "work_start_hour": 0,
            "work_end_hour": 24,
            "weekdays_only": False,
            "delay_min_minutes": 1,
            "delay_max_minutes": 1,
        }
    ],
    "recipients": [],
    "events": [],
}


def queue_state(claim_age_minutes, status="sending"):
    import copy

    state = copy.deepcopy(QUEUE_STATE_TEMPLATE)
    now = outreach._now()
    state["recipients"] = [
        {
            "id": "rcp_1",
            "campaign_id": "cmp_1",
            "email": "lead@example.com",
            "status": status,
            "claimed_at": outreach._iso(now - timedelta(minutes=claim_age_minutes)),
            "attempts": 1,
            "error": "",
            "updated_at": outreach._iso(now),
        }
    ]
    return state


def run_claim(monkeypatch, state):
    monkeypatch.setattr(outreach, "_mutate", lambda mutator: mutator(state))
    monkeypatch.setattr(outreach, "_within_working_hours", lambda campaign, now: True)
    monkeypatch.setattr(outreach, "_sender_caps_allow", lambda state, sender, now: True)
    outreach.claim_due()
    return state["recipients"][0]


def test_stale_claim_does_not_mark_failed_on_first_window(monkeypatch):
    recipient = run_claim(monkeypatch, queue_state(outreach.STALE_CLAIM_MINUTES + 5))
    assert recipient["status"] == "stale"
    assert recipient["status"] != "failed"


def test_unconfirmed_after_second_window_marks_failed(monkeypatch):
    recipient = run_claim(
        monkeypatch, queue_state(outreach.UNCONFIRMED_CLAIM_MINUTES + 5)
    )
    assert recipient["status"] == "failed"
    assert recipient["error"] == "unconfirmed"


def test_fresh_claim_is_left_alone(monkeypatch):
    recipient = run_claim(monkeypatch, queue_state(1))
    assert recipient["status"] == "sending"


def test_user_retry_excludes_stale_rows(monkeypatch):
    state = queue_state(0, status="sending")
    state["recipients"] = [
        {"id": "rcp_failed", "campaign_id": "cmp_1", "status": "failed", "email": "a@x.com"},
        {"id": "rcp_bounced", "campaign_id": "cmp_1", "status": "bounced", "email": "b@x.com"},
        {"id": "rcp_stale", "campaign_id": "cmp_1", "status": "stale", "email": "c@x.com"},
    ]
    monkeypatch.setattr(outreach, "_mutate", lambda mutator: mutator(state))

    outreach.requeue("cmp_1")

    statuses = {item["id"]: item["status"] for item in state["recipients"]}
    # _schedule_first promotes one retried row straight to "scheduled".
    assert statuses["rcp_failed"] in {"queued", "scheduled"}
    assert statuses["rcp_bounced"] in {"queued", "scheduled"}
    assert statuses["rcp_stale"] == "stale"


def test_explicit_retry_statuses_cannot_smuggle_in_stale(monkeypatch):
    state = queue_state(0, status="sending")
    state["recipients"] = [
        {"id": "rcp_stale", "campaign_id": "cmp_1", "status": "stale", "email": "c@x.com"},
    ]
    monkeypatch.setattr(outreach, "_mutate", lambda mutator: mutator(state))

    outreach.requeue("cmp_1", statuses={"stale", "failed"})

    assert state["recipients"][0]["status"] == "stale"


# ---------------------------------------------------------------------------
# Item 3: production worker refuses the file-backed queue
# ---------------------------------------------------------------------------


def test_worker_refuses_to_start_in_production_without_database_url():
    env = {
        **os.environ,
        "APP_ENV": "production",
        "APP_PASSWORD": "set-in-env",
        "SESSION_SECRET": "set-in-env",
        # An empty value blocks .env from supplying DATABASE_URL.
        "DATABASE_URL": "",
    }
    for key in ("VERCEL", "VERCEL_ENV", "ENVIRONMENT"):
        env.pop(key, None)

    result = subprocess.run(
        [sys.executable, "-c", "import backend.outreach as o; o.start_worker()"],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "RuntimeError" in result.stderr
    assert "DATABASE_URL" in result.stderr


# ---------------------------------------------------------------------------
# Item 4: legacy Outlook senders can still be edited
# ---------------------------------------------------------------------------

LEGACY_OUTLOOK = {
    "id": "snd_outlook",
    "email": "info@example.com",
    "provider": "outlook",
    "host": "smtp.office365.com",
    "port": 587,
    "security": "starttls",
    "password": "ciphertext",
    "spf_include": "include:spf.protection.outlook.com",
    "dmarc_policy": "none",
    "timezone": "UTC",
}


def test_legacy_outlook_sender_timezone_update_succeeds():
    updated = outreach._clean_sender({"timezone": "Europe/London"}, LEGACY_OUTLOOK)

    assert updated["provider"] == "outlook"
    assert updated["timezone"] == "Europe/London"
    assert updated["password"] == "ciphertext"
    assert updated["spf_include"] == "include:spf.protection.outlook.com"


# ---------------------------------------------------------------------------
# Item 2 (follow-up): startup call site must not break worker-disabled deploys
# ---------------------------------------------------------------------------


def test_importing_the_app_with_vercel_and_no_database_url_does_not_raise():
    env = {
        **os.environ,
        "APP_ENV": "production",
        "VERCEL": "1",
        "DATABASE_URL": "",
        "APP_PASSWORD": "set-in-env",
        "SESSION_SECRET": "set-in-env",
    }

    result = subprocess.run(
        [sys.executable, "-c", "import backend.app"],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_worker_disabled_deploy_starts_without_database_url(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setattr(config, "IS_DEVELOPMENT", False)
    monkeypatch.setattr(config, "DATABASE_URL", "")
    monkeypatch.setattr(outreach, "_startup_audit_done", False)

    outreach.start_worker()  # degrades to "worker disabled" instead of raising


def test_start_worker_does_not_repeat_the_credential_audit(monkeypatch):
    calls = []
    monkeypatch.setattr(
        outreach, "audit_sender_credentials", lambda: calls.append("audit")
    )
    monkeypatch.setattr(outreach, "_startup_audit_done", False)

    class AliveThread:
        def is_alive(self):
            return True

    monkeypatch.setattr(outreach, "_worker_thread", AliveThread())
    outreach.start_worker()
    outreach.start_worker()

    assert calls == ["audit"]


# ---------------------------------------------------------------------------
# Item 3 (follow-up): the Test button drains the legacy list too
# ---------------------------------------------------------------------------


def test_sender_test_button_migrates_a_legacy_credential(rotated_keys, monkeypatch):
    """The route calls get_sender_account -> decrypt_sender_account, so pressing
    Test re-encrypts exactly like a campaign send does."""
    from backend import app as app_module

    stored = encrypt_under(OLD_KEY, "app-password")
    state = {
        "senders": [
            {
                "id": "snd_1",
                "email": "sender@example.com",
                "provider": "smtp",
                "host": "smtp.example.com",
                "port": 587,
                "security": "starttls",
                "password": stored,
                "updated_at": "",
            }
        ],
        "brands": [],
        "templates": [],
        "campaigns": [],
        "recipients": [],
        "events": [],
    }
    monkeypatch.setattr(outreach, "load_state", lambda: state)
    monkeypatch.setattr(outreach, "_mutate", lambda mutator: mutator(state))
    monkeypatch.setattr(app_module, "enforce_rate_limit", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        app_module, "test_account", lambda account: (True, "SMTP login ok")
    )

    result = app_module.test_sender("snd_1", app_module.SenderTestRequest(), None)

    assert result["ok"] is True
    assert state["senders"][0]["password"] != stored
    config.SENDER_CREDENTIAL_KEY_LEGACY = ""
    assert (
        credential_crypto.decrypt(state["senders"][0]["password"], "snd_1")
        == "app-password"
    )


# ---------------------------------------------------------------------------
# Item 4 (follow-up): requeue outside the working window stays queued
# ---------------------------------------------------------------------------


def test_requeue_outside_working_hours_leaves_rows_queued(monkeypatch):
    state = queue_state(0, status="failed")
    campaign = state["campaigns"][0]
    local_hour = outreach._now().astimezone(outreach._timezone(campaign)).hour
    # Pick a window that does not include the current hour, without wrapping.
    if local_hour < 12:
        campaign["work_start_hour"], campaign["work_end_hour"] = 20, 21
    else:
        campaign["work_start_hour"], campaign["work_end_hour"] = 0, 1
    state["recipients"] = [
        {
            "id": "rcp_failed",
            "campaign_id": "cmp_1",
            "status": "failed",
            "email": "lead@example.com",
            "created_at": outreach._iso(outreach._now()),
        }
    ]
    monkeypatch.setattr(outreach, "_mutate", lambda mutator: mutator(state))

    outreach.requeue("cmp_1")

    assert state["recipients"][0]["status"] == "queued"
