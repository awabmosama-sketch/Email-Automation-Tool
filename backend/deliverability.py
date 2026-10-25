import copy
from concurrent.futures import ThreadPoolExecutor
import re
import threading
import time
from typing import Any

import dns.exception
import dns.resolver

from . import config

_CACHE_SECONDS = 300
_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_cache_lock = threading.Lock()

COMMON_DKIM_SELECTORS = (
    "zoho",
    "zmail",
    "google",
    "selector1",
    "selector2",
    "default",
    "k1",
    "dkim",
)


def _txt_records(name: str) -> list[str] | None:
    try:
        answers = dns.resolver.resolve(name, "TXT", lifetime=6)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return []
    except (dns.exception.DNSException, OSError):
        return None

    records = []
    for answer in answers:
        try:
            records.append(b"".join(answer.strings).decode("utf-8", "replace"))
        except AttributeError:
            records.append(str(answer).strip('"'))
    return records


def _check_spf(domain: str) -> dict[str, Any]:
    records = _txt_records(domain)
    if records is None:
        return {"status": "error", "record": "", "detail": "DNS lookup failed."}
    record = next((item for item in records if item.lower().startswith("v=spf1")), "")
    if record:
        return {"status": "pass", "record": record, "detail": "SPF record found."}
    return {"status": "missing", "record": "", "detail": "No SPF record found."}


def _check_dmarc(domain: str) -> dict[str, Any]:
    records = _txt_records("_dmarc." + domain)
    if records is None:
        return {"status": "error", "record": "", "detail": "DNS lookup failed."}
    record = next((item for item in records if item.lower().startswith("v=dmarc1")), "")
    if record:
        match = re.search(r"\bp=([a-z]+)", record, re.IGNORECASE)
        policy = match.group(1).lower() if match else ""
        detail = f"DMARC policy: p={policy}." if policy else "DMARC record found."
        return {"status": "pass", "record": record, "policy": policy, "detail": detail}
    return {"status": "missing", "record": "", "policy": "", "detail": "No DMARC record found."}


def _check_dkim(domain: str) -> dict[str, Any]:
    selectors = [config.DKIM_SELECTOR] if config.DKIM_SELECTOR else list(COMMON_DKIM_SELECTORS)
    looked_up = 0
    for selector in selectors:
        records = _txt_records(f"{selector}._domainkey.{domain}")
        if records is None:
            # A resolver-level failure will not improve by trying more selectors.
            break
        looked_up += 1
        record = next(
            (item for item in records if "v=dkim1" in item.lower() or "p=" in item.lower()),
            "",
        )
        if record:
            return {
                "status": "pass",
                "selector": selector,
                "record": record,
                "detail": f"DKIM key found at selector '{selector}'.",
            }
    if not looked_up:
        return {
            "status": "error",
            "selector": config.DKIM_SELECTOR,
            "record": "",
            "detail": "DNS lookup failed.",
        }
    return {
        "status": "missing",
        "selector": config.DKIM_SELECTOR,
        "record": "",
        "detail": f"No DKIM key found for selectors: {', '.join(selectors)}.",
    }


def check_domain(domain: str, provider: str | None = None, refresh: bool = False) -> dict[str, Any]:
    domain = (domain or "").strip().lower()
    now = time.monotonic()
    if not refresh:
        with _cache_lock:
            cached = _cache.get(domain)
        if cached and cached[0] > now:
            result = copy.deepcopy(cached[1])
            result["provider"] = provider or config.SMTP_PROVIDER
            return result

    # The three lookups are independent, so run them together to keep the
    # endpoint responsive when a resolver is slow.
    with ThreadPoolExecutor(max_workers=3) as executor:
        spf_future = executor.submit(_check_spf, domain)
        dkim_future = executor.submit(_check_dkim, domain)
        dmarc_future = executor.submit(_check_dmarc, domain)
        spf = spf_future.result()
        dkim = dkim_future.result()
        dmarc = dmarc_future.result()
    checks = (spf, dkim, dmarc)

    tips = []
    if spf["status"] != "pass":
        tips.append(
            "Publish an SPF TXT record that includes your mail provider, "
            "e.g. v=spf1 include:zohomail.com ~all."
        )
    if dkim["status"] != "pass":
        tips.append(
            "Enable DKIM in your mail provider's admin console and publish the TXT record. "
            "If your provider uses a custom selector, set DKIM_SELECTOR."
        )
    if dmarc["status"] != "pass":
        tips.append(
            f"Publish a DMARC record, e.g. v=DMARC1; p=none; rua=mailto:postmaster@{domain}."
        )
    if all(check["status"] == "pass" for check in checks):
        tips.append(
            "Authentication looks good. Gmail's Promotions tab is influenced by content and "
            "engagement, so keep emails short and personal and test with a Gmail seed account."
        )
    else:
        tips.append(
            "After fixing the records, verify with Gmail's 'Show original' "
            "(SPF, DKIM, and DMARC should all be PASS) and run a mail-tester.com check."
        )
    tips.append(
        "Cold outreach is safer from a dedicated, warmed subdomain than from your primary "
        "business domain."
    )

    result = {
        "provider": provider or config.SMTP_PROVIDER,
        "domain": domain,
        "overall": "pass" if all(check["status"] == "pass" for check in checks) else "attention",
        "spf": spf,
        "dkim": dkim,
        "dmarc": dmarc,
        "tips": tips,
    }
    with _cache_lock:
        if len(_cache) > 200:
            for key in [key for key, (expires, _) in _cache.items() if expires <= now]:
                _cache.pop(key, None)
        _cache[domain] = (now + _CACHE_SECONDS, copy.deepcopy(result))
    return result
