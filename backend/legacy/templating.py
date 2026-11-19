"""Template rendering, placeholder mapping, and a fast local spam guard."""

from __future__ import annotations

import re
from typing import Any, Iterable

PLACEHOLDER_PATTERN = re.compile(r"\{\{\s*([^{}]{1,80}?)\s*\}\}")

# Canonical field -> accepted placeholder spellings. Normalisation strips case,
# spaces, underscores, hyphens, and slashes, so "{{First Name}}" and
# "{{first_name}}" both resolve to ``first_name``.
_PLACEHOLDER_ALIASES: dict[str, tuple[str, ...]] = {
    "first_name": ("first name", "firstname", "first", "given name", "name", "contact name"),
    "position": ("position", "job title", "jobtitle", "title", "role", "designation"),
    "company_name": ("company name", "company", "companyname", "organization", "organisation"),
    "personal_email": ("personal email", "personalemail", "personal e mail"),
    "business_email": ("business email", "businessemail", "work email", "email", "email address"),
    "brand_name": ("brand", "brand name", "brandname", "business"),
    "sender_name": ("sender name", "sendername", "from name", "fromname"),
    "sender_email": ("sender email", "senderemail", "from email", "fromemail"),
    "website": ("website", "site", "url"),
    "sending_domain": ("sending domain", "domain"),
}

def normalize_placeholder(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


_LOOKUP: dict[str, str] = {}
for _canonical, _aliases in _PLACEHOLDER_ALIASES.items():
    _LOOKUP[normalize_placeholder(_canonical)] = _canonical
    for _alias in _aliases:
        _LOOKUP[normalize_placeholder(_alias)] = _canonical
del _canonical, _aliases, _alias


def canonical_placeholder(value: str) -> str | None:
    return _LOOKUP.get(normalize_placeholder(value))


def build_context(
    lead: dict[str, Any] | None = None,
    *,
    brand: dict[str, Any] | None = None,
    sender: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Builds the canonical placeholder context for a lead/campaign render."""
    lead = lead or {}
    brand = brand or {}
    sender = sender or {}
    values = {
        "first_name": str(lead.get("first_name") or ""),
        "position": str(lead.get("position") or ""),
        "company_name": str(lead.get("company_name") or ""),
        "personal_email": str(lead.get("personal_email") or ""),
        "business_email": str(lead.get("business_email") or lead.get("email") or ""),
        "brand_name": str(brand.get("name") or ""),
        "sender_name": str(sender.get("from_name") or sender.get("label") or ""),
        "sender_email": str(sender.get("email") or sender.get("smtp_username") or ""),
        "website": str(brand.get("website") or ""),
        "sending_domain": str(sender.get("domain") or brand.get("sending_domain") or ""),
    }
    # Also accept already-normalised keys and human labels from callers.
    for key, value in list(values.items()):
        values[normalize_placeholder(key)] = value
    return values


def _lookup_value(context: dict[str, str], placeholder: str) -> tuple[str, bool]:
    canonical = canonical_placeholder(placeholder)
    if canonical:
        return str(context.get(canonical, "")), True
    direct = context.get(normalize_placeholder(placeholder))
    if direct is not None:
        return str(direct), True
    return "", False


def find_unknown_placeholders(text: str) -> list[str]:
    unknown: list[str] = []
    for match in PLACEHOLDER_PATTERN.finditer(text or ""):
        _, found = _lookup_value({}, match.group(1))
        if not found and match.group(0) not in unknown:
            unknown.append(match.group(0))
    return unknown


def render_template(text: str, context: dict[str, str], *, keep_unknown: bool = False) -> str:
    """Replaces ``{{Placeholder}}`` tokens. Unknown tokens become blank by default."""

    def replace(match: re.Match[str]) -> str:
        value, found = _lookup_value(context, match.group(1))
        if found:
            return value
        return match.group(0) if keep_unknown else ""

    return PLACEHOLDER_PATTERN.sub(replace, text or "")


def render_preview(
    *,
    subject_template: str,
    body_template: str,
    context: dict[str, str],
) -> dict[str, Any]:
    subject = clean_subject(render_template(subject_template, context))
    body = render_template(body_template, context).strip()
    warnings = []
    unknown_subject = find_unknown_placeholders(subject_template)
    unknown_body = find_unknown_placeholders(body_template)
    if unknown_subject:
        warnings.append("Unknown subject placeholder(s): " + ", ".join(unknown_subject))
    if unknown_body:
        warnings.append("Unknown body placeholder(s): " + ", ".join(unknown_body))
    if not subject:
        warnings.append("The subject renders to an empty string.")
    if not body:
        warnings.append("The body renders to an empty string.")
    return {"subject": subject, "body": body, "warnings": warnings}


def clean_subject(subject: str, limit: int = 200) -> str:
    value = re.sub(r"\s+", " ", (subject or "").replace("\r", " ").replace("\n", " ")).strip()
    return value[:limit]


_SPAM_TRIGGERS: dict[str, int] = {
    "act now": 3,
    "apply now": 2,
    "buy now": 3,
    "cash bonus": 3,
    "click here": 2,
    "congratulations": 2,
    "credit card": 2,
    "double your": 3,
    "earn money": 3,
    "exclusive deal": 2,
    "free": 2,
    "free trial": 1,
    "guarantee": 3,
    "guaranteed": 3,
    "limited time": 2,
    "lowest price": 2,
    "make money": 3,
    "no obligation": 2,
    "order now": 3,
    "risk free": 3,
    "risk-free": 3,
    "satisfaction guaranteed": 3,
    "special promotion": 2,
    "urgent": 2,
    "winner": 3,
    "100%": 2,
    "$$$": 3,
}


def local_spam_score(subject: str, body: str) -> dict[str, Any]:
    """Returns a deterministic 0-10 spam risk score without an LLM call."""
    text = f"{subject}\n{body}".lower()
    score = 0
    issues: list[str] = []

    for trigger, weight in _SPAM_TRIGGERS.items():
        if trigger in text:
            score += weight
            issues.append(trigger)

    letters = [character for character in (subject or "") if character.isalpha()]
    if len(letters) >= 8 and all(character.isupper() for character in letters):
        score += 2
        issues.append("all-caps subject")

    if (subject or "").count("!") >= 3:
        score += 1
        issues.append("excessive exclamation marks")

    links = len(re.findall(r"https?://", body or ""))
    if links > 3:
        score += 1
        issues.append("too many links")

    if len(body or "") > 5000:
        score += 1
        issues.append("very long body")

    return {"score": min(10, score), "issues": issues or ["none"]}


def apply_signature(body: str, signature: str) -> str:
    signature = (signature or "").strip()
    body = (body or "").strip()
    if not signature:
        return body
    if signature.lower() in body.lower():
        return body
    return body.rstrip() + "\n\n" + signature


def truncate(value: str, limit: int) -> str:
    value = value or ""
    return value if len(value) <= limit else value[: max(0, limit - 1)].rstrip() + "…"


def join_present(values: Iterable[str], separator: str = " · ") -> str:
    return separator.join(str(value) for value in values if value)


__all__ = [
    "PLACEHOLDER_PATTERN",
    "apply_signature",
    "build_context",
    "canonical_placeholder",
    "clean_subject",
    "find_unknown_placeholders",
    "join_present",
    "local_spam_score",
    "normalize_placeholder",
    "render_preview",
    "render_template",
    "truncate",
]
