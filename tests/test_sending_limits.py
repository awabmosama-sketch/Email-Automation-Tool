"""Global campaign sending limits: defaults, persistence, and enforcement."""

from datetime import timedelta

from backend import config, outreach


def _state(**limits):
    state = outreach._normalise(
        {
            "senders": [
                {
                    "id": "snd_1",
                    "email": "sender@example.com",
                    "provider": "smtp",
                    "host": "smtp.example.com",
                    "password": "x",
                    "hourly_limit": 0,
                    "daily_limit": 0,
                }
            ],
            "templates": [{"id": "tpl_1", "subject": "Hi", "body": "Body"}],
            "campaigns": [
                {
                    "id": "cmp_1",
                    "status": "running",
                    "sender_id": "snd_1",
                    "template_id": "tpl_1",
                    "timezone": "UTC",
                    "hourly_limit": 100,
                    "daily_limit": 500,
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
    )
    state["settings"].update(limits)
    return state


def _recipient(recipient_id, status, sent_at=None):
    return {
        "id": recipient_id,
        "campaign_id": "cmp_1",
        "email": f"{recipient_id}@example.com",
        "status": status,
        "sent_at": outreach._iso(sent_at) if sent_at else "",
        "created_at": outreach._iso(),
    }


def test_normalise_defaults_the_limits_from_config():
    state = outreach._normalise({})

    assert state["settings"] == {
        "hourly_limit": config.CAMPAIGN_DEFAULT_HOURLY_LIMIT,
        "daily_limit": config.CAMPAIGN_DEFAULT_DAILY_LIMIT,
    }


def test_update_limits_persists_values_and_clamps_the_maximum(monkeypatch):
    state = _state()
    monkeypatch.setattr(outreach, "_mutate", lambda mutator: mutator(state))

    result = outreach.update_limits({"hourly_limit": 3, "daily_limit": 12})
    assert result["limits"] == {"hourly_limit": 3, "daily_limit": 12}
    assert state["settings"] == {"hourly_limit": 3, "daily_limit": 12}

    result = outreach.update_limits({"hourly_limit": 999})
    assert result["limits"]["hourly_limit"] == 100
    assert result["limits"]["daily_limit"] == 12


def test_hourly_limit_blocks_claims_for_the_rest_of_the_rolling_hour(monkeypatch):
    state = _state(hourly_limit=1, daily_limit=50)
    state["recipients"] = [
        _recipient("already_sent", "sent", outreach._now() - timedelta(minutes=2)),
        _recipient("queued_1", "queued"),
    ]
    monkeypatch.setattr(outreach, "_mutate", lambda mutator: mutator(state))

    assert outreach.claim_due() == []


def test_daily_limit_blocks_claims_after_the_daily_cap(monkeypatch):
    state = _state(hourly_limit=50, daily_limit=1)
    state["recipients"] = [
        _recipient("already_sent", "sent", outreach._now() - timedelta(hours=2)),
        _recipient("queued_1", "queued"),
    ]
    monkeypatch.setattr(outreach, "_mutate", lambda mutator: mutator(state))

    assert outreach.claim_due() == []


def test_claim_due_respects_the_remaining_global_budget(monkeypatch):
    state = _state(hourly_limit=1, daily_limit=50)
    state["campaigns"].append(
        {
            **state["campaigns"][0],
            "id": "cmp_2",
            "name": "Second",
        }
    )
    state["recipients"] = [
        _recipient("queued_1", "queued"),
        {**_recipient("queued_2", "queued"), "campaign_id": "cmp_2"},
    ]
    monkeypatch.setattr(outreach, "_mutate", lambda mutator: mutator(state))

    claims = outreach.claim_due()

    assert len(claims) == 1


def test_limits_appear_in_overview_with_usage(monkeypatch):
    state = _state(hourly_limit=4, daily_limit=20)
    state["recipients"] = [_recipient("already_sent", "sent", outreach._now())]
    monkeypatch.setattr(outreach, "load_state", lambda: state)

    overview = outreach.overview()

    assert overview["limits"] == {"hourly_limit": 4, "daily_limit": 20}
    assert overview["limits_usage"]["sent_last_hour"] == 1
    assert overview["limits_usage"]["sent_today"] == 1
