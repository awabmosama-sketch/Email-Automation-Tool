"""Brand profiles, templates, campaigns, leads, and campaign reporting."""

from __future__ import annotations

import copy
from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import automation_store as store
from .. import config, sheets
from . import scheduling, templating
from .senders import get_sender, public_sender
from ..verifier import is_valid_format

CAMPAIGN_STATUSES = ("draft", "running", "paused", "completed")
TEMPLATE_TYPES = ("static", "ai")
OUTCOMES = ("inbox", "spam", "bounced", "replied", "cleared")


class CampaignError(Exception):
    """Validation or workflow error with an optional details payload."""

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.details = details or {}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _safe_timezone(value: str) -> str:
    candidate = _clean(value) or config.CAMPAIGN_TIMEZONE or "UTC"
    try:
        ZoneInfo(candidate)
        return candidate
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return "UTC"


def _hhmm(value: Any, default: str) -> str:
    raw = _clean(value) or default
    try:
        hour_text, minute_text = raw.split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return f"{hour:02d}:{minute:02d}"
    except (ValueError, TypeError):
        pass
    return default


def _days(value: Any) -> list[int]:
    if value is None:
        return [0, 1, 2, 3, 4]
    days: list[int] = []
    for item in value:
        try:
            day = int(item)
        except (TypeError, ValueError):
            continue
        if 0 <= day <= 6 and day not in days:
            days.append(day)
    return sorted(days) or [0, 1, 2, 3, 4]


def _column_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, str] = {}
    for field in sheets.LEAD_FIELDS:
        header = _clean(value.get(field))
        if header:
            result[field] = header[:150]
    return result


# ---------------------------------------------------------------------------
# Brands
# ---------------------------------------------------------------------------


def normalize_brand(payload: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    existing = existing or {}
    name = _clean(payload.get("name", existing.get("name")))
    if not name:
        raise CampaignError("Brand name is required.")
    website = _clean(payload.get("website", existing.get("website")))
    sending_domain = _clean(payload.get("sending_domain", existing.get("sending_domain"))).lower()
    if not sending_domain and website:
        sending_domain = website.replace("https://", "").replace("http://", "").split("/")[0].lower()
    return {
        "id": existing.get("id") or store.new_id("brd"),
        "name": name[:100],
        "website": website[:300],
        "sending_domain": sending_domain[:200],
        "from_name": _clean(payload.get("from_name", existing.get("from_name")))[:100],
        "signature": _clean(payload.get("signature", existing.get("signature")))[:2000],
        "description": _clean(payload.get("description", existing.get("description")))[:1000],
    }


def list_brands() -> list[dict[str, Any]]:
    return store.list_docs("brands")


def create_brand(payload: dict[str, Any]) -> dict[str, Any]:
    return store.upsert_doc("brands", normalize_brand(payload))


def update_brand(brand_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    existing = store.get_doc("brands", brand_id)
    if not existing:
        raise CampaignError("Brand not found.")
    brand = normalize_brand(payload, existing)
    brand["id"] = brand_id
    return store.upsert_doc("brands", brand)


def delete_brand(brand_id: str) -> None:
    used = store.query_docs("campaigns", equals={"brand_id": brand_id})
    if used:
        raise CampaignError("This brand is used by a campaign. Reassign or delete those campaigns first.")
    store.delete_doc("brands", brand_id)


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


def normalize_template(
    payload: dict[str, Any],
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    existing = existing or {}
    name = _clean(payload.get("name", existing.get("name")))
    if not name:
        raise CampaignError("Template name is required.")
    template_type = _clean(payload.get("type", existing.get("type", "static"))).lower()
    if template_type not in TEMPLATE_TYPES:
        template_type = "static"
    body = _clean(payload.get("body", existing.get("body")))
    prompt = _clean(payload.get("prompt", existing.get("prompt")))
    subject = _clean(payload.get("subject", existing.get("subject")))
    if template_type == "static" and not body:
        raise CampaignError("A static template needs a body.")
    if template_type == "ai" and not prompt:
        raise CampaignError("An AI template needs a prompt with instructions for the model.")
    return {
        "id": existing.get("id") or store.new_id("tpl"),
        "name": name[:120],
        "brand_id": _clean(payload.get("brand_id", existing.get("brand_id"))),
        "type": template_type,
        "subject": subject[:300],
        "body": body[:20000],
        "prompt": prompt[:12000],
        "tone": _clean(payload.get("tone", existing.get("tone", "friendly-professional")))[:100],
        "signature": _clean(payload.get("signature", existing.get("signature")))[:2000],
    }


def list_templates() -> list[dict[str, Any]]:
    return store.list_docs("templates")


def create_template(payload: dict[str, Any]) -> dict[str, Any]:
    return store.upsert_doc("templates", normalize_template(payload))


def update_template(template_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    existing = store.get_doc("templates", template_id)
    if not existing:
        raise CampaignError("Template not found.")
    template = normalize_template(payload, existing)
    template["id"] = template_id
    return store.upsert_doc("templates", template)


def delete_template(template_id: str) -> None:
    used = store.query_docs("campaigns", equals={"template_id": template_id})
    if used:
        raise CampaignError("This template is used by a campaign. Reassign those campaigns first.")
    store.delete_doc("templates", template_id)


# ---------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------


def normalize_campaign(
    payload: dict[str, Any],
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    existing = existing or {}
    name = _clean(payload.get("name", existing.get("name")))
    if not name:
        raise CampaignError("Campaign name is required.")

    sender_id = _clean(payload.get("sender_id", existing.get("sender_id")))
    template_id = _clean(payload.get("template_id", existing.get("template_id")))
    brand_id = _clean(payload.get("brand_id", existing.get("brand_id")))
    if sender_id and not get_sender(sender_id):
        raise CampaignError("Choose a valid sender account.")
    if template_id and not store.get_doc("templates", template_id):
        raise CampaignError("Choose a valid template.")
    if brand_id and not store.get_doc("brands", brand_id):
        raise CampaignError("Choose a valid brand.")

    delay_min = _int(payload.get("delay_min_seconds", existing.get("delay_min_seconds", 240)), 240, 1, 21600)
    delay_max = _int(payload.get("delay_max_seconds", existing.get("delay_max_seconds", 720)), 720, 1, 21600)
    if delay_min > delay_max:
        delay_min, delay_max = delay_max, delay_min

    window_start = _hhmm(payload.get("send_window_start", existing.get("send_window_start")), "09:00")
    window_end = _hhmm(payload.get("send_window_end", existing.get("send_window_end")), "18:00")
    if window_start >= window_end:
        raise CampaignError("The send window start must be earlier than the end time.")

    status = _clean(payload.get("status", existing.get("status", "draft"))).lower()
    if status not in CAMPAIGN_STATUSES:
        status = "draft"

    return {
        "id": existing.get("id") or store.new_id("cmp"),
        "name": name[:150],
        "brand_id": brand_id,
        "sender_id": sender_id,
        "template_id": template_id,
        "sheet_url": _clean(payload.get("sheet_url", existing.get("sheet_url")))[:1000],
        "sheet_tab": _clean(payload.get("sheet_tab", existing.get("sheet_tab")))[:200],
        "has_header": _bool(payload.get("has_header", existing.get("has_header", True)), True),
        "column_map": _column_map(payload.get("column_map", existing.get("column_map", {}))),
        "prefer_personal": _bool(
            payload.get("prefer_personal", existing.get("prefer_personal", False)), False
        ),
        "hourly_limit": _int(payload.get("hourly_limit", existing.get("hourly_limit", 8)), 8, 1, 100),
        "daily_limit": _int(payload.get("daily_limit", existing.get("daily_limit", 40)), 40, 1, 500),
        "delay_min_seconds": delay_min,
        "delay_max_seconds": delay_max,
        "send_window_start": window_start,
        "send_window_end": window_end,
        "send_days": _days(payload.get("send_days", existing.get("send_days"))),
        "timezone": _safe_timezone(_clean(payload.get("timezone", existing.get("timezone")))),
        "send_window_enabled": _bool(
            payload.get("send_window_enabled", existing.get("send_window_enabled", True)), True
        ),
        "require_dns_pass": _bool(
            payload.get("require_dns_pass", existing.get("require_dns_pass", True)), True
        ),
        "dedupe_across_campaigns": _bool(
            payload.get("dedupe_across_campaigns", existing.get("dedupe_across_campaigns", True)), True
        ),
        "spam_score_limit": _int(
            payload.get("spam_score_limit", existing.get("spam_score_limit", 8)), 8, 1, 10
        ),
        "status": status,
        "next_send_at": existing.get("next_send_at", ""),
        "last_sync_at": existing.get("last_sync_at", ""),
        "last_sync_stats": existing.get("last_sync_stats", {}),
        "last_error": existing.get("last_error", ""),
        "last_send_at": existing.get("last_send_at", ""),
    }


def list_campaigns() -> list[dict[str, Any]]:
    return store.list_docs("campaigns")


def get_campaign(campaign_id: str) -> dict[str, Any] | None:
    return store.get_doc("campaigns", campaign_id)


def create_campaign(payload: dict[str, Any]) -> dict[str, Any]:
    campaign = normalize_campaign({**payload, "status": "draft"})
    return store.upsert_doc("campaigns", campaign)


def update_campaign(campaign_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    existing = get_campaign(campaign_id)
    if not existing:
        raise CampaignError("Campaign not found.")
    merged = {**payload}
    # Status transitions happen through start/pause/resume/complete so the DNS
    # gate and queue setup cannot be skipped.
    merged.pop("status", None)
    campaign = normalize_campaign(merged, existing)
    campaign["id"] = campaign_id
    return store.upsert_doc("campaigns", campaign)


def delete_campaign(campaign_id: str) -> None:
    if not get_campaign(campaign_id):
        raise CampaignError("Campaign not found.")
    store.delete_docs("leads", {"campaign_id": campaign_id})
    store.delete_docs("jobs", {"campaign_id": campaign_id})
    store.delete_docs("events", {"campaign_id": campaign_id})
    store.delete_doc("campaigns", campaign_id)


def start_campaign(campaign_id: str, *, force_dns: bool = False) -> dict[str, Any]:
    campaign = get_campaign(campaign_id)
    if not campaign:
        raise CampaignError("Campaign not found.")
    if not campaign.get("sender_id"):
        raise CampaignError("Assign a sender account before starting this campaign.")
    if not campaign.get("template_id"):
        raise CampaignError("Assign a template before starting this campaign.")

    leads = store.query_docs("leads", equals={"campaign_id": campaign_id})
    if not leads:
        if not campaign.get("sheet_url"):
            raise CampaignError("Connect a Google Sheet and sync leads before starting.")
        sync_campaign_leads(campaign_id)
        leads = store.query_docs("leads", equals={"campaign_id": campaign_id})
    if not leads:
        raise CampaignError("No valid leads were imported from the sheet.")
    if not any(lead.get("status") in {"pending", "queued"} for lead in leads) and not any(
        job.get("status") in {"queued", "running"}
        for job in store.query_docs("jobs", equals={"campaign_id": campaign_id})
    ):
        raise CampaignError("Every lead has already been processed. Sync new rows to continue.")

    if campaign.get("require_dns_pass") and not force_dns:
        sender = get_sender(campaign["sender_id"])
        if not sender:
            raise CampaignError("The assigned sender no longer exists.")
        from .senders import sender_dns

        dns = sender_dns(sender["id"])
        if dns.get("overall") != "pass":
            raise CampaignError(
                "DNS authentication is not passing for the sender domain. "
                "Fix SPF, DKIM, and DMARC, or disable 'Require DNS pass' to override.",
                {
                    "dns": {
                        "domain": dns.get("domain"),
                        "overall": dns.get("overall"),
                        "spf": dns.get("spf", {}).get("status"),
                        "dkim": dns.get("dkim", {}).get("status"),
                        "dmarc": dns.get("dmarc", {}).get("status"),
                    }
                },
            )

    now = scheduling.utc_now()
    campaign["status"] = "running"
    campaign["last_error"] = ""
    campaign["next_send_at"] = scheduling.iso(scheduling.next_window_start(campaign, now))
    store.upsert_doc("campaigns", campaign)

    # Re-time already queued jobs that may have been parked by a pause.
    for job in store.query_docs("jobs", equals={"campaign_id": campaign_id, "status": "queued"}):
        job["scheduled_at"] = scheduling.iso(now)
        store.upsert_doc("jobs", job)

    return campaign


def pause_campaign(campaign_id: str) -> dict[str, Any]:
    campaign = get_campaign(campaign_id)
    if not campaign:
        raise CampaignError("Campaign not found.")
    campaign["status"] = "paused"
    store.upsert_doc("campaigns", campaign)
    return campaign


def resume_campaign(campaign_id: str, *, force_dns: bool = False) -> dict[str, Any]:
    return start_campaign(campaign_id, force_dns=force_dns)


def complete_campaign(campaign_id: str) -> dict[str, Any]:
    campaign = get_campaign(campaign_id)
    if not campaign:
        raise CampaignError("Campaign not found.")
    campaign["status"] = "completed"
    store.upsert_doc("campaigns", campaign)
    for job in store.query_docs("jobs", equals={"campaign_id": campaign_id, "status": "queued"}):
        job["status"] = "cancelled"
        store.upsert_doc("jobs", job)
    return campaign


# ---------------------------------------------------------------------------
# Leads / Google Sheets sync
# ---------------------------------------------------------------------------


def lead_target_email(lead: dict[str, Any], campaign: dict[str, Any]) -> str:
    personal = _clean(lead.get("personal_email")).lower()
    business = _clean(lead.get("business_email")).lower()
    if campaign.get("prefer_personal"):
        return personal or business
    return business or personal


def sync_campaign_leads(
    campaign_id: str,
    *,
    column_map: dict[str, str] | None = None,
    sheet_tab: str | None = None,
    has_header: bool | None = None,
) -> dict[str, Any]:
    campaign = get_campaign(campaign_id)
    if not campaign:
        raise CampaignError("Campaign not found.")
    if not campaign.get("sheet_url"):
        raise CampaignError("Connect a Google Sheet URL first.")

    tab = campaign.get("sheet_tab", "") if sheet_tab is None else sheet_tab
    header_flag = campaign.get("has_header", True) if has_header is None else has_header
    try:
        table = sheets.fetch_table(campaign["sheet_url"], sheet_tab=tab, has_header=header_flag)
    except sheets.SheetError as error:
        raise CampaignError(str(error)) from error

    mapping = column_map or campaign.get("column_map") or sheets.auto_map_headers(table["headers"])
    try:
        extracted = sheets.extract_leads(
            table, mapping, prefer_personal=bool(campaign.get("prefer_personal"))
        )
    except sheets.SheetError as error:
        raise CampaignError(str(error)) from error

    existing = store.query_docs("leads", equals={"campaign_id": campaign_id})
    by_email = {
        _clean(lead.get("email")).lower(): lead
        for lead in existing
        if _clean(lead.get("email"))
    }

    imported = 0
    updated = 0
    to_save: list[dict[str, Any]] = []
    for item in extracted["leads"]:
        email = item["email"].lower()
        current = by_email.get(email)
        if current:
            changed = False
            for field in ("first_name", "position", "company_name", "personal_email", "business_email"):
                if item.get(field) and item[field] != current.get(field):
                    current[field] = item[field]
                    changed = True
            if changed:
                updated += 1
                to_save.append(current)
            continue
        lead = {
            "id": store.new_id("led"),
            "campaign_id": campaign_id,
            "row_number": item["row_number"],
            "first_name": item["first_name"],
            "position": item["position"],
            "company_name": item["company_name"],
            "personal_email": item["personal_email"],
            "business_email": item["business_email"],
            "email": email,
            "status": "pending",
            "outcome": "",
            "outcome_note": "",
            "outcome_at": "",
            "error": "",
            "job_id": "",
            "sent_at": "",
            "attempts": 0,
        }
        by_email[email] = lead
        to_save.append(lead)
        imported += 1

    if to_save:
        store.upsert_docs("leads", to_save)

    stats = {
        "imported": imported,
        "updated": updated,
        "total_rows": extracted["total_rows"],
        "valid_rows": len(extracted["leads"]),
        "duplicates": extracted["duplicates"] + len(existing) - len(by_email) + imported,
        "skipped_count": len(extracted["skipped"]),
    }
    # Duplicate math: rows already present are neither imported nor updated.
    stats["duplicates"] = max(0, extracted["total_rows"] - imported - updated - len(extracted["skipped"]))

    campaign["column_map"] = mapping
    campaign["last_sync_at"] = store.now_iso()
    campaign["last_sync_stats"] = stats
    campaign["last_error"] = ""
    store.upsert_doc("campaigns", campaign)

    return {
        "campaign_id": campaign_id,
        "stats": stats,
        "column_map": mapping,
        "headers": table["headers"],
        "source": table["source"],
        "warnings": extracted["warnings"],
        "skipped": extracted["skipped"][:20],
        "sample": extracted["leads"][:10],
    }


def list_leads(campaign_id: str, *, status: str = "", limit: int = 500) -> list[dict[str, Any]]:
    equals = {"campaign_id": campaign_id}
    if status:
        equals["status"] = status
    return store.query_docs("leads", equals=equals, order_by="row_number", limit=max(1, min(2000, limit)))


def remove_lead(campaign_id: str, lead_id: str) -> None:
    lead = store.get_doc("leads", lead_id)
    if not lead or lead.get("campaign_id") != campaign_id:
        raise CampaignError("Lead not found.")
    store.delete_docs("jobs", {"campaign_id": campaign_id, "lead_id": lead_id, "status": "queued"})
    store.delete_doc("leads", lead_id)


def record_outcome(campaign_id: str, lead_id: str, outcome: str, note: str = "") -> dict[str, Any]:
    if outcome not in OUTCOMES:
        raise CampaignError("Outcome must be inbox, spam, bounced, replied, or cleared.")
    lead = store.get_doc("leads", lead_id)
    if not lead or lead.get("campaign_id") != campaign_id:
        raise CampaignError("Lead not found.")
    lead["outcome"] = "" if outcome == "cleared" else outcome
    lead["outcome_note"] = _clean(note)[:500]
    lead["outcome_at"] = store.now_iso() if outcome != "cleared" else ""
    store.upsert_doc("leads", lead)
    if outcome != "cleared":
        record_event(
            campaign_id,
            lead_id=lead_id,
            sender_id="",
            email=lead.get("email", ""),
            status=outcome,
            subject="",
            detail=note[:500],
        )
    return lead


# ---------------------------------------------------------------------------
# Events / reporting
# ---------------------------------------------------------------------------


def record_event(
    campaign_id: str,
    *,
    status: str,
    lead_id: str = "",
    sender_id: str = "",
    email: str = "",
    subject: str = "",
    detail: str = "",
    job_id: str = "",
) -> dict[str, Any]:
    return store.upsert_doc(
        "events",
        {
            "id": store.new_id("evt"),
            "campaign_id": campaign_id,
            "lead_id": lead_id,
            "sender_id": sender_id,
            "email": _clean(email).lower(),
            "status": status,
            "subject": _clean(subject)[:300],
            "detail": _clean(detail)[:1000],
            "job_id": job_id,
            "created_at": store.now_iso(),
        },
    )


def _event_counts(events: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        status = _clean(event.get("status")) or "unknown"
        counts[status] = counts.get(status, 0) + 1
    return counts


def campaign_metrics(campaign_id: str, *, days: int = 14) -> dict[str, Any]:
    campaign = get_campaign(campaign_id)
    if not campaign:
        raise CampaignError("Campaign not found.")
    days = max(1, min(90, days))

    leads = store.query_docs("leads", equals={"campaign_id": campaign_id})
    events = store.query_docs("events", equals={"campaign_id": campaign_id})
    jobs = store.query_docs("jobs", equals={"campaign_id": campaign_id})
    now = scheduling.utc_now()
    day_start = scheduling.local_day_start(campaign, now)
    hour_start = scheduling.hour_ago(now)

    lead_counts: dict[str, int] = {}
    for lead in leads:
        status = _clean(lead.get("status")) or "pending"
        lead_counts[status] = lead_counts.get(status, 0) + 1

    event_totals = _event_counts(events)
    sent_today = sum(
        1
        for event in events
        if event.get("status") == "sent" and str(event.get("created_at", "")) >= scheduling.iso(day_start)
    )
    sent_last_hour = sum(
        1
        for event in events
        if event.get("status") == "sent" and str(event.get("created_at", "")) >= scheduling.iso(hour_start)
    )

    tz = scheduling.get_timezone(campaign.get("timezone"))

    def local_date(event: dict[str, Any]) -> str:
        moment = scheduling.parse_iso(event.get("created_at", ""))
        return moment.astimezone(tz).date().isoformat() if moment else ""

    daily: dict[str, dict[str, int]] = {}
    for offset in range(days - 1, -1, -1):
        day = (now.astimezone(tz).date() - timedelta(days=offset)).isoformat()
        daily[day] = {"sent": 0, "failed": 0, "bounced": 0, "spam": 0, "inbox": 0, "replied": 0}
    for event in events:
        day = local_date(event)
        if day not in daily:
            continue
        status = event.get("status", "")
        if status in daily[day]:
            daily[day][status] += 1

    queued_jobs = [job for job in jobs if job.get("status") in {"queued", "running"}]
    next_scheduled = min(
        (str(job.get("scheduled_at")) for job in queued_jobs if job.get("scheduled_at")),
        default="",
    )

    recent = sorted(events, key=lambda item: str(item.get("created_at", "")), reverse=True)[:25]
    lead_lookup = {lead["id"]: lead for lead in leads}
    for event in recent:
        lead = lead_lookup.get(event.get("lead_id", ""))
        if lead:
            event["first_name"] = lead.get("first_name", "")
            event["company_name"] = lead.get("company_name", "")

    sender = get_sender(campaign.get("sender_id", ""))
    brand = store.get_doc("brands", campaign.get("brand_id", "")) if campaign.get("brand_id") else None
    template = (
        store.get_doc("templates", campaign.get("template_id", ""))
        if campaign.get("template_id")
        else None
    )

    return {
        "campaign": campaign,
        "sender": public_sender(sender) if sender else None,
        "brand": brand,
        "template": template,
        "leads": {"total": len(leads), "by_status": lead_counts},
        "events": event_totals,
        "queued_now": len(queued_jobs),
        "next_send_at": next_scheduled or campaign.get("next_send_at", ""),
        "sent_today": sent_today,
        "sent_last_hour": sent_last_hour,
        "sent_total": event_totals.get("sent", 0),
        "daily": [{"date": day, **values} for day, values in daily.items()],
        "recent_events": recent,
    }


def campaign_public(campaign: dict[str, Any], *, metrics: bool = True) -> dict[str, Any]:
    item = copy.deepcopy(campaign)
    sender = get_sender(campaign.get("sender_id", ""))
    item["sender"] = public_sender(sender) if sender else None
    item["brand"] = (
        store.get_doc("brands", campaign.get("brand_id", "")) if campaign.get("brand_id") else None
    )
    item["template"] = (
        store.get_doc("templates", campaign.get("template_id", ""))
        if campaign.get("template_id")
        else None
    )
    if metrics:
        leads = store.query_docs("leads", equals={"campaign_id": campaign["id"]})
        jobs = store.query_docs("jobs", equals={"campaign_id": campaign["id"]})
        item["summary"] = {
            "leads": len(leads),
            "pending": sum(1 for lead in leads if lead.get("status") == "pending"),
            "queued": sum(1 for job in jobs if job.get("status") in {"queued", "running"}),
            "sent": sum(1 for lead in leads if lead.get("status") == "sent"),
            "failed": sum(1 for lead in leads if lead.get("status") == "failed"),
        }
    return item


def dashboard_overview(*, days: int = 14) -> dict[str, Any]:
    campaigns = list_campaigns()
    all_events = store.list_docs("events")
    all_leads = store.list_docs("leads")
    jobs = store.list_docs("jobs")
    now = scheduling.utc_now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    hour_start = scheduling.hour_ago(now)

    today_iso = scheduling.iso(today_start)
    hour_iso = scheduling.iso(hour_start)

    sent_today = sum(
        1 for event in all_events if event.get("status") == "sent" and event.get("created_at", "") >= today_iso
    )
    sent_last_hour = sum(
        1 for event in all_events if event.get("status") == "sent" and event.get("created_at", "") >= hour_iso
    )
    counts = _event_counts(all_events)

    daily: dict[str, dict[str, int]] = {}
    for offset in range(days - 1, -1, -1):
        day = (now.date() - timedelta(days=offset)).isoformat()
        daily[day] = {"sent": 0, "failed": 0, "bounced": 0, "spam": 0, "inbox": 0, "replied": 0}
    for event in all_events:
        moment = scheduling.parse_iso(event.get("created_at", ""))
        day = moment.date().isoformat() if moment else ""
        if day in daily and event.get("status") in daily[day]:
            daily[day][event["status"]] += 1

    recent = sorted(all_events, key=lambda item: str(item.get("created_at", "")), reverse=True)[:30]
    campaign_lookup = {campaign["id"]: campaign for campaign in campaigns}
    lead_lookup = {lead["id"]: lead for lead in all_leads}
    for event in recent:
        campaign = campaign_lookup.get(event.get("campaign_id", ""))
        lead = lead_lookup.get(event.get("lead_id", ""))
        event["campaign_name"] = campaign.get("name", "") if campaign else ""
        event["first_name"] = lead.get("first_name", "") if lead else ""
        event["company_name"] = lead.get("company_name", "") if lead else ""

    return {
        "campaigns": [campaign_public(campaign) for campaign in campaigns],
        "totals": {
            "campaigns": len(campaigns),
            "active_campaigns": sum(1 for campaign in campaigns if campaign.get("status") == "running"),
            "leads": len(all_leads),
            "pending_leads": sum(1 for lead in all_leads if lead.get("status") == "pending"),
            "queued_now": sum(1 for job in jobs if job.get("status") in {"queued", "running"}),
            "sent_today": sent_today,
            "sent_last_hour": sent_last_hour,
            "sent": counts.get("sent", 0),
            "failed": counts.get("failed", 0),
            "bounced": counts.get("bounced", 0),
            "spam": counts.get("spam", 0),
            "inbox": counts.get("inbox", 0),
            "replied": counts.get("replied", 0),
            "skipped": counts.get("skipped", 0),
        },
        "daily": [{"date": day, **values} for day, values in daily.items()],
        "recent_events": recent,
    }


def lead_public(lead: dict[str, Any]) -> dict[str, Any]:
    """Never expose anything beyond the five mapped lead fields."""
    return {
        key: lead.get(key, "")
        for key in (
            "id",
            "campaign_id",
            "row_number",
            "first_name",
            "position",
            "company_name",
            "personal_email",
            "business_email",
            "email",
            "status",
            "outcome",
            "outcome_note",
            "outcome_at",
            "error",
            "sent_at",
            "attempts",
        )
    }


__all__ = [
    "CAMPAIGN_STATUSES",
    "CampaignError",
    "OUTCOMES",
    "TEMPLATE_TYPES",
    "campaign_metrics",
    "campaign_public",
    "complete_campaign",
    "create_brand",
    "create_campaign",
    "create_template",
    "dashboard_overview",
    "delete_brand",
    "delete_campaign",
    "delete_template",
    "get_campaign",
    "lead_public",
    "lead_target_email",
    "list_brands",
    "list_campaigns",
    "list_leads",
    "list_templates",
    "normalize_campaign",
    "pause_campaign",
    "record_event",
    "record_outcome",
    "remove_lead",
    "resume_campaign",
    "start_campaign",
    "sync_campaign_leads",
    "update_brand",
    "update_campaign",
    "update_template",
]
