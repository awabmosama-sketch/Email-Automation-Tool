"""Multi-campaign outreach: senders, brands, templates, campaigns, and queue.

The queue is a durable document (Neon app_meta JSONB when DATABASE_URL is set,
otherwise .local-data/outreach.json). It replaces a Celery/Redis stack for this
single-workspace workload: every claim is atomic (row lock or process lock), so
parallel workers, parallel campaigns, and parallel serverless invocations can
never send the same recipient twice.

Campaigns are fully isolated: each one has its own sheet, mapping, template,
sender, hourly cap, daily cap, working hours, and randomized delay window.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import random
import re
import secrets
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from . import config, credential_crypto, database, suppressions
from .sender import send_account_email, wrap_in_html
from .sheets import TARGET_FIELDS
from .unsubscribe import build_unsubscribe_url
from .verifier import is_valid_format

logger = logging.getLogger(__name__)

STATE_KEY = "outreach"
MAX_EVENTS = 500
MAX_RECIPIENTS = 20000
# Two-phase stale handling. A send that has not been recorded as finished is not
# proof of failure: the SMTP handoff may have completed before the tick died.
# 45 minutes covers a worst-case connect + TLS + DATA + provider ack with one
# retry (a few minutes) and still leaves ~10x margin for provider throttling;
# after 3 hours total the result is treated as never confirmed and the row is
# reported failed so the operator can decide whether to retry.
STALE_CLAIM_MINUTES = 45
UNCONFIRMED_CLAIM_MINUTES = 180
SENT_STATUSES = {"sent", "inboxed", "spam_alert"}
TERMINAL_STATUSES = SENT_STATUSES | {"failed", "bounced", "skipped"}
# A retry re-sends the message, so it is only allowed for rows known not to have
# been delivered. "stale" and "sending" may already be in the recipient's inbox.
RETRYABLE_STATUSES = {"failed", "bounced"}

DEFAULT_MAPPING = {
    "first_name": "First Name",
    "position": "Position",
    "company_name": "Company Name",
    "personal_email": "Personal Email",
    "business_email": "Business Email",
}

_EMPTY_STATE = {
    "senders": [],
    "brands": [],
    "templates": [],
    "campaigns": [],
    "recipients": [],
    "events": [],
}

_STATE_LOCK = threading.RLock()
_worker_lock = threading.Lock()
_worker_thread: threading.Thread | None = None
_startup_audit_done = False
_worker_stop = threading.Event()


class OutreachError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime | None = None) -> str:
    return (moment or _now()).astimezone(timezone.utc).isoformat()


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(4)}"


def _normalise(state: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(state, dict):
        state = {}
    for key, default in _EMPTY_STATE.items():
        if not isinstance(state.get(key), list):
            state[key] = copy.deepcopy(default)
    _sending_limits(state)
    for campaign in state["campaigns"]:
        _campaign_defaults(campaign)
    return state


def _load_raw_state() -> dict[str, Any]:
    if database.is_configured():
        raw = database.get_meta(STATE_KEY)
        try:
            return json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            return {}
    path = config.OUTREACH_FILE
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.exception("Could not read outreach state from %s", path)
        return {}


def _read_state() -> dict[str, Any]:
    return _normalise(_load_raw_state())


def _needs_gid_repair(raw: dict[str, Any]) -> bool:
    campaigns = raw.get("campaigns") if isinstance(raw, dict) else None
    if not isinstance(campaigns, list):
        return False
    return any(
        isinstance(campaign, dict) and not campaign.get("sheet_gid_repaired")
        for campaign in campaigns
    )


def _write_state(state: dict[str, Any]) -> None:
    path = config.OUTREACH_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _mutate(mutator: Callable[[dict[str, Any]], Any]) -> Any:
    if database.is_configured():
        return database.mutate_meta(
            STATE_KEY, lambda raw: mutator(_normalise(raw))
        )
    with _STATE_LOCK:
        state = _read_state()
        result = mutator(state)
        _write_state(state)
        return result


def load_state() -> dict[str, Any]:
    with _STATE_LOCK:
        raw = _load_raw_state()
        # Check before _normalise mutates the document in place and adds the marker.
        needs_repair = _needs_gid_repair(raw)
        state = _normalise(raw)
        if needs_repair:
            # Persist the one-time gid repair on the read that performs it, so
            # it survives a restart even if no other write happens. Re-running
            # is idempotent because the marker is written with the repair.
            if database.is_configured():
                database.set_meta(STATE_KEY, json.dumps(state))
            else:
                _write_state(state)
        return copy.deepcopy(state)


def _find(items: list[dict], item_id: str) -> dict | None:
    return next((item for item in items if item.get("id") == item_id), None)


def _require(items: list[dict], item_id: str, label: str) -> dict:
    item = _find(items, item_id)
    if not item:
        raise OutreachError(f"{label} not found.", 404)
    return item


# ---------------------------------------------------------------------------
# Campaign defaults
# ---------------------------------------------------------------------------

def _int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _sending_limits(state: dict[str, Any]) -> dict[str, int]:
    """Global hourly/daily ceiling shared by every running campaign."""
    limits = state.get("settings")
    if not isinstance(limits, dict):
        limits = {}
    limits["hourly_limit"] = _int(
        limits.get("hourly_limit"), config.CAMPAIGN_DEFAULT_HOURLY_LIMIT, 1, 100
    )
    limits["daily_limit"] = _int(
        limits.get("daily_limit"), config.CAMPAIGN_DEFAULT_DAILY_LIMIT, 1, 500
    )
    state["settings"] = limits
    return limits


def _sending_usage(state: dict[str, Any], now: datetime | None = None) -> dict[str, int]:
    """Counts successful campaign sends in the rolling hour and the UTC day."""
    moment = now or _now()
    stamps = [
        _parse_dt(recipient.get("sent_at"))
        for recipient in state["recipients"]
        if recipient.get("status") in SENT_STATUSES
    ]
    stamps = [stamp for stamp in stamps if stamp]
    return {
        "sent_last_hour": sum(1 for stamp in stamps if stamp > moment - timedelta(hours=1)),
        "sent_today": sum(1 for stamp in stamps if stamp.date() == moment.date()),
    }


def update_limits(payload: dict[str, Any]) -> dict[str, Any]:
    """Persists the global campaign sending limits."""

    def mutator(state: dict[str, Any]) -> dict[str, Any]:
        limits = _sending_limits(state)
        if "hourly_limit" in payload and payload.get("hourly_limit") is not None:
            limits["hourly_limit"] = _int(payload["hourly_limit"], limits["hourly_limit"], 1, 100)
        if "daily_limit" in payload and payload.get("daily_limit") is not None:
            limits["daily_limit"] = _int(payload["daily_limit"], limits["daily_limit"], 1, 500)
        return {"limits": dict(limits), "usage": _sending_usage(state)}

    return _mutate(mutator)


def _repair_fabricated_gid(campaign: dict[str, Any]) -> None:
    """One-time repair for campaigns saved by an older app version.

    That version prefilled the gid field with "0", so a pasted URL such as
    ``.../edit?gid=679250682`` was stored with the fabricated 0 and the URL's
    real gid was never read. Only a stored "0" that was never confirmed by the
    operator is repaired, and only once per campaign (the marker below): after
    that, an operator who deliberately sets 0 keeps it. gid 0 itself stays a
    valid value everywhere -- it is only the *fabricated* 0 that is dropped.

    * URL has an explicit gid -> adopt it (the value the user actually pasted).
    * URL has no gid -> None, so the first tab is read instead of a 0 that the
      old form invented and that may not even exist on the sheet.
    * URL says ``gid=0`` -> 0 was deliberate, keep it.
    """
    if campaign.get("sheet_gid_repaired"):
        return
    campaign["sheet_gid_repaired"] = True
    if str(campaign.get("sheet_gid") or "") != "0":
        return
    url = str(campaign.get("sheet_url") or "")
    if not url:
        return
    try:
        from .sheets import parse_sheet_url

        _, url_gid = parse_sheet_url(url)
    except Exception:
        return
    if url_gid and url_gid != "0":
        logger.info(
            "Campaign %s: replacing fabricated gid 0 with gid %s from its sheet URL",
            campaign.get("id", "?"),
            url_gid,
        )
        campaign["sheet_gid"] = url_gid
    elif not url_gid:
        logger.info(
            "Campaign %s: dropping fabricated gid 0 (its sheet URL has no gid); "
            "the first tab will be used",
            campaign.get("id", "?"),
        )
        campaign["sheet_gid"] = None


def _sheet_gid(value: Any) -> str | None:
    """Normalises a submitted gid: blank means "not set" (None), never 0.

    An explicit 0 is preserved because gid 0 is a real first tab.
    """
    text = str(value or "").strip()[:20]
    return text or None


def _resolve_sheet_gid(value: Any, sheet_url: str) -> str | None:
    """Resolves the gid a campaign targets, at write time.

    A gid typed into the form wins. Otherwise the gid in the pasted URL
    (``?gid=`` or ``#gid=``) is stored on the campaign, so the record states
    which tab it targets instead of every read re-parsing the URL. No gid
    anywhere means None (first tab).
    """
    explicit = _sheet_gid(value)
    if explicit:
        return explicit
    url = str(sheet_url or "")
    if not url:
        return None
    try:
        from .sheets import parse_sheet_url

        _, url_gid = parse_sheet_url(url)
    except Exception:
        return None
    return url_gid or None


def _campaign_defaults(campaign: dict[str, Any]) -> dict[str, Any]:
    campaign.setdefault("id", _new_id("cmp"))
    campaign.setdefault("status", "draft")
    campaign.setdefault("sheet_url", "")
    # A missing gid means "use whatever the sheet URL points at" (the URL's
    # ?gid=/#gid= is parsed later). 0 is a real gid, so never invent it here.
    campaign.setdefault("sheet_gid", None)
    _repair_fabricated_gid(campaign)
    campaign.setdefault("sheet_tab", "")
    campaign.setdefault("mapping", dict(DEFAULT_MAPPING))
    campaign.setdefault("sender_id", "")
    campaign.setdefault("template_id", "")
    campaign.setdefault("brand_id", "")
    campaign.setdefault("timezone", "UTC")
    campaign.setdefault("use_ai", False)
    campaign.setdefault("ai_prompt", "")
    campaign.setdefault("weekdays_only", False)
    campaign.setdefault("auto_complete", True)
    campaign.setdefault("unsubscribe_base_url", "")
    campaign.setdefault("created_at", _iso())
    campaign["hourly_limit"] = _int(
        campaign.get("hourly_limit"), config.CAMPAIGN_DEFAULT_HOURLY_LIMIT, 1, 100
    )
    campaign["daily_limit"] = _int(
        campaign.get("daily_limit"), config.CAMPAIGN_DEFAULT_DAILY_LIMIT, 1, 500
    )
    campaign["delay_min_minutes"] = _float(
        campaign.get("delay_min_minutes"), float(config.CAMPAIGN_DEFAULT_DELAY_MIN), 1, 240
    )
    campaign["delay_max_minutes"] = max(
        campaign["delay_min_minutes"],
        _float(
            campaign.get("delay_max_minutes"),
            float(config.CAMPAIGN_DEFAULT_DELAY_MAX),
            1,
            240,
        ),
    )
    campaign["work_start_hour"] = _int(
        campaign.get("work_start_hour"), 9, 0, 23
    )
    campaign["work_end_hour"] = _int(
        campaign.get("work_end_hour"), 18, 1, 24
    )
    if campaign["work_end_hour"] <= campaign["work_start_hour"]:
        campaign["work_end_hour"] = min(24, campaign["work_start_hour"] + 1)
    mapping = campaign.get("mapping")
    if not isinstance(mapping, dict) or not mapping:
        mapping = dict(DEFAULT_MAPPING)
    campaign["mapping"] = {
        field: str(mapping.get(field) or "") for field in TARGET_FIELDS
    }
    return campaign


def _timezone(campaign: dict[str, Any]) -> ZoneInfo:
    name = str(campaign.get("timezone") or "UTC").strip()
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("UTC")


def _within_working_hours(campaign: dict[str, Any], moment: datetime) -> bool:
    local = moment.astimezone(_timezone(campaign))
    if campaign.get("weekdays_only") and local.weekday() >= 5:
        return False
    start = int(campaign.get("work_start_hour", 9))
    end = int(campaign.get("work_end_hour", 18))
    return start <= local.hour < end


def _window_start(campaign: dict[str, Any], moment: datetime) -> datetime:
    """Returns the next moment inside the campaign's working window."""
    if _within_working_hours(campaign, moment):
        return moment
    tz = _timezone(campaign)
    local = moment.astimezone(tz)
    start_hour = int(campaign.get("work_start_hour", 9))
    for day_offset in range(0, 9):
        day = (local + timedelta(days=day_offset)).replace(
            hour=start_hour, minute=random.randint(0, 9), second=0, microsecond=0
        )
        if day_offset == 0 and day <= local:
            continue
        if campaign.get("weekdays_only") and day.weekday() >= 5:
            continue
        return day.astimezone(timezone.utc)
    return moment


def _next_send_time(campaign: dict[str, Any], base: datetime) -> datetime:
    low = float(campaign.get("delay_min_minutes", 4))
    high = float(campaign.get("delay_max_minutes", 12))
    delay = random.uniform(low, high)
    candidate = base + timedelta(minutes=delay)
    if not _within_working_hours(campaign, candidate):
        candidate = _window_start(campaign, candidate) + timedelta(
            minutes=random.randint(0, 5)
        )
    return candidate


def _initial_send_time(campaign: dict[str, Any]) -> datetime:
    now = _now()
    # The first message of a campaign can go out immediately; subsequent
    # messages get the randomized delay window.
    return now if _within_working_hours(campaign, now) else _window_start(campaign, now)


# ---------------------------------------------------------------------------
# Public views
# ---------------------------------------------------------------------------

def public_sender(sender: dict[str, Any]) -> dict[str, Any]:
    view = {key: value for key, value in sender.items() if key != "password"}
    view["has_password"] = bool(sender.get("password"))
    view["domain"] = (sender.get("email") or "").rsplit("@", 1)[-1].lower()
    return view


def _sender_caps_allow(
    state: dict[str, Any], sender: dict[str, Any], now: datetime
) -> bool:
    hourly_cap = int(sender.get("hourly_limit") or 0)
    daily_cap = int(sender.get("daily_limit") or 0)
    if not hourly_cap and not daily_cap:
        return True
    sender_id = sender.get("id")
    stamps = [
        _parse_dt(recipient.get("sent_at"))
        for recipient in state["recipients"]
        if recipient.get("sender_id") == sender_id
        and recipient.get("status") in SENT_STATUSES
    ]
    stamps = [stamp for stamp in stamps if stamp]
    if hourly_cap and sum(1 for stamp in stamps if stamp > now - timedelta(hours=1)) >= hourly_cap:
        return False
    if daily_cap:
        local_today = now.astimezone(_timezone({"timezone": sender.get("timezone") or "UTC"})).date()
        todays = [stamp for stamp in stamps if stamp.astimezone(_timezone({"timezone": sender.get("timezone") or "UTC"})).date() == local_today]
        if len(todays) >= daily_cap:
            return False
    return True


def _campaign_counts(state: dict[str, Any], campaign: dict[str, Any]) -> dict[str, int]:
    counts = Counter(
        recipient.get("status", "queued")
        for recipient in state["recipients"]
        if recipient.get("campaign_id") == campaign.get("id")
    )
    sent = sum(counts[status] for status in SENT_STATUSES)
    total = sum(counts.values())
    return {
        "total": total,
        "queued": counts["queued"],
        "scheduled": counts["scheduled"],
        "sending": counts["sending"],
        "stale": counts["stale"],
        "sent": sent,
        "delivered": counts["sent"],
        "inboxed": counts["inboxed"],
        "spam_alert": counts["spam_alert"],
        "bounced": counts["bounced"],
        "failed": counts["failed"],
        "skipped": counts["skipped"],
        "remaining": counts["queued"] + counts["scheduled"],
    }


def campaign_summary(state: dict[str, Any], campaign: dict[str, Any]) -> dict[str, Any]:
    sender = _find(state["senders"], campaign.get("sender_id", ""))
    template = _find(state["templates"], campaign.get("template_id", ""))
    brand = _find(state["brands"], campaign.get("brand_id", ""))
    recipients = [
        recipient
        for recipient in state["recipients"]
        if recipient.get("campaign_id") == campaign.get("id")
    ]
    scheduled = [
        recipient
        for recipient in recipients
        if recipient.get("status") == "scheduled" and recipient.get("scheduled_at")
    ]
    scheduled.sort(key=lambda recipient: recipient.get("scheduled_at", ""))
    counts = _campaign_counts(state, campaign)
    recent = [
        event
        for event in state["events"]
        if event.get("campaign_id") == campaign.get("id")
    ][-5:]
    summary = copy.deepcopy(campaign)
    summary["mapping"] = dict(campaign.get("mapping") or {})
    summary["stats"] = counts
    summary["sender_email"] = sender.get("email", "") if sender else ""
    summary["sender_name"] = sender.get("name", "") if sender else ""
    summary["template_name"] = template.get("name", "") if template else ""
    summary["brand_name"] = brand.get("name", "") if brand else ""
    summary["next_send_at"] = scheduled[0].get("scheduled_at") if scheduled else ""
    summary["recent_events"] = recent
    return summary


def list_campaigns() -> list[dict[str, Any]]:
    state = load_state()
    return [campaign_summary(state, campaign) for campaign in state["campaigns"]]


# ---------------------------------------------------------------------------
# Send-level helpers
# ---------------------------------------------------------------------------

PLACEHOLDER_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")
PLACEHOLDER_LABELS = {
    "firstname": "first_name",
    "first": "first_name",
    "name": "first_name",
    "position": "position",
    "jobtitle": "position",
    "title": "position",
    "role": "position",
    "companyname": "company_name",
    "company": "company_name",
    "organization": "company_name",
    "personalemail": "personal_email",
    "businessemail": "business_email",
    "email": "email",
    "brandname": "brand_name",
    "brand": "brand_name",
    "branddomain": "brand_domain",
    "website": "brand_website",
    "sendername": "sender_name",
    "senderemail": "sender_email",
}


def _placeholder_key(raw: str) -> str:
    compact = re.sub(r"[^a-z0-9]", "", raw.lower())
    return PLACEHOLDER_LABELS.get(compact, compact)


def render_template(
    template: str,
    recipient: dict[str, Any],
    campaign: dict[str, Any],
    brand: dict[str, Any] | None = None,
    sender: dict[str, Any] | None = None,
) -> str:
    brand = brand or {}
    sender = sender or {}
    values = {
        "first_name": recipient.get("first_name") or "there",
        "position": recipient.get("position", ""),
        "company_name": recipient.get("company_name", ""),
        "personal_email": recipient.get("personal_email", ""),
        "business_email": recipient.get("business_email", ""),
        "email": recipient.get("email", ""),
        "brand_name": brand.get("name") or campaign.get("name", ""),
        "brand_domain": brand.get("domain", ""),
        "brand_website": brand.get("website", ""),
        "sender_name": sender.get("name") or config.SENDER_NAME,
        "sender_email": sender.get("email", ""),
    }

    def replace(match: re.Match) -> str:
        return str(values.get(_placeholder_key(match.group(1)), ""))

    rendered = PLACEHOLDER_RE.sub(replace, template or "")
    # Collapse the double spaces left by empty optional values.
    return re.sub(r"[ \t]{2,}", " ", rendered).strip()


def _unsubscribe_base(campaign: dict[str, Any]) -> str:
    return (campaign.get("unsubscribe_base_url") or config.APP_BASE_URL or "").rstrip("/")


# ---------------------------------------------------------------------------
# Queue: claim, send, finish
# ---------------------------------------------------------------------------

def _assert_queue_backend() -> None:
    """Refuses to run the queue without a shared backend outside development.

    The file fallback keeps state in one process-local file with a threading lock,
    so two instances (or two cron invocations) would claim the same recipient and
    send it twice. That is safe for a single local process only.
    """
    if config.IS_DEVELOPMENT or config.DATABASE_URL:
        return
    raise RuntimeError(
        "DATABASE_URL is required outside development: the file-backed queue "
        f"({config.OUTREACH_FILE}) cannot coordinate claims between processes, so "
        "concurrent workers or cron ticks would double-send the same recipient. "
        "Set DATABASE_URL to a shared PostgreSQL database, or run with "
        "APP_ENV=development for a single local process."
    )


def claim_due(per_campaign: int = 1) -> list[dict[str, Any]]:
    """Atomically claims due recipients and marks them as sending."""
    now = _now()

    def mutator(state: dict[str, Any]) -> list[dict[str, Any]]:
        claims: list[dict[str, Any]] = []
        limits = _sending_limits(state)
        usage = _sending_usage(state, now)
        remaining = min(
            limits["hourly_limit"] - usage["sent_last_hour"],
            limits["daily_limit"] - usage["sent_today"],
        )
        if remaining <= 0:
            return claims
        for campaign in state["campaigns"]:
            if len(claims) >= remaining:
                break
            if campaign.get("status") != "running":
                continue
            if not _within_working_hours(campaign, now):
                continue
            sender = _find(state["senders"], campaign.get("sender_id", ""))
            template = _find(state["templates"], campaign.get("template_id", ""))
            if not sender or not template:
                continue
            if not _sender_caps_allow(state, sender, now):
                continue

            recipients = [
                recipient
                for recipient in state["recipients"]
                if recipient.get("campaign_id") == campaign.get("id")
            ]
            for recipient in recipients:
                if recipient.get("status") != "sending":
                    continue
                claimed_at = _parse_dt(recipient.get("claimed_at"))
                if not claimed_at:
                    continue
                age = now - claimed_at
                if age > timedelta(minutes=UNCONFIRMED_CLAIM_MINUTES):
                    recipient["status"] = "failed"
                    recipient["error"] = "unconfirmed"
                    recipient["updated_at"] = _iso(now)
                    _add_event(
                        state,
                        campaign,
                        recipient,
                        "failed",
                        "Send result was never confirmed after the second window; "
                        "reported failed as unconfirmed (it may still have been delivered).",
                        now,
                    )
                elif age > timedelta(minutes=STALE_CLAIM_MINUTES):
                    # Not failed yet: hold the row out of retry and out of the
                    # failed count until the second window expires.
                    recipient["status"] = "stale"
                    recipient["error"] = "Awaiting send confirmation."
                    recipient["updated_at"] = _iso(now)
                    _add_event(
                        state,
                        campaign,
                        recipient,
                        "stale",
                        "Send is unconfirmed past the first window; not reported "
                        "as failed until the second window expires.",
                        now,
                    )

            sent_stamps = [
                _parse_dt(recipient.get("sent_at"))
                for recipient in recipients
                if recipient.get("status") in SENT_STATUSES
            ]
            sent_stamps = [stamp for stamp in sent_stamps if stamp]
            tz = _timezone(campaign)
            today = now.astimezone(tz).date()
            hour_count = sum(1 for stamp in sent_stamps if stamp > now - timedelta(hours=1))
            day_count = sum(1 for stamp in sent_stamps if stamp.astimezone(tz).date() == today)
            if hour_count >= int(campaign.get("hourly_limit", 1)):
                continue
            if day_count >= int(campaign.get("daily_limit", 1)):
                continue

            scheduled = [
                recipient
                for recipient in recipients
                if recipient.get("status") == "scheduled"
            ]
            due = [
                recipient
                for recipient in scheduled
                if (_parse_dt(recipient.get("scheduled_at")) or now) <= now
            ]
            due.sort(key=lambda recipient: recipient.get("scheduled_at", ""))
            if not due:
                if scheduled or any(
                    recipient.get("status") == "sending" for recipient in recipients
                ):
                    # A future send is already planned, or one is in flight.
                    continue
                queued = [
                    recipient
                    for recipient in recipients
                    if recipient.get("status") == "queued"
                ]
                queued.sort(key=lambda recipient: (recipient.get("created_at", ""), recipient.get("id", "")))
                if not queued:
                    if campaign.get("auto_complete") and recipients and all(
                        recipient.get("status") in TERMINAL_STATUSES
                        for recipient in recipients
                    ):
                        campaign["status"] = "completed"
                        campaign["completed_at"] = _iso(now)
                    continue
                due = [queued[0]]
                due[0]["status"] = "scheduled"
                due[0]["scheduled_at"] = _iso(now)

            for _ in range(per_campaign):
                if not due or len(claims) >= remaining:
                    break
                recipient = due.pop(0)
                recipient["status"] = "sending"
                recipient["sender_id"] = sender.get("id", "")
                recipient["claimed_at"] = _iso(now)
                recipient["attempts"] = int(recipient.get("attempts") or 0) + 1
                recipient["updated_at"] = _iso(now)
                campaign["last_activity_at"] = _iso(now)
                brand = _find(state["brands"], campaign.get("brand_id", "")) or {}
                claims.append(
                    {
                        "campaign": copy.deepcopy(campaign),
                        "recipient": copy.deepcopy(recipient),
                        "sender": copy.deepcopy(sender),
                        "template": copy.deepcopy(template),
                        "brand": copy.deepcopy(brand),
                    }
                )
        return claims

    return _mutate(mutator)


def _add_event(
    state: dict[str, Any],
    campaign: dict[str, Any] | None,
    recipient: dict[str, Any] | None,
    event_type: str,
    message: str,
    moment: datetime | None = None,
) -> None:
    event = {
        "id": _new_id("evt"),
        "campaign_id": (campaign or {}).get("id", ""),
        "recipient_id": (recipient or {}).get("id", ""),
        "email": (recipient or {}).get("email", ""),
        "type": event_type,
        "message": (message or "")[:500],
        "at": _iso(moment),
    }
    state["events"].append(event)
    if len(state["events"]) > MAX_EVENTS:
        state["events"] = state["events"][-MAX_EVENTS:]


def finish_send(
    campaign_id: str,
    recipient_id: str,
    status: str,
    error: str = "",
    subject: str = "",
) -> dict[str, Any]:
    now = _now()

    def mutator(state: dict[str, Any]) -> dict[str, Any]:
        campaign = _find(state["campaigns"], campaign_id)
        recipient = _find(state["recipients"], recipient_id)
        if not campaign or not recipient:
            return {"ok": False}
        recipient["status"] = status
        recipient["updated_at"] = _iso(now)
        recipient["error"] = (error or "")[:500]
        if status in SENT_STATUSES:
            recipient["sent_at"] = _iso(now)
        if subject:
            recipient["subject"] = subject[:300]
        campaign["last_activity_at"] = _iso(now)
        _add_event(state, campaign, recipient, status, error or subject, now)

        recipients = [
            item
            for item in state["recipients"]
            if item.get("campaign_id") == campaign_id
        ]
        if not any(item.get("status") == "scheduled" for item in recipients):
            queued = [
                item for item in recipients if item.get("status") == "queued"
            ]
            queued.sort(key=lambda item: (item.get("created_at", ""), item.get("id", "")))
            if queued and campaign.get("status") == "running":
                next_time = _next_send_time(campaign, now)
                queued[0]["status"] = "scheduled"
                queued[0]["scheduled_at"] = _iso(next_time)
                queued[0]["updated_at"] = _iso(now)
            elif (
                campaign.get("auto_complete")
                and recipients
                and all(item.get("status") in TERMINAL_STATUSES for item in recipients)
            ):
                campaign["status"] = "completed"
                campaign["completed_at"] = _iso(now)
        return {"ok": True, "status": status}

    return _mutate(mutator)


def process_queue(max_sends: int = 10) -> dict[str, Any]:
    """Sends every due message once. Safe to call from a worker or a cron."""
    _assert_queue_backend()
    claims = claim_due()
    results: list[dict[str, Any]] = []
    for claim in claims[:max_sends]:
        campaign = claim["campaign"]
        recipient = claim["recipient"]
        sender = claim["sender"]
        template = claim["template"]
        brand = claim["brand"]
        email = (recipient.get("email") or "").strip().lower()

        try:
            if not email or not is_valid_format(email):
                finish_send(campaign["id"], recipient["id"], "skipped", "Invalid email address.")
                results.append({"email": email, "status": "skipped"})
                continue
            try:
                if suppressions.is_suppressed(email):
                    finish_send(
                        campaign["id"],
                        recipient["id"],
                        "skipped",
                        "Recipient is on the suppression list.",
                    )
                    results.append({"email": email, "status": "skipped"})
                    continue
            except Exception:
                logger.exception("Suppression lookup failed for %s", email)
                finish_send(
                    campaign["id"],
                    recipient["id"],
                    "failed",
                    "Could not verify the suppression list, so the send was skipped.",
                )
                results.append({"email": email, "status": "failed"})
                continue

            subject = render_template(
                template.get("subject", ""), recipient, campaign, brand, sender
            )
            body = render_template(
                template.get("body", ""), recipient, campaign, brand, sender
            )
            if campaign.get("use_ai"):
                try:
                    from .llm import generate_campaign_email

                    generated = generate_campaign_email(
                        recipient, campaign, template, brand, sender
                    )
                    subject = generated.get("subject") or subject
                    body = generated.get("body") or body
                except Exception as error:
                    logger.warning("AI personalization failed for %s: %s", email, error)

            if not subject.strip() or not body.strip():
                finish_send(
                    campaign["id"],
                    recipient["id"],
                    "failed",
                    "Template produced an empty subject or body.",
                )
                results.append({"email": email, "status": "failed"})
                continue

            unsubscribe_url = ""
            base_url = _unsubscribe_base(campaign)
            if base_url:
                try:
                    unsubscribe_url = build_unsubscribe_url(email, base_url)
                except Exception:
                    logger.exception("Could not build unsubscribe link for %s", email)

            html = wrap_in_html(
                body, sender.get("name") or config.SENDER_NAME, unsubscribe_url or None
            )
            sending_account = decrypt_sender_account(sender)
            sent, send_error, kind = send_account_email(
                sending_account,
                email,
                subject,
                html,
                body,
                unsubscribe_url=unsubscribe_url or None,
            )
            status = "sent" if sent else ("bounced" if kind == "bounced" else "failed")
            finish_send(
                campaign["id"], recipient["id"], status, "" if sent else send_error, subject
            )
            results.append({"email": email, "status": status, "error": send_error})
        except Exception as error:
            logger.exception("Campaign send crashed for %s", email)
            try:
                finish_send(
                    campaign["id"],
                    recipient["id"],
                    "failed",
                    f"Unexpected send error: {error}",
                )
            except Exception:
                logger.exception("Could not record failed send for %s", email)
            results.append({"email": email, "status": "failed", "error": str(error)})
    return {"processed": len(results), "results": results}


# ---------------------------------------------------------------------------
# Campaign lifecycle
# ---------------------------------------------------------------------------

def _schedule_first(campaign: dict[str, Any], state: dict[str, Any], now: datetime) -> None:
    if not _within_working_hours(campaign, now):
        # Nothing can be claimed before the window opens, so leave rows queued:
        # "scheduled" would imply a send that is about to happen. claim_due()
        # promotes the first queued row once the window is open.
        return
    recipients = [
        recipient
        for recipient in state["recipients"]
        if recipient.get("campaign_id") == campaign.get("id")
    ]
    if any(
        recipient.get("status") in {"scheduled", "sending"} for recipient in recipients
    ):
        return
    queued = [recipient for recipient in recipients if recipient.get("status") == "queued"]
    queued.sort(key=lambda recipient: (recipient.get("created_at", ""), recipient.get("id", "")))
    if queued:
        queued[0]["status"] = "scheduled"
        queued[0]["scheduled_at"] = _iso(_initial_send_time(campaign))
        queued[0]["updated_at"] = _iso(now)


def start_campaign(campaign_id: str) -> dict[str, Any]:
    now = _now()

    def mutator(state: dict[str, Any]) -> dict[str, Any]:
        campaign = _require(state["campaigns"], campaign_id, "Campaign")
        sender = _find(state["senders"], campaign.get("sender_id", ""))
        if not sender:
            raise OutreachError("Assign a sender account before starting the campaign.")
        if not _find(state["templates"], campaign.get("template_id", "")):
            raise OutreachError("Assign an email template before starting the campaign.")
        recipients = [
            recipient
            for recipient in state["recipients"]
            if recipient.get("campaign_id") == campaign_id
        ]
        if not recipients:
            raise OutreachError("This campaign has no leads yet. Sync the Google Sheet first.")
        if not any(recipient.get("status") == "queued" for recipient in recipients) and not any(
            recipient.get("status") == "scheduled" for recipient in recipients
        ):
            raise OutreachError("There are no queued leads left to send. Sync the sheet or requeue failed leads.")
        campaign["status"] = "running"
        campaign["started_at"] = campaign.get("started_at") or _iso(now)
        campaign["paused_at"] = ""
        campaign["last_activity_at"] = _iso(now)
        _schedule_first(campaign, state, now)
        return campaign_summary(state, campaign)

    return _mutate(mutator)


def pause_campaign(campaign_id: str) -> dict[str, Any]:
    now = _now()

    def mutator(state: dict[str, Any]) -> dict[str, Any]:
        campaign = _require(state["campaigns"], campaign_id, "Campaign")
        if campaign.get("status") != "draft":
            campaign["status"] = "paused"
            campaign["paused_at"] = _iso(now)
        return campaign_summary(state, campaign)

    return _mutate(mutator)


def requeue(campaign_id: str, statuses: set[str] | None = None) -> dict[str, Any]:
    # Never retry rows that may already have been delivered: a retry would send
    # a second copy. "stale" rows are excluded until they become "failed".
    statuses = (
        set(RETRYABLE_STATUSES)
        if statuses is None
        else {status for status in statuses if status in RETRYABLE_STATUSES}
    )
    now = _now()

    def mutator(state: dict[str, Any]) -> dict[str, Any]:
        campaign = _require(state["campaigns"], campaign_id, "Campaign")
        moved = 0
        for recipient in state["recipients"]:
            if recipient.get("campaign_id") == campaign_id and recipient.get("status") in statuses:
                recipient["status"] = "queued"
                recipient["error"] = ""
                recipient["scheduled_at"] = ""
                recipient["attempts"] = 0
                recipient["updated_at"] = _iso(now)
                moved += 1
        _schedule_first(campaign, state, now)
        return {"requeued": moved, "campaign": campaign_summary(state, campaign)}

    return _mutate(mutator)


def add_leads(campaign_id: str, leads: list[dict[str, Any]]) -> dict[str, Any]:
    """Adds mapped sheet leads to a campaign, skipping duplicates and invalid rows."""
    now = _now()

    def mutator(state: dict[str, Any]) -> dict[str, Any]:
        campaign = _require(state["campaigns"], campaign_id, "Campaign")
        existing = {
            (recipient.get("email") or "").lower()
            for recipient in state["recipients"]
            if recipient.get("campaign_id") == campaign_id
        }
        if len(state["recipients"]) + len(leads) > MAX_RECIPIENTS:
            raise OutreachError(
                f"This workspace can hold at most {MAX_RECIPIENTS} leads. "
                "Archive or delete an old campaign first.",
                409,
            )
        added = 0
        duplicates = 0
        invalid = 0
        for lead in leads:
            email = (lead.get("email") or "").strip().lower()
            if not is_valid_format(email):
                invalid += 1
                continue
            if email in existing:
                duplicates += 1
                continue
            existing.add(email)
            state["recipients"].append(
                {
                    "id": _new_id("rcp"),
                    "campaign_id": campaign_id,
                    "sender_id": campaign.get("sender_id", ""),
                    "first_name": str(lead.get("first_name") or "")[:100],
                    "position": str(lead.get("position") or "")[:150],
                    "company_name": str(lead.get("company_name") or "")[:150],
                    "personal_email": str(lead.get("personal_email") or "")[:320],
                    "business_email": str(lead.get("business_email") or "")[:320],
                    "email": email,
                    "source_row": lead.get("source_row"),
                    "status": "queued",
                    "scheduled_at": "",
                    "sent_at": "",
                    "attempts": 0,
                    "error": "",
                    "subject": "",
                    "created_at": _iso(now),
                    "updated_at": _iso(now),
                }
            )
            added += 1
        campaign["last_sync_at"] = _iso(now)
        campaign["last_sync_added"] = added
        if campaign.get("status") == "running":
            _schedule_first(campaign, state, now)
        return {
            "added": added,
            "duplicates": duplicates,
            "invalid": invalid,
            "campaign": campaign_summary(state, campaign),
        }

    return _mutate(mutator)


# ---------------------------------------------------------------------------
# CRUD: senders, brands, templates, campaigns
# ---------------------------------------------------------------------------

SENDER_PROVIDERS = {"smtp", "gmail", "zoho"}
# Microsoft disabled basic SMTP auth for Exchange Online, so Outlook/M365 needs
# OAuth, which is not implemented. Existing rows stay editable and testable but
# no new outlook sender can be created.
LEGACY_SENDER_PROVIDERS = {"outlook"}
SENDER_SPF_INCLUDES = {
    "gmail": "include:_spf.google.com",
    "zoho": "include:zoho.com",
    "outlook": "include:spf.protection.outlook.com",
    "smtp": "",
}
DMARC_POLICIES = {"none", "quarantine", "reject"}
# Underscores are required by real includes such as include:_spf.google.com.
SPF_INCLUDE_RE = re.compile(r"^include:[A-Za-z0-9._-]+$")


def _sender_dns_value(
    payload: dict[str, Any],
    existing: dict[str, Any],
    key: str,
    provider: str,
) -> str:
    """Resolve a per-domain DNS field without collapsing "absent" into "empty".

    A key that is absent (or explicitly null) leaves the stored value alone; an
    explicit "" clears the value. Using `or` here would resurrect the provider
    default on the next partial update.
    """
    if key in payload and payload[key] is not None:
        return str(payload[key])
    stored = existing.get(key)
    if stored is not None:
        return str(stored)
    return SENDER_SPF_INCLUDES.get(provider, "") if key == "spf_include" else "none"


def _clean_sender(payload: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    existing = existing or {}
    email = str(payload.get("email") or existing.get("email") or "").strip().lower()
    if not email or not is_valid_format(email):
        raise OutreachError("Enter a valid sender email address.")
    provider = str(payload.get("provider") or existing.get("provider") or "smtp").strip().lower()
    if provider in LEGACY_SENDER_PROVIDERS:
        if existing.get("provider") != provider:
            raise OutreachError(
                "Microsoft 365 disabled basic SMTP auth, so new Outlook senders need OAuth, "
                "which is not supported yet. Use a Gmail/Zoho account or a custom SMTP host.",
                422,
            )
    elif provider not in SENDER_PROVIDERS:
        provider = "smtp"
    name = str(payload.get("name") or existing.get("name") or email.split("@")[0]).strip()[:100]
    host = str(payload.get("host", existing.get("host", "")) or "").strip()
    if provider == "gmail":
        host = host or "smtp.gmail.com"
    elif provider == "zoho":
        host = host or "smtp.zoho.com"
    elif provider == "outlook":
        host = host or "smtp.office365.com"
    if not host:
        raise OutreachError("SMTP host is required for a custom account.")
    try:
        port = int(payload.get("port", existing.get("port", 465)) or 465)
    except (TypeError, ValueError):
        port = 465
    security = str(payload.get("security", existing.get("security", "ssl")) or "ssl").lower()
    if security not in {"ssl", "starttls"}:
        security = "ssl"
    password = payload.get("password")
    if password is None or password == "":
        # Keep the stored ciphertext when the dashboard submits an empty
        # password field (the API never returns the secret).
        password = existing.get("password", "")
    else:
        password = credential_crypto.encrypt(str(password))
    username = str(
        payload.get("username", existing.get("username", email)) or email
    ).strip()
    # DNS authentication is configured per sending domain (see _sender_dns_value).
    spf_include = _sender_dns_value(payload, existing, "spf_include", provider).strip()[:255]
    if spf_include and not SPF_INCLUDE_RE.match(spf_include):
        raise OutreachError(
            "SPF include must be a single include:provider.tld entry; "
            "ip4:/ip6: tokens and multiple includes are not supported.",
            422,
        )
    dmarc_policy = _sender_dns_value(payload, existing, "dmarc_policy", provider).strip().lower()
    if dmarc_policy and dmarc_policy not in DMARC_POLICIES:
        dmarc_policy = "none"
    return {
        "email": email,
        "name": name,
        "provider": provider,
        "host": host,
        "port": max(1, min(65535, port)),
        "security": security,
        "username": username,
        "password": str(password or ""),
        "brand": str(payload.get("brand", existing.get("brand", "")) or "").strip()[:100],
        "dkim_selector": str(
            payload.get("dkim_selector", existing.get("dkim_selector", "")) or ""
        ).strip().lower()[:100],
        "spf_include": spf_include,
        "dmarc_policy": dmarc_policy,
        "timezone": str(payload.get("timezone", existing.get("timezone", "UTC")) or "UTC"),
        "hourly_limit": _int(payload.get("hourly_limit", existing.get("hourly_limit", 0)), 0, 0, 100),
        "daily_limit": _int(payload.get("daily_limit", existing.get("daily_limit", 0)), 0, 0, 1000),
    }


def create_sender(payload: dict[str, Any]) -> dict[str, Any]:
    sender = _clean_sender(payload)
    if not sender["password"]:
        raise OutreachError("An app password is required for the sender account.")
    now = _iso()

    def mutator(state: dict[str, Any]) -> dict[str, Any]:
        if any(item.get("email") == sender["email"] for item in state["senders"]):
            raise OutreachError("A sender with this email already exists.", 409)
        sender.update({"id": _new_id("snd"), "created_at": now, "updated_at": now})
        state["senders"].append(sender)
        return public_sender(sender)

    return _mutate(mutator)


def update_sender(sender_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    now = _iso()

    def mutator(state: dict[str, Any]) -> dict[str, Any]:
        existing = _require(state["senders"], sender_id, "Sender")
        cleaned = _clean_sender(payload, existing)
        if any(
            item.get("email") == cleaned["email"] and item.get("id") != sender_id
            for item in state["senders"]
        ):
            raise OutreachError("Another sender already uses this email.", 409)
        cleaned.update(
            {"id": sender_id, "created_at": existing.get("created_at", now), "updated_at": now}
        )
        existing.clear()
        existing.update(cleaned)
        return public_sender(existing)

    return _mutate(mutator)


def delete_sender(sender_id: str) -> dict[str, Any]:
    def mutator(state: dict[str, Any]) -> dict[str, Any]:
        _require(state["senders"], sender_id, "Sender")
        if any(campaign.get("sender_id") == sender_id for campaign in state["campaigns"]):
            raise OutreachError(
                "This sender is assigned to a campaign. Reassign or delete that campaign first.",
                409,
            )
        state["senders"] = [item for item in state["senders"] if item.get("id") != sender_id]
        return {"deleted": sender_id}

    return _mutate(mutator)


def get_sender(sender_id: str) -> dict[str, Any]:
    state = load_state()
    return public_sender(_require(state["senders"], sender_id, "Sender"))


def get_campaign(campaign_id: str) -> dict[str, Any]:
    state = load_state()
    return campaign_summary(state, _require(state["campaigns"], campaign_id, "Campaign"))


def _persist_migrated_password(sender_id: str, ciphertext: str) -> None:
    """Writes a re-encrypted credential back so the legacy key is no longer needed."""
    def mutator(state: dict[str, Any]) -> None:
        sender = _find(state["senders"], sender_id)
        if sender and sender.get("password") != ciphertext:
            sender["password"] = ciphertext
            sender["updated_at"] = _iso()

    try:
        _mutate(mutator)
    except Exception:
        logger.exception(
            "Could not persist the re-encrypted credential for sender %s; it will be "
            "migrated again on the next send.",
            sender_id,
        )


def decrypt_sender_account(
    account: dict[str, Any], *, persist: bool = True
) -> dict[str, Any]:
    """Returns a copy with the stored secret decrypted for SMTP use only.

    A value that only decoded under SENDER_CREDENTIAL_KEY_LEGACY is re-encrypted
    under the current key and written back, so the legacy key can be removed
    after one deploy. Raises CredentialDecryptError naming the sender when no
    configured key works, instead of sending with an empty password.
    """
    sender_id = str(account.get("id") or account.get("email") or "unknown")
    decrypted = copy.deepcopy(account)
    plaintext, migrated = credential_crypto.decrypt_and_migrate(
        account.get("password"), sender_id
    )
    decrypted["password"] = plaintext
    if migrated and persist and account.get("id"):
        _persist_migrated_password(str(account["id"]), migrated)
    return decrypted


def audit_sender_credentials() -> dict[str, list[str]]:
    """Logs (never the secrets) which senders still need a legacy key or are unreadable."""
    if credential_crypto.legacy_key_is_public():
        # Deliberately a warning, not a refusal: an operator mid-migration must
        # still be able to start the app and rotate credentials.
        logger.error(
            "SECURITY: SENDER_CREDENTIAL_KEY_LEGACY is set to the pre-hardening built-in "
            "fallback secret, which is public in this repository's git history. This is a "
            "compromise path, not a recovery path: anything decrypted with it was never "
            "protected. Unset SENDER_CREDENTIAL_KEY_LEGACY, rotate every sender password at "
            "the provider, and save the new passwords in the campaign console. See README "
            "'Upgrading from a pre-SESSION_SECRET deploy' (Case B)."
        )
    state = load_state()
    items = [
        (str(sender.get("id") or ""), sender.get("password"))
        for sender in state["senders"]
    ]
    legacy_ids = credential_crypto.legacy_encrypted_ids(items)
    broken_ids = credential_crypto.undecryptable_ids(items)
    if legacy_ids:
        logger.warning(
            "%s sender credential(s) are still encrypted with a legacy key and will be "
            "re-encrypted on their next send: %s. Keep SENDER_CREDENTIAL_KEY_LEGACY set "
            "until this list is empty, then unset it.",
            len(legacy_ids),
            ", ".join(legacy_ids),
        )
    if broken_ids:
        logger.error(
            "%s sender credential(s) cannot be decrypted with the current key or any "
            "legacy key and must be re-entered: %s.",
            len(broken_ids),
            ", ".join(broken_ids),
        )
    return {"legacy": legacy_ids, "undecryptable": broken_ids}


def get_sender_account(sender_id: str) -> dict[str, Any]:
    """Internal use only: includes decrypted credentials for SMTP actions."""
    state = load_state()
    sender = _require(state["senders"], sender_id, "Sender")
    return decrypt_sender_account(sender)


def create_brand(payload: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("name") or "").strip()
    if not name:
        raise OutreachError("Brand name is required.")
    now = _iso()
    brand = {
        "id": _new_id("brd"),
        "name": name[:100],
        "domain": str(payload.get("domain") or "").strip().lower()[:200],
        "website": str(payload.get("website") or "").strip()[:300],
        "description": str(payload.get("description") or "").strip()[:1000],
        "signature": str(payload.get("signature") or "").strip()[:2000],
        "created_at": now,
        "updated_at": now,
    }

    def mutator(state: dict[str, Any]) -> dict[str, Any]:
        if any(item.get("name", "").lower() == name.lower() for item in state["brands"]):
            raise OutreachError("A brand with this name already exists.", 409)
        state["brands"].append(brand)
        return brand

    return _mutate(mutator)


def update_brand(brand_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    def mutator(state: dict[str, Any]) -> dict[str, Any]:
        brand = _require(state["brands"], brand_id, "Brand")
        name = str(payload.get("name", brand.get("name")) or "").strip()
        if not name:
            raise OutreachError("Brand name is required.")
        brand.update(
            {
                "name": name[:100],
                "domain": str(payload.get("domain", brand.get("domain", "")) or "").strip().lower()[:200],
                "website": str(payload.get("website", brand.get("website", "")) or "").strip()[:300],
                "description": str(payload.get("description", brand.get("description", "")) or "").strip()[:1000],
                "signature": str(payload.get("signature", brand.get("signature", "")) or "").strip()[:2000],
                "updated_at": _iso(),
            }
        )
        return brand

    return _mutate(mutator)


def delete_brand(brand_id: str) -> dict[str, Any]:
    def mutator(state: dict[str, Any]) -> dict[str, Any]:
        _require(state["brands"], brand_id, "Brand")
        if any(campaign.get("brand_id") == brand_id for campaign in state["campaigns"]):
            raise OutreachError(
                "This brand is used by a campaign. Reassign or delete that campaign first.",
                409,
            )
        state["brands"] = [item for item in state["brands"] if item.get("id") != brand_id]
        return {"deleted": brand_id}

    return _mutate(mutator)


def create_template(payload: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("name") or "").strip()
    subject = str(payload.get("subject") or "").strip()
    body = str(payload.get("body") or "").strip()
    if not name:
        raise OutreachError("Template name is required.")
    if not subject or not body:
        raise OutreachError("Template subject and body are required.")
    now = _iso()
    template = {
        "id": _new_id("tpl"),
        "name": name[:100],
        "brand_id": str(payload.get("brand_id") or ""),
        "subject": subject[:300],
        "body": body[:20000],
        "prompt": str(payload.get("prompt") or "").strip()[:5000],
        "created_at": now,
        "updated_at": now,
    }

    def mutator(state: dict[str, Any]) -> dict[str, Any]:
        state["templates"].append(template)
        return template

    return _mutate(mutator)


def update_template(template_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    def mutator(state: dict[str, Any]) -> dict[str, Any]:
        template = _require(state["templates"], template_id, "Template")
        name = str(payload.get("name", template.get("name")) or "").strip()
        subject = str(payload.get("subject", template.get("subject")) or "").strip()
        body = str(payload.get("body", template.get("body")) or "").strip()
        if not name or not subject or not body:
            raise OutreachError("Template name, subject, and body are required.")
        template.update(
            {
                "name": name[:100],
                "brand_id": str(payload.get("brand_id", template.get("brand_id", "")) or ""),
                "subject": subject[:300],
                "body": body[:20000],
                "prompt": str(payload.get("prompt", template.get("prompt", "")) or "").strip()[:5000],
                "updated_at": _iso(),
            }
        )
        return template

    return _mutate(mutator)


def delete_template(template_id: str) -> dict[str, Any]:
    def mutator(state: dict[str, Any]) -> dict[str, Any]:
        _require(state["templates"], template_id, "Template")
        if any(campaign.get("template_id") == template_id for campaign in state["campaigns"]):
            raise OutreachError(
                "This template is used by a campaign. Reassign or delete that campaign first.",
                409,
            )
        state["templates"] = [item for item in state["templates"] if item.get("id") != template_id]
        return {"deleted": template_id}

    return _mutate(mutator)


def create_campaign(payload: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("name") or "").strip()
    if not name:
        raise OutreachError("Campaign name is required.")
    now = _iso()
    sheet_url = str(payload.get("sheet_url") or "").strip()[:1000]
    campaign = {
        "id": _new_id("cmp"),
        "name": name[:120],
        "sheet_url": sheet_url,
        "sheet_gid": _resolve_sheet_gid(payload.get("sheet_gid"), sheet_url),
        "sheet_tab": str(payload.get("sheet_tab") or "").strip()[:100],
        "sender_id": str(payload.get("sender_id") or ""),
        "template_id": str(payload.get("template_id") or ""),
        "brand_id": str(payload.get("brand_id") or ""),
        "timezone": str(payload.get("timezone") or "UTC").strip()[:80],
        "hourly_limit": payload.get("hourly_limit"),
        "daily_limit": payload.get("daily_limit"),
        "delay_min_minutes": payload.get("delay_min_minutes"),
        "delay_max_minutes": payload.get("delay_max_minutes"),
        "work_start_hour": payload.get("work_start_hour"),
        "work_end_hour": payload.get("work_end_hour"),
        "weekdays_only": bool(payload.get("weekdays_only")),
        "use_ai": bool(payload.get("use_ai")),
        "ai_prompt": str(payload.get("ai_prompt") or "").strip()[:5000],
        "auto_complete": bool(payload.get("auto_complete", True)),
        "unsubscribe_base_url": str(payload.get("unsubscribe_base_url") or "").strip()[:500],
        "mapping": payload.get("mapping") or dict(DEFAULT_MAPPING),
        "status": "draft",
        "created_at": now,
        "updated_at": now,
    }
    _campaign_defaults(campaign)

    def mutator(state: dict[str, Any]) -> dict[str, Any]:
        if campaign["sender_id"] and not _find(state["senders"], campaign["sender_id"]):
            raise OutreachError("Selected sender account does not exist.")
        if campaign["template_id"] and not _find(state["templates"], campaign["template_id"]):
            raise OutreachError("Selected template does not exist.")
        state["campaigns"].append(campaign)
        return campaign_summary(state, campaign)

    return _mutate(mutator)


def update_campaign(campaign_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    def mutator(state: dict[str, Any]) -> dict[str, Any]:
        campaign = _require(state["campaigns"], campaign_id, "Campaign")
        if campaign.get("status") == "running":
            locked = {
                "hourly_limit",
                "daily_limit",
                "delay_min_minutes",
                "delay_max_minutes",
                "work_start_hour",
                "work_end_hour",
                "timezone",
            }
            if locked & set(payload):
                raise OutreachError(
                    "Pause the campaign before changing its rate limits or schedule.", 409
                )
        for field in (
            "name",
            "sheet_url",
            "sheet_tab",
            "sender_id",
            "template_id",
            "brand_id",
            "timezone",
            "ai_prompt",
            "unsubscribe_base_url",
        ):
            if field in payload:
                campaign[field] = str(payload.get(field) or "")[:5000]
        # Resolve after sheet_url is written so a changed URL is honoured.
        if "sheet_gid" in payload:
            campaign["sheet_gid"] = _resolve_sheet_gid(
                payload.get("sheet_gid"), campaign.get("sheet_url", "")
            )
        elif "sheet_url" in payload:
            campaign["sheet_gid"] = _resolve_sheet_gid(
                None, campaign.get("sheet_url", "")
            )
        for field in ("weekdays_only", "use_ai", "auto_complete"):
            if field in payload:
                campaign[field] = bool(payload.get(field))
        for field in (
            "hourly_limit",
            "daily_limit",
            "delay_min_minutes",
            "delay_max_minutes",
            "work_start_hour",
            "work_end_hour",
        ):
            if field in payload:
                campaign[field] = payload.get(field)
        if "mapping" in payload:
            campaign["mapping"] = payload.get("mapping") or {}
        campaign["updated_at"] = _iso()
        _campaign_defaults(campaign)
        return campaign_summary(state, campaign)

    return _mutate(mutator)


def delete_campaign(campaign_id: str) -> dict[str, Any]:
    def mutator(state: dict[str, Any]) -> dict[str, Any]:
        _require(state["campaigns"], campaign_id, "Campaign")
        state["campaigns"] = [
            item for item in state["campaigns"] if item.get("id") != campaign_id
        ]
        state["recipients"] = [
            item for item in state["recipients"] if item.get("campaign_id") != campaign_id
        ]
        state["events"] = [
            item for item in state["events"] if item.get("campaign_id") != campaign_id
        ]
        return {"deleted": campaign_id}

    return _mutate(mutator)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def campaign_report(campaign_id: str, limit: int = 100) -> dict[str, Any]:
    state = load_state()
    campaign = _require(state["campaigns"], campaign_id, "Campaign")
    recipients = [
        recipient
        for recipient in state["recipients"]
        if recipient.get("campaign_id") == campaign_id
    ]
    recipients.sort(key=lambda recipient: recipient.get("updated_at", ""), reverse=True)
    events = [
        event for event in state["events"] if event.get("campaign_id") == campaign_id
    ][-50:][::-1]
    counts = _campaign_counts(state, campaign)
    attempted = counts["sent"] + counts["bounced"] + counts["failed"]
    deliverability = {
        "bounce_rate": round(counts["bounced"] / attempted * 100, 1) if attempted else 0.0,
        "failure_rate": round(counts["failed"] / attempted * 100, 1) if attempted else 0.0,
        "inbox_rate": round(counts["inboxed"] / attempted * 100, 1) if attempted else 0.0,
        "spam_rate": round(counts["spam_alert"] / attempted * 100, 1) if attempted else 0.0,
    }
    daily: dict[str, int] = {}
    for recipient in recipients:
        sent_at = _parse_dt(recipient.get("sent_at"))
        if recipient.get("status") in SENT_STATUSES and sent_at:
            key = sent_at.date().isoformat()
            daily[key] = daily.get(key, 0) + 1
    series = [
        {"date": key, "sent": daily.get(key, 0)}
        for key in sorted(daily)
    ][-14:]
    return {
        "campaign": campaign_summary(state, campaign),
        "counts": counts,
        "deliverability": deliverability,
        "recipients": recipients[: max(1, min(500, limit))],
        "events": events,
        "series": series,
    }


def overview() -> dict[str, Any]:
    state = load_state()
    campaigns = [campaign_summary(state, campaign) for campaign in state["campaigns"]]
    counts = Counter(recipient.get("status", "queued") for recipient in state["recipients"])
    now = _now()
    today = now.date()
    sent_today = sum(
        1
        for recipient in state["recipients"]
        if recipient.get("status") in SENT_STATUSES
        and (_parse_dt(recipient.get("sent_at")) or now).date() == today
    )
    attempted = sum(counts[status] for status in SENT_STATUSES) + counts["bounced"] + counts["failed"]
    return {
        "campaigns": campaigns,
        "totals": {
            "campaigns": len(campaigns),
            "running": sum(1 for campaign in campaigns if campaign["status"] == "running"),
            "leads": sum(counts.values()),
            "queued": counts["queued"] + counts["scheduled"],
            "sending": counts["sending"],
            "stale": counts["stale"],
            "sent": sum(counts[status] for status in SENT_STATUSES),
            "sent_today": sent_today,
            "bounced": counts["bounced"],
            "failed": counts["failed"],
            "skipped": counts["skipped"],
            "inboxed": counts["inboxed"],
            "spam_alert": counts["spam_alert"],
            "bounce_rate": round(counts["bounced"] / attempted * 100, 1) if attempted else 0.0,
            "spam_rate": round(counts["spam_alert"] / attempted * 100, 1) if attempted else 0.0,
        },
        "limits": dict(_sending_limits(state)),
        "limits_usage": _sending_usage(state, now),
        "senders": [public_sender(sender) for sender in state["senders"]],
        "brands": state["brands"],
        "templates": state["templates"],
        "recent_events": state["events"][-30:][::-1],
        "queue_worker": bool(_worker_thread and _worker_thread.is_alive()),
        "google_sheets_configured": bool(
            config.GOOGLE_SHEETS_API_KEY
            or config.GOOGLE_SERVICE_ACCOUNT_JSON
            or config.GOOGLE_SERVICE_ACCOUNT_FILE
        ),
        "queue_tick_secret_configured": bool(config.QUEUE_TICK_SECRET),
    }


def mark_recipient(recipient_id: str, status: str) -> dict[str, Any]:
    """Records inbox/spam feedback coming from a webhook or manual review."""
    allowed = {"inboxed", "spam_alert", "bounced", "sent", "failed"}
    if status not in allowed:
        raise OutreachError("Unsupported status update.")
    now = _now()

    def mutator(state: dict[str, Any]) -> dict[str, Any]:
        recipient = _require(state["recipients"], recipient_id, "Recipient")
        recipient["status"] = status
        recipient["updated_at"] = _iso(now)
        campaign = _find(state["campaigns"], recipient.get("campaign_id", ""))
        _add_event(
            state,
            campaign,
            recipient,
            status,
            f"Marked {status} from reporting.",
            now,
        )
        return {"ok": True, "recipient": recipient}

    return _mutate(mutator)


def mark_by_email(
    email: str, status: str, campaign_id: str = ""
) -> dict[str, Any]:
    """Applies provider feedback (inbox/spam/bounce) by recipient address."""
    allowed = {"inboxed", "spam_alert", "bounced", "sent", "failed", "skipped"}
    if status not in allowed:
        raise OutreachError("Unsupported status update.")
    target = (email or "").strip().lower()
    if not target:
        raise OutreachError("An email address is required.")
    now = _now()

    def mutator(state: dict[str, Any]) -> dict[str, Any]:
        matches = [
            recipient
            for recipient in state["recipients"]
            if (recipient.get("email") or "").lower() == target
            and (not campaign_id or recipient.get("campaign_id") == campaign_id)
        ]
        if not matches:
            raise OutreachError("No campaign recipient matches that address.", 404)
        matches.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
        recipient = matches[0]
        recipient["status"] = status
        recipient["updated_at"] = _iso(now)
        campaign = _find(state["campaigns"], recipient.get("campaign_id", ""))
        _add_event(
            state,
            campaign,
            recipient,
            status,
            f"Provider feedback received: {status}.",
            now,
        )
        return {"ok": True, "recipient": recipient}

    return _mutate(mutator)


# ---------------------------------------------------------------------------
# Queue worker
# ---------------------------------------------------------------------------

def _audit_sheets_credentials() -> None:
    """One-time startup sanity check for the Google Sheets import path.

    When a service account is configured the operator can copy its
    ``client_email`` into the sheet's share dialog; otherwise the app falls
    back to the public CSV export and the sheet must be shared with
    "Anyone with the link".
    """
    from .sheets import _service_account_info

    info = _service_account_info()
    if info and info.get("client_email"):
        logger.info(
            "Google Sheets: service account configured for private sheets. "
            "Share the sheet with %s (client_email) and use the sheet URL or tab name in the campaign.",
            info["client_email"],
        )
        return
    logger.info(
        "Google Sheets: no service account configured. The app is using the public CSV export path, "
        "so every imported sheet must be shared as 'Anyone with the link' (View). "
        "If the sheet contains personal emails, share it with a Google service account instead "
        "and set GOOGLE_SERVICE_ACCOUNT_JSON (or GOOGLE_SERVICE_ACCOUNT_FILE) — the public CSV path sends PII over the open export URL."
    )


def _worker_loop() -> None:
    logger.info("Outreach queue worker started")
    while not _worker_stop.wait(5):
        try:
            result = process_queue()
            if result.get("processed"):
                logger.info("Outreach queue processed %s messages", result["processed"])
        except Exception:
            logger.exception("Outreach queue tick failed")
        _worker_stop.wait(max(5, config.QUEUE_TICK_SECONDS - 5))


def start_worker() -> None:
    global _worker_thread, _startup_audit_done
    # The audit is read-only, so it runs even where the worker is disabled (Vercel
    # uses the cron tick) and every operator still sees legacy or compromised
    # credentials. It runs at most once per process.
    if not _startup_audit_done:
        _startup_audit_done = True
        try:
            audit_sender_credentials()
        except Exception:
            logger.exception("Sender credential audit failed; continuing with startup")
        _audit_sheets_credentials()
    if not config.QUEUE_WORKER_ENABLED or os.getenv("VERCEL"):
        return
    # Only guard a worker that will actually run in-process: a Vercel deploy with
    # the worker disabled must serve every route instead of failing at startup,
    # and its cron path is guarded in process_queue().
    _assert_queue_backend()
    with _worker_lock:
        if _worker_thread and _worker_thread.is_alive():
            return
        _worker_stop.clear()
        _worker_thread = threading.Thread(
            target=_worker_loop, name="outreach-queue", daemon=True
        )
        _worker_thread.start()


def stop_worker() -> None:
    global _worker_thread
    _worker_stop.set()
    with _worker_lock:
        if _worker_thread and _worker_thread.is_alive():
            _worker_thread.join(timeout=2)
        _worker_thread = None
