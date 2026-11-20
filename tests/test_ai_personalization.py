"""AI personalization must actually reach the sent message.

Regression test for the shadowed ``generate_campaign_email``: the live send
path calls it positionally with ``(recipient, campaign, template, brand,
sender)``. A keyword-only second definition used to shadow that function, so
the call raised TypeError, ``process_queue`` swallowed it, and every AI-enabled
campaign silently sent the plain template instead.
"""

import importlib

import pytest

from backend import config, outreach


@pytest.mark.unit
def test_ai_personalization_is_not_silently_broken(monkeypatch):
    # Make importing backend.llm independent of real provider credentials.
    monkeypatch.setattr(config, "LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    llm = importlib.import_module("backend.llm")
    monkeypatch.setattr(
        llm,
        "_complete",
        lambda *args, **kwargs: '{"subject": "AI subject", "body": "AI personalized body"}',
    )

    claim = {
        "campaign": {
            "id": "cmp_ai",
            "name": "AI campaign",
            "use_ai": True,
            "ai_prompt": "Mention their company.",
        },
        "recipient": {
            "id": "rcp_1",
            "email": "lead@example.com",
            "first_name": "Ada",
            "company_name": "Northwind",
        },
        "sender": {"name": "Sender", "email": "sender@example.com"},
        "template": {
            "subject": "Template subject for {{First Name}}",
            "body": "Template body for {{First Name}}",
        },
        "brand": {"name": "Brand"},
    }
    sent: dict[str, str] = {}

    monkeypatch.setattr(outreach, "_assert_queue_backend", lambda: None)
    monkeypatch.setattr(outreach, "claim_due", lambda: [claim])
    monkeypatch.setattr(outreach.suppressions, "is_suppressed", lambda email: False)
    monkeypatch.setattr(outreach, "decrypt_sender_account", lambda sender: dict(sender))
    monkeypatch.setattr(outreach, "finish_send", lambda *args, **kwargs: None)

    def fake_send(account, email, subject, html, body, **kwargs):
        sent.update(subject=subject, html=html, body=body)
        return True, "", "sent"

    monkeypatch.setattr(outreach, "send_account_email", fake_send)

    result = outreach.process_queue(max_sends=1)

    assert result["results"][0]["status"] == "sent"
    assert sent["subject"] == "AI subject"
    assert sent["body"] == "AI personalized body"
