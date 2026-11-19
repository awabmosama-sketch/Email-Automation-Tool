"""Durable, rate-limited campaign send queue.

The queue is stored in the same database as the campaign data. PostgreSQL uses
``FOR UPDATE SKIP LOCKED`` to claim jobs atomically, which is the serverless-safe
equivalent of a Redis/Celery worker: any number of web/worker processes can call
``run_tick`` without sending the same email twice.

Pacing rules implemented here:

* hourly and daily caps (campaign + sender, whichever is lower)
* randomized gaps between individual sends (default 4-12 minutes)
* local send windows (default 09:00-18:00, weekdays)
* one in-flight job per lead, with stale-job recovery
* DNS gate, suppression list, cross-campaign dedupe, and a local spam guard
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from . import automation_store as store
from .. import config, suppressions
from . import campaigns, scheduling, templating
from ..sender import send_with_settings, wrap_in_html
from .senders import SenderError, get_sender, resolve_settings, sender_dns
from ..unsubscribe import build_unsubscribe_url
from ..verifier import is_blocked_domain, is_valid_format

logger = logging.getLogger(__name__)

STALE_JOB_SECONDS = 900
MAX_ATTEMPTS = 3


def _sent_events(
    *,
    campaign_id: str = "",
    sender_id: str = "",
    since: str | None = None,
) -> list[dict[str, Any]]:
    equals = {"status": "sent"}
    if campaign_id:
        equals["campaign_id"] = campaign_id
    if sender_id:
        equals["sender_id"] = sender_id
    return store.query_docs("events", equals=equals, gte={"created_at": since} if since else None)


def effective_caps(campaign: dict[str, Any], sender: dict[str, Any] | None) -> dict[str, int]:
    campaign_hourly = int(campaign.get("hourly_limit") or 8)
    campaign_daily = int(campaign.get("daily_limit") or 40)
    if sender:
        return {
            "hourly": min(campaign_hourly, int(sender.get("hourly_limit") or campaign_hourly)),
            "daily": min(campaign_daily, int(sender.get("daily_limit") or campaign_daily)),
        }
    return {"hourly": campaign_hourly, "daily": campaign_daily}


def _limits_state(campaign: dict[str, Any], sender: dict[str, Any] | None) -> dict[str, Any]:
    now = scheduling.utc_now()
    hour_since = scheduling.iso(scheduling.hour_ago(now))
    day_since = scheduling.iso(scheduling.local_day_start(campaign, now))
    campaign_id = campaign["id"]
    sender_id = sender.get("id", "") if sender else ""

    campaign_hour_events = _sent_events(campaign_id=campaign_id, since=hour_since)
    campaign_day_events = _sent_events(campaign_id=campaign_id, since=day_since)
    sender_hour_events = _sent_events(sender_id=sender_id, since=hour_since) if sender_id else []
    sender_day_events = _sent_events(sender_id=sender_id, since=day_since) if sender_id else []

    caps = effective_caps(campaign, sender)
    return {
        "caps": caps,
        "campaign_hour": len(campaign_hour_events),
        "campaign_day": len(campaign_day_events),
        "sender_hour": len(sender_hour_events),
        "sender_day": len(sender_day_events),
        "hour_events": sorted(
            campaign_hour_events, key=lambda event: str(event.get("created_at", ""))
        ),
        "now": now,
    }


def _cap_retry_time(campaign: dict[str, Any], state: dict[str, Any]) -> tuple[str, str]:
    """Returns ``(iso_time, reason)`` when the next send may be attempted."""
    caps = state["caps"]
    if state["campaign_day"] >= caps["daily"] or (
        state["sender_day"] >= caps["daily"] and state.get("sender_hour") is not None
    ):
        return scheduling.iso(scheduling.next_cap_reset(campaign, state["now"])), "Daily limit reached"
    if state["campaign_hour"] >= caps["hourly"]:
        events = state["hour_events"]
        # The oldest event that must age out before another slot frees up.
        index = max(0, len(events) - caps["hourly"])
        if index < len(events):
            moment = scheduling.parse_iso(events[index].get("created_at", ""))
            if moment:
                return scheduling.iso(moment + timedelta(hours=1, seconds=5)), "Hourly limit reached"
        return scheduling.iso(state["now"] + timedelta(minutes=10)), "Hourly limit reached"
    return scheduling.iso(state["now"]), ""


def ensure_next_job(
    campaign: dict[str, Any],
    now: Any | None = None,
) -> dict[str, Any] | None:
    """Creates one queued job for the next pending lead, if none exists."""
    campaign_id = campaign["id"]
    existing = store.query_docs("jobs", equals={"campaign_id": campaign_id, "status": "queued"})
    running = store.query_docs("jobs", equals={"campaign_id": campaign_id, "status": "running"})
    if existing or running:
        return existing[0] if existing else None

    pending = store.query_docs(
        "leads",
        equals={"campaign_id": campaign_id, "status": "pending"},
        order_by="row_number",
        limit=1,
    )
    if not pending:
        return None

    lead = pending[0]
    moment = now or scheduling.utc_now()
    scheduled = scheduling.parse_iso(campaign.get("next_send_at", ""))
    if not scheduled or scheduled < moment:
        scheduled = moment

    job = {
        "id": f"job_{lead['id']}",
        "campaign_id": campaign_id,
        "lead_id": lead["id"],
        "status": "queued",
        "scheduled_at": scheduling.iso(scheduled),
        "attempts": int(lead.get("attempts") or 0),
        "error": "",
        "created_at": store.now_iso(),
    }
    store.upsert_doc("jobs", job)
    lead["status"] = "queued"
    lead["job_id"] = job["id"]
    store.upsert_doc("leads", lead)
    return job


def _reschedule_queued(campaign: dict[str, Any], when: str, reason: str = "") -> int:
    now = scheduling.iso(scheduling.utc_now())
    moved = 0
    for job in store.query_docs("jobs", equals={"campaign_id": campaign["id"], "status": "queued"}):
        if str(job.get("scheduled_at") or "") < when:
            job["scheduled_at"] = when
            store.upsert_doc("jobs", job)
            moved += 1
    campaign["next_send_at"] = when
    if reason:
        campaign["last_error"] = reason
    store.upsert_doc("campaigns", campaign)
    return moved


def _requeue(
    job: dict[str, Any],
    lead: dict[str, Any] | None,
    *,
    scheduled_at: str,
    error: str = "",
    decrement_attempt: bool = False,
) -> None:
    """Returns a job to the queue.

    Claiming increments ``attempts``. A real send/generation failure keeps that
    increment, while a pre-attempt deferral (cap, DNS, temporary lookup error)
    rolls it back so limits do not burn through retry attempts.
    """
    job["status"] = "queued"
    job["scheduled_at"] = scheduled_at
    job["error"] = error[:1000]
    if decrement_attempt:
        job["attempts"] = max(0, int(job.get("attempts") or 0) - 1)
    store.upsert_doc("jobs", job)
    if lead and lead.get("status") != "sent":
        lead["status"] = "queued"
        lead["error"] = error[:1000]
        store.upsert_doc("leads", lead)


def _finish_job(
    job: dict[str, Any],
    lead: dict[str, Any] | None,
    *,
    lead_status: str,
    error: str = "",
) -> None:
    job["status"] = "done" if lead_status in {"sent", "skipped"} else "failed"
    job["error"] = error[:1000]
    store.upsert_doc("jobs", job)
    if lead:
        lead["status"] = lead_status
        lead["error"] = error[:1000]
        lead["attempts"] = int(job.get("attempts") or 0)
        if lead_status == "sent":
            lead["sent_at"] = store.now_iso()
        store.upsert_doc("leads", lead)


def _render_email(
    campaign: dict[str, Any],
    template: dict[str, Any],
    lead: dict[str, Any],
    brand: dict[str, Any] | None,
    sender: dict[str, Any],
) -> dict[str, Any]:
    context = templating.build_context(lead, brand=brand, sender=sender)
    signature = (template.get("signature") or (brand or {}).get("signature") or "").strip()

    if template.get("type") == "ai":
        try:                    from ..llm import generate_campaign_email
        except ValueError as error:
            raise RuntimeError(f"AI template cannot run: {error}") from error
        prompt = templating.render_template(template.get("prompt", ""), context).strip()
        if not prompt:
            raise RuntimeError("The AI prompt renders to an empty string.")
        generated = generate_campaign_email(
            prompt=prompt,
            lead=lead,
            brand=brand or {},
            sender=sender,
            signature=signature,
            subject_template=templating.render_template(template.get("subject", ""), context),
            tone=template.get("tone") or "friendly-professional",
        )
        subject = templating.clean_subject(
            generated.get("subject") or templating.render_template(template.get("subject", ""), context)
        )
        body = (generated.get("body") or "").strip()
        if not body:
            raise RuntimeError("The AI returned an empty email body.")
        body = templating.apply_signature(body, signature)
    else:
        subject = templating.clean_subject(
            templating.render_template(template.get("subject", ""), context)
        )
        body = templating.render_template(template.get("body", ""), context).strip()
        body = templating.apply_signature(body, signature)

    if not subject:
        raise RuntimeError("The subject template renders to an empty string.")
    if not body:
        raise RuntimeError("The email body renders to an empty string.")

    spam = templating.local_spam_score(subject, body)
    return {"subject": subject, "body": body, "spam": spam}


def _dns_gate(campaign: dict[str, Any], sender: dict[str, Any]) -> tuple[bool, str]:
    if not campaign.get("require_dns_pass"):
        return True, ""
    try:
        result = sender_dns(sender["id"])
    except Exception as error:
        return False, f"DNS check failed: {error}"
    if result.get("overall") != "pass":
        return False, "SPF, DKIM, or DMARC is not passing for the sender domain."
    return True, ""


def process_job(job: dict[str, Any]) -> str:
    """Processes one claimed job. Returns sent|failed|skipped|capped|requeued."""
    campaign = campaigns.get_campaign(job.get("campaign_id", ""))
    lead = store.get_doc("leads", job.get("lead_id", ""))
    if not campaign or not lead:
        _finish_job(job, lead, lead_status="failed", error="Campaign or lead no longer exists.")
        return "failed"
    if campaign.get("status") != "running":
        _requeue(
            job,
            lead,
            scheduled_at=store.now_iso(),
            error="Campaign is not running.",
            decrement_attempt=True,
        )
        return "capped"
    if lead.get("status") == "sent":
        _finish_job(job, lead, lead_status="sent")
        return "skipped"

    sender = get_sender(campaign.get("sender_id", ""))
    if not sender:
        _finish_job(job, lead, lead_status="failed", error="Sender account not found.")
        return "failed"

    template = store.get_doc("templates", campaign.get("template_id", ""))
    if not template:
        _finish_job(job, lead, lead_status="failed", error="Template not found.")
        return "failed"
    brand = store.get_doc("brands", campaign["brand_id"]) if campaign.get("brand_id") else None

    email = campaigns.lead_target_email(lead, campaign)
    if not is_valid_format(email):
        _finish_job(job, lead, lead_status="skipped", error="No valid email address on this lead.")
        campaigns.record_event(
            campaign["id"], status="skipped", lead_id=lead["id"], job_id=job["id"],
            email=email, detail="No valid email address.", sender_id=sender["id"],
        )
        campaign["next_send_at"] = scheduling.iso(scheduling.utc_now() + timedelta(seconds=15))
        store.upsert_doc("campaigns", campaign)
        return "skipped"
    if is_blocked_domain(email):
        _finish_job(job, lead, lead_status="skipped", error="Disposable email domain.")
        campaigns.record_event(
            campaign["id"], status="skipped", lead_id=lead["id"], job_id=job["id"],
            email=email, detail="Disposable email domain.", sender_id=sender["id"],
        )
        campaign["next_send_at"] = scheduling.iso(scheduling.utc_now() + timedelta(seconds=15))
        store.upsert_doc("campaigns", campaign)
        return "skipped"

    try:
        if suppressions.is_suppressed(email):
            _finish_job(job, lead, lead_status="skipped", error="Recipient unsubscribed.")
            campaigns.record_event(
                campaign["id"], status="skipped", lead_id=lead["id"], job_id=job["id"],
                email=email, detail="Recipient is on the suppression list.", sender_id=sender["id"],
            )
            campaign["next_send_at"] = scheduling.iso(scheduling.utc_now() + timedelta(seconds=15))
            store.upsert_doc("campaigns", campaign)
            return "skipped"
    except Exception as error:
        _requeue(
            job,
            lead,
            scheduled_at=scheduling.iso(scheduling.utc_now() + timedelta(minutes=10)),
            error=f"Suppression lookup failed: {error}",
            decrement_attempt=True,
        )
        return "requeued"

    if campaign.get("dedupe_across_campaigns"):
        already = store.query_docs("events", equals={"email": email, "status": "sent"}, limit=1)
        if already and already[0].get("campaign_id") != campaign["id"]:
            _finish_job(job, lead, lead_status="skipped", error="Already contacted in another campaign.")
            campaigns.record_event(
                campaign["id"], status="skipped", lead_id=lead["id"], job_id=job["id"],
                email=email, detail="Already contacted in another campaign.", sender_id=sender["id"],
            )
            campaign["next_send_at"] = scheduling.iso(scheduling.utc_now() + timedelta(seconds=15))
            store.upsert_doc("campaigns", campaign)
            return "skipped"

    dns_ok, dns_error = _dns_gate(campaign, sender)
    if not dns_ok:
        when = scheduling.iso(scheduling.utc_now() + timedelta(hours=6))
        _requeue(job, lead, scheduled_at=when, error=dns_error, decrement_attempt=True)
        campaign["last_error"] = dns_error
        campaign["next_send_at"] = when
        store.upsert_doc("campaigns", campaign)
        return "requeued"

    state = _limits_state(campaign, sender)
    retry_at, cap_reason = _cap_retry_time(campaign, state)
    if cap_reason:
        _requeue(job, lead, scheduled_at=retry_at, error=cap_reason, decrement_attempt=True)
        campaign["last_error"] = (
            f"{cap_reason} ({state['campaign_hour']}/{state['caps']['hourly']} this hour, "
            f"{state['campaign_day']}/{state['caps']['daily']} today)."
        )
        campaign["next_send_at"] = retry_at
        store.upsert_doc("campaigns", campaign)
        return "capped"

    try:
        rendered = _render_email(campaign, template, lead, brand, sender)
    except Exception as error:
        attempts = int(job.get("attempts") or 0)
        if attempts < MAX_ATTEMPTS:
            _requeue(
                job,
                lead,
                scheduled_at=scheduling.iso(scheduling.utc_now() + timedelta(minutes=10)),
                error=f"Generation failed: {error}",
            )
            campaign["last_error"] = f"Generation retry {attempts}/{MAX_ATTEMPTS}: {error}"
            store.upsert_doc("campaigns", campaign)
            return "requeued"
        _finish_job(job, lead, lead_status="failed", error=f"Generation failed: {error}")
        campaigns.record_event(
            campaign["id"], status="failed", lead_id=lead["id"], job_id=job["id"],
            email=email, detail=f"Generation failed: {error}", sender_id=sender["id"],
        )
        campaign["next_send_at"] = scheduling.iso(
            scheduling.utc_now() + timedelta(seconds=scheduling.random_delay_seconds(campaign))
        )
        campaign["last_error"] = f"Generation failed: {error}"
        store.upsert_doc("campaigns", campaign)
        return "failed"

    spam_score = int(rendered["spam"].get("score") or 0)
    spam_limit = int(campaign.get("spam_score_limit") or 8)
    if spam_score >= spam_limit:
        detail = "Local spam guard: " + ", ".join(rendered["spam"].get("issues") or [])
        _finish_job(job, lead, lead_status="skipped", error=detail)
        campaigns.record_event(
            campaign["id"], status="skipped", lead_id=lead["id"], job_id=job["id"],
            email=email, subject=rendered["subject"], detail=detail, sender_id=sender["id"],
        )
        campaign["next_send_at"] = scheduling.iso(scheduling.utc_now() + timedelta(seconds=30))
        store.upsert_doc("campaigns", campaign)
        return "skipped"

    try:
        settings = resolve_settings(sender)
    except SenderError as error:
        _finish_job(job, lead, lead_status="failed", error=str(error))
        campaigns.record_event(
            campaign["id"], status="failed", lead_id=lead["id"], job_id=job["id"],
            email=email, subject=rendered["subject"], detail=str(error), sender_id=sender["id"],
        )
        campaign["last_error"] = str(error)
        store.upsert_doc("campaigns", campaign)
        return "failed"

    unsubscribe_url = build_unsubscribe_url(email, config.APP_BASE_URL)
    html = wrap_in_html(
        rendered["body"],
        sender.get("from_name") or sender.get("label") or config.SENDER_NAME,
        unsubscribe_url=unsubscribe_url or None,
    )
    ok, send_error = send_with_settings(
        settings,
        email,
        rendered["subject"],
        html,
        rendered["body"],
        sender_name=sender.get("from_name") or (brand or {}).get("from_name") or sender.get("label"),
        unsubscribe_url=unsubscribe_url or None,
    )

    if ok:
        _finish_job(job, lead, lead_status="sent")
        campaigns.record_event(
            campaign["id"], status="sent", lead_id=lead["id"], job_id=job["id"],
            email=email, subject=rendered["subject"], sender_id=sender["id"],
        )
        campaign["last_send_at"] = store.now_iso()
        campaign["last_error"] = ""
    else:
        attempts = int(job.get("attempts") or 0)
        if attempts < MAX_ATTEMPTS:
            _requeue(
                job,
                lead,
                scheduled_at=scheduling.iso(scheduling.utc_now() + timedelta(minutes=15)),
                error=f"Send failed: {send_error}",
            )
            campaigns.record_event(
                campaign["id"], status="failed", lead_id=lead["id"], job_id=job["id"],
                email=email, subject=rendered["subject"],
                detail=f"Attempt {attempts}/{MAX_ATTEMPTS}: {send_error}", sender_id=sender["id"],
            )
            campaign["last_error"] = f"Send retry {attempts}/{MAX_ATTEMPTS}: {send_error}"
            store.upsert_doc("campaigns", campaign)
            return "requeued"
        _finish_job(job, lead, lead_status="failed", error=f"Send failed: {send_error}")
        campaigns.record_event(
            campaign["id"], status="failed", lead_id=lead["id"], job_id=job["id"],
            email=email, subject=rendered["subject"],
            detail=f"Send failed after {MAX_ATTEMPTS} attempts: {send_error}", sender_id=sender["id"],
        )
        campaign["last_error"] = f"Send failed: {send_error}"

    # Always move to the next lead with a fresh randomized gap.
    campaign["next_send_at"] = scheduling.iso(
        scheduling.utc_now() + timedelta(seconds=scheduling.random_delay_seconds(campaign))
    )
    store.upsert_doc("campaigns", campaign)
    return "sent" if ok else "failed"


def _maybe_complete(campaign: dict[str, Any]) -> bool:
    pending = store.query_docs("leads", equals={"campaign_id": campaign["id"], "status": "pending"})
    queued = store.query_docs("jobs", equals={"campaign_id": campaign["id"], "status": "queued"})
    running = store.query_docs("jobs", equals={"campaign_id": campaign["id"], "status": "running"})
    if pending or queued or running:
        return False
    leads = store.query_docs("leads", equals={"campaign_id": campaign["id"]})
    if not leads:
        return False
    campaign["status"] = "completed"
    campaign["next_send_at"] = ""
    store.upsert_doc("campaigns", campaign)
    return True


def run_tick(
    *,
    max_jobs: int | None = None,
    campaign_id: str | None = None,
) -> dict[str, Any]:
    """Runs one queue pass. Safe to call concurrently and from serverless cron."""
    max_jobs = max(1, min(25, int(max_jobs or config.QUEUE_MAX_JOBS)))
    recovered = store.recover_stale_jobs(STALE_JOB_SECONDS, MAX_ATTEMPTS)

    if campaign_id:
        campaign = campaigns.get_campaign(campaign_id)
        selected = [campaign] if campaign else []
    else:
        selected = [item for item in campaigns.list_campaigns() if item.get("status") == "running"]

    summary: dict[str, Any] = {
        "processed": 0,
        "sent": 0,
        "failed": 0,
        "skipped": 0,
        "requeued": 0,
        "capped": 0,
        "recovered_stale": recovered,
        "campaigns": len(selected),
        "details": [],
    }

    for campaign in selected:
        if not campaign or campaign.get("status") != "running":
            continue
        now = scheduling.utc_now()
        if not scheduling.is_within_window(campaign, now):
            when = scheduling.iso(scheduling.next_window_start(campaign, now))
            _reschedule_queued(campaign, when, "Outside the configured send window.")
            summary["details"].append({"campaign_id": campaign["id"], "action": "outside_window", "next": when})
            continue

        ensure_next_job(campaign, now)
        while summary["processed"] < max_jobs:
            job = store.claim_due_job(campaign["id"])
            if not job:
                break
            outcome = process_job(job)
            summary["processed"] += 1
            if outcome in summary:
                summary[outcome] += 1
            summary["details"].append(
                {
                    "campaign_id": campaign["id"],
                    "job_id": job.get("id"),
                    "lead_id": job.get("lead_id"),
                    "outcome": outcome,
                }
            )
            if outcome in {"capped", "requeued"}:
                break

        refreshed = campaigns.get_campaign(campaign["id"])
        if refreshed:
            _maybe_complete(refreshed)

    return summary


async def worker_loop() -> None:
    """In-process polling loop for long-running servers (disabled on Vercel)."""
    logger.info("Campaign queue worker started (every %ss).", config.QUEUE_TICK_SECONDS)
    while True:
        try:
            summary = await asyncio.to_thread(run_tick, max_jobs=config.QUEUE_MAX_JOBS)
            if summary.get("processed"):
                logger.info("Queue tick: %s", summary)
        except asyncio.CancelledError:
            logger.info("Campaign queue worker stopped.")
            raise
        except Exception:
            logger.exception("Campaign queue tick failed.")
        await asyncio.sleep(config.QUEUE_TICK_SECONDS)


def queue_status() -> dict[str, Any]:
    jobs = store.list_docs("jobs")
    queued = [job for job in jobs if job.get("status") == "queued"]
    running = [job for job in jobs if job.get("status") == "running"]
    next_at = min((str(job.get("scheduled_at")) for job in queued if job.get("scheduled_at")), default="")
    return {
        "queued": len(queued),
        "running": len(running),
        "next_send_at": next_at,
        "worker_enabled": config.QUEUE_WORKER_ENABLED,
        "tick_seconds": config.QUEUE_TICK_SECONDS,
        **store.pool_health(),
    }


__all__ = [
    "MAX_ATTEMPTS",
    "effective_caps",
    "ensure_next_job",
    "process_job",
    "queue_status",
    "run_tick",
    "worker_loop",
]
