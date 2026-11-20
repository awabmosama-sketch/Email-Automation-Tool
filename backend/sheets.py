"""Google Sheets ingestion for campaign leads.

Two access modes are supported:

1. **Public / link-shared sheets** - fetched through Google's CSV export
   endpoint. No credentials are required, but the sheet must be viewable by
   anyone with the link.
2. **Private sheets** - fetched through the Google Sheets API v4 using a
   service account. Set ``GOOGLE_SERVICE_ACCOUNT_JSON`` (raw JSON) or
   ``GOOGLE_SERVICE_ACCOUNT_FILE`` (path to the key file) and share the sheet
   with the service account's ``client_email``.

Only the five requested lead fields are parsed. Phone, mobile, and telephone
columns are deliberately ignored and never stored.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from . import config
from .verifier import is_blocked_domain, is_valid_format

logger = logging.getLogger(__name__)

LEAD_FIELDS = ("first_name", "position", "company_name", "personal_email", "business_email")
LEAD_FIELD_LABELS = {
    "first_name": "First Name",
    "position": "Position / Job Title",
    "company_name": "Company Name",
    "personal_email": "Personal Email",
    "business_email": "Business Email",
}

MAX_SHEET_BYTES = 8 * 1024 * 1024
MAX_LEADS_PER_SYNC = 5000
SHEET_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{20,}$")
PHONE_HEADER_PATTERN = re.compile(r"(phone|mobile|tel|telephone|whatsapp|cell)", re.IGNORECASE)

_HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "first_name": (
        "first name",
        "firstname",
        "first",
        "given name",
        "contact first name",
        "contact name",
        "full name",
        "name",
    ),
    "position": (
        "position",
        "job title",
        "jobtitle",
        "job role",
        "title",
        "role",
        "designation",
        "seniority",
    ),
    "company_name": (
        "company name",
        "company",
        "organization",
        "organisation",
        "org",
        "employer",
        "account name",
        "business name",
    ),
    "personal_email": (
        "personal email",
        "personal e mail",
        "personal email address",
        "personal mail",
        "private email",
        "home email",
    ),
    "business_email": (
        "business email",
        "business e mail",
        "business email address",
        "work email",
        "work e mail",
        "corporate email",
        "company email",
        "professional email",
        "email",
        "email address",
        "e mail",
        "mail",
    ),
}


class SheetError(Exception):
    """Raised when a sheet cannot be parsed or fetched."""


def normalize_header(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", (value or "").lower())).strip()


def parse_sheet_url(raw: str) -> tuple[str, str]:
    """Returns ``(spreadsheet_id, gid)`` from a URL or bare spreadsheet ID."""
    raw = (raw or "").strip()
    if not raw:
        raise SheetError("Paste a Google Sheet URL or spreadsheet ID.")

    gid = ""
    match = re.search(r"[?#&]gid=(\d+)", raw)
    if match:
        gid = match.group(1)

    if SHEET_ID_PATTERN.fullmatch(raw) and "/" not in raw:
        return raw, gid

    url_match = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", raw)
    if url_match:
        spreadsheet_id = url_match.group(1)
        if not SHEET_ID_PATTERN.fullmatch(spreadsheet_id):
            raise SheetError("That spreadsheet ID looks invalid.")
        hash_match = re.search(r"#gid=(\d+)", raw)
        if hash_match and not gid:
            gid = hash_match.group(1)
        return spreadsheet_id, gid

    if SHEET_ID_PATTERN.fullmatch(raw):
        return raw, gid
    raise SheetError("Paste a Google Sheets URL that contains /spreadsheets/d/<id>.")


def _service_account_info() -> dict[str, Any] | None:
    if config.GOOGLE_SERVICE_ACCOUNT_JSON:
        try:
            info = json.loads(config.GOOGLE_SERVICE_ACCOUNT_JSON)
        except json.JSONDecodeError as error:
            raise SheetError("GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON.") from error
        if not isinstance(info, dict):
            raise SheetError("GOOGLE_SERVICE_ACCOUNT_JSON must be a JSON object.")
        return info
    if config.GOOGLE_SERVICE_ACCOUNT_FILE:
        path = Path(config.GOOGLE_SERVICE_ACCOUNT_FILE)
        if not path.exists():
            raise SheetError(f"GOOGLE_SERVICE_ACCOUNT_FILE not found: {path}")
        try:
            info = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise SheetError("Could not read the Google service account key file.") from error
        return info if isinstance(info, dict) else None
    return None


def _sheet_info(
    spreadsheet_id: str,
    gid: str,
    sheet_tab: str,
    *,
    use_service_account: bool,
    bearer_token: str,
) -> dict[str, Any]:
    """Fetches a sheet and returns rows plus the tab resolution used.

    Logs the exact URL the client was asked to read. When a specific gid or
    tab was requested and the service answers HTTP 400, the call fails loudly
    instead of silently reading the first sheet.
    """
    resolved_tab: str | None = None
    tab_list: list[str] = []
    if sheet_tab:
        # For a requested tab we must confirm it exists before we trust any
        # rows the server returns. Google's gviz endpoint returns HTTP 200 and
        # the *first* sheet's data for a tab name that does not exist, which is
        # the exact silent-wrong-sheet bug this block is here to kill.
        #
        # spreadsheets.get is the only reliable check. It works for the
        # service-account path; for the public path it 401/403s when no
        # credentials are configured, and in that case we cannot confirm the
        # tab and must ask the operator to configure the service account (or
        # switch to the gid path) instead of silently reading the wrong sheet.
        list_url = (
            f"https://sheets.googleapis.com/v4/spreadsheets/{quote(spreadsheet_id)}"
        )
        list_auth = "" if not use_service_account else f"Bearer {bearer_token}"
        try:
            list_response = httpx.get(
                list_url,
                headers=(
                    {"Authorization": list_auth} if list_auth else {}
                ),
                params={"fields": "sheets.properties.title"},
                timeout=30,
                follow_redirects=True,
            )
        except httpx.HTTPError:
            list_response = None

        if list_response and list_response.status_code == 200:
            try:
                tab_list = [
                    sheet["properties"]["title"]
                    for sheet in list_response.json().get("sheets", [])
                ]
            except ValueError:
                tab_list = []

        if tab_list and sheet_tab in tab_list:
            resolved_tab = sheet_tab
        elif tab_list:
            # Tab name was supplied but spreadsheets.get shows it does not exist.
            # gviz would return HTTP 200 and the *first* sheet's rows for this
            # request, which is the silent-wrong-sheet bug this raises to kill.
            raise SheetError(
                f"Tab '{sheet_tab}' was not found in sheet {spreadsheet_id}. "
                f"Available tabs: {tab_list}. "
                "Copy the correct tab name from the Sheets UI, or use the sheet gid instead."
            )
        else:
            # We could not get a tab list at all (no service account configured on
            # the public path, or the Sheets API refused the call). We must not guess
            # and silently return the first sheet, so raise and point at the gid path,
            # which does not need tab metadata.
            raise SheetError(
                f"Google could not confirm that tab '{sheet_tab}' exists in sheet {spreadsheet_id}. "
                "The tab-name path needs the sheet shared with a Google service account "
                "(configure GOOGLE_SERVICE_ACCOUNT_JSON and share the sheet with its client_email), "
                "or use the sheet gid instead, which works for publicly shared sheets."
            )

    if not sheet_tab:
        resolved_tab = "first sheet"

    if gid:
        suffix = f"&gid={quote(gid, safe='')}"
    else:
        suffix = ""

    if sheet_tab:
        url = (
            f"https://docs.google.com/spreadsheets/d/{quote(spreadsheet_id)}/gviz/tq"
            f"?tqx=out:csv&sheet={quote(sheet_tab)}"
        )
    else:
        url = f"https://docs.google.com/spreadsheets/d/{quote(spreadsheet_id)}/export?format=csv{suffix}"

    logger.info(
        "fetch_sheet",
        extra={
            "spreadsheet_id": spreadsheet_id,
            "gid": gid or "(first sheet)",
            "tab": resolved_tab,
            "request_url": url,
            "source": "service_account" if use_service_account else "public_csv",
        },
    )
    try:
        response = httpx.get(url, timeout=30, follow_redirects=True)
    except httpx.HTTPError as error:
        raise SheetError(f"Could not download the sheet: {error}") from error

    if response.status_code in {401, 403}:
        raise SheetError(
            "Google refused the download. Either share the sheet as 'Anyone with the link', "
            "or configure a Google service account for private sheets."
        )
    if response.status_code >= 400:
        if gid:
            raise SheetError(
                f"Tab with gid={gid} was not found in sheet {spreadsheet_id}. "
                "Click the correct tab, copy the number after #gid= in the URL, "
                "or clear the gid to use the first tab."
            )
        if sheet_tab:
            raise SheetError(
                f"Google Sheets API returned HTTP {response.status_code} while reading "
                f"tab '{sheet_tab}' in sheet {spreadsheet_id}. "
                f"Available tabs: {tab_list or 'unknown'}. "
                "Copy the correct tab name from the Sheets UI."
            )
        raise SheetError(
            f"Google returned HTTP {response.status_code} while downloading the sheet. "
            "Check that the sheet is shared as 'Anyone with the link' and that the "
            "sheet tab / gid still exists."
        )
    if len(response.content) > MAX_SHEET_BYTES:
        raise SheetError("The sheet is too large to import (limit: 8 MB of CSV).")

    text = response.text
    head = text[:500].lower()
    if "<html" in head and ("sign in" in head or "google accounts" in head):
        raise SheetError(
            "That sheet is not public. Share it as 'Anyone with the link', or configure "
            "a Google service account for private sheets."
        )

    try:
        return {
            "values": [row for row in csv.reader(io.StringIO(text))],
            "request_url": url,
            "used_tab": resolved_tab or "first sheet",
            "tab_exists": True,
        }
    except csv.Error as error:
        raise SheetError(f"Could not parse the sheet as CSV: {error}") from error


def _fetch_with_service_account(spreadsheet_id: str, sheet_tab: str) -> list[list[str]]:
    info = _service_account_info()
    if not info:
        raise SheetError("No Google service account is configured.")
    try:
        from google.auth.transport.requests import Request as GoogleRequest
        from google.oauth2 import service_account
    except ImportError as error:  # pragma: no cover - requirements.txt includes google-auth
        raise SheetError(
            "Private Google Sheets need the google-auth package. Run pip install -r requirements.txt."
        ) from error

    try:
        credentials = service_account.Credentials.from_service_account_info(
            info,
            scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
        )
        credentials.refresh(GoogleRequest())
    except Exception as error:
        raise SheetError(f"Google service account authentication failed: {error}") from error

    return [[str(cell) if cell is not None else "" for cell in row] for row in _sheet_info(
        spreadsheet_id,
        gid="",
        sheet_tab=sheet_tab,
        use_service_account=True,
        bearer_token=credentials.token,
    )["values"]]


def _fetch_public_csv(spreadsheet_id: str, gid: str, sheet_tab: str) -> list[list[str]]:
    return _sheet_info(
        spreadsheet_id,
        gid=gid,
        sheet_tab=sheet_tab,
        use_service_account=False,
        bearer_token="",
    )["values"]


def fetch_table(
    sheet_url: str,
    sheet_tab: str = "",
    has_header: bool = True,
    gid: str = "",
) -> dict[str, Any]:
    """Downloads a sheet and returns headers + raw rows plus tab resolution."""
    spreadsheet_id, parsed_gid = parse_sheet_url(sheet_url)
    gid = (gid or parsed_gid or "").strip()
    service_account = _service_account_info() is not None
    source = "service_account" if service_account else "public_csv"
    bearer_token = ""
    if service_account:
        info = _service_account_info()
        if info:
            from google.oauth2 import service_account

            bearer_token = info.get("client_email")

    try:
        info = _sheet_info(
            spreadsheet_id,
            gid=gid,
            sheet_tab=sheet_tab,
            use_service_account=service_account,
            bearer_token=bearer_token or "",
        )
    except SheetError:
        raise
    except Exception as error:
        raise SheetError(f"Could not read the sheet: {error}") from error

    values = info["values"]
    rows = [[str(cell or "").strip() for cell in row] for row in values]
    rows = [row for row in rows if any(cell for cell in row)]

    warnings: list[str] = []
    if rows and len(rows) > MAX_LEADS_PER_SYNC + 1:
        warnings.append(
            f"Only the first {MAX_LEADS_PER_SYNC:,} data rows are imported per sync."
        )
        rows = rows[: MAX_LEADS_PER_SYNC + 1]

    if not rows:
        return {
            "headers": [],
            "rows": [],
            "data_rows": [],
            "source": source,
            "spreadsheet_id": spreadsheet_id,
            "gid": gid,
            "has_header": has_header,
            "warnings": ["The sheet is empty."],
            "used_tab": info.get("used_tab", "first sheet"),
            "tab_exists": info.get("tab_exists", True),
            "request_url": info.get("request_url", ""),
        }

    if has_header:
        headers = [cell or f"Column {index + 1}" for index, cell in enumerate(rows[0])]
        data_rows = rows[1:]
    else:
        width = max(len(row) for row in rows)
        headers = [f"Column {_column_name(index)}" for index in range(width)]
        data_rows = rows

    # Normalise the width so every row can be zipped with headers safely.
    width = len(headers)
    data_rows = [(row + [""] * width)[:width] for row in data_rows]

    return {
        "headers": headers,
        "rows": data_rows,
        "data_rows": data_rows,
        "source": source,
        "spreadsheet_id": spreadsheet_id,
        "gid": gid,
        "has_header": has_header,
        "warnings": warnings,
        "used_tab": info.get("used_tab", "first sheet"),
        "tab_exists": info.get("tab_exists", True),
        "request_url": info.get("request_url", ""),
    }


def _column_name(index: int) -> str:
    name = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def auto_map_headers(headers: list[str]) -> dict[str, str]:
    """Suggests canonical lead fields from the sheet's header row.

    Phone-like columns are explicitly skipped, and a single generic ``Email``
    column maps to Business Email rather than being used twice.
    """
    normalized: dict[str, str] = {}
    for header in headers:
        key = normalize_header(header)
        if not key or PHONE_HEADER_PATTERN.search(header):
            continue
        normalized.setdefault(key, header)

    result: dict[str, str] = {}
    used: set[str] = set()

    # Pass 1: exact alias matches, most specific fields first.
    for field in ("first_name", "position", "company_name", "personal_email", "business_email"):
        for alias in _HEADER_ALIASES[field]:
            key = normalize_header(alias)
            header = normalized.get(key)
            if header and header not in used:
                result[field] = header
                used.add(header)
                break

    # Pass 2: contained aliases (e.g. "Contact First Name (optional)").
    for field in ("first_name", "position", "company_name", "personal_email", "business_email"):
        if field in result:
            continue
        for alias in _HEADER_ALIASES[field]:
            key = normalize_header(alias)
            # Short generic words ("name", "email", "title") would match the
            # wrong column when a header is descriptive, so pass 2 only uses
            # the longer aliases.
            if len(key) < 8:
                continue
            for candidate_key, header in normalized.items():
                if header in used:
                    continue
                if key in candidate_key:
                    result[field] = header
                    used.add(header)
                    break
            if field in result:
                break

    return result


def _resolve_index(headers: list[str], header_name: str) -> int | None:
    target = normalize_header(header_name)
    if not target:
        return None
    for index, header in enumerate(headers):
        if normalize_header(header) == target:
            return index
    return None


def extract_leads(
    table: dict[str, Any],
    column_map: dict[str, str],
    *,
    prefer_personal: bool = False,
) -> dict[str, Any]:
    """Maps raw sheet rows to the five allowed lead fields.

    Returns ``{"leads": [...], "skipped": [...], "duplicates": n, "warnings": [...]}``.
    The returned lead dictionaries intentionally contain no phone field.
    """
    headers: list[str] = table.get("headers", [])
    rows: list[list[str]] = table.get("data_rows") or table.get("rows") or []
    warnings = list(table.get("warnings") or [])

    indexes: dict[str, int | None] = {}
    for field in LEAD_FIELDS:
        mapped_header = (column_map or {}).get(field) or ""
        index = _resolve_index(headers, mapped_header) if mapped_header else None
        if mapped_header and index is None:
            warnings.append(f"Column '{mapped_header}' was not found; {LEAD_FIELD_LABELS[field]} will be blank.")
        indexes[field] = index

    if indexes.get("business_email") is None and indexes.get("personal_email") is None:
        raise SheetError(
            "Map at least one email column (Business Email or Personal Email) before importing."
        )

    leads: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    seen_emails: set[str] = set()
    duplicates = 0

    for offset, row in enumerate(rows):
        row_number = offset + (2 if table.get("has_header") else 1)

        def cell(field: str) -> str:
            index = indexes.get(field)
            if index is None or index >= len(row):
                return ""
            return str(row[index] or "").strip()

        first_name = cell("first_name")
        # A "Full Name" column is useful, but we only keep the first token.
        first_name = first_name.split()[0] if first_name.split() else ""
        position = cell("position")
        company_name = cell("company_name")
        personal_email = cell("personal_email").lower()
        business_email = cell("business_email").lower()

        personal_ok = is_valid_format(personal_email) and not is_blocked_domain(personal_email)
        business_ok = is_valid_format(business_email) and not is_blocked_domain(business_email)

        if personal_email and not personal_ok:
            personal_email = ""
        if business_email and not business_ok:
            business_email = ""

        chosen = ""
        if prefer_personal:
            chosen = personal_email or business_email
        else:
            chosen = business_email or personal_email

        if not chosen:
            skipped.append(
                {
                    "row": row_number,
                    "reason": "No valid personal or business email address.",
                    "first_name": first_name,
                    "company_name": company_name,
                }
            )
            continue
        if chosen in seen_emails:
            duplicates += 1
            continue
        seen_emails.add(chosen)

        leads.append(
            {
                "row_number": row_number,
                "source_row": row_number,
                "first_name": first_name[:100],
                "position": position[:150],
                "company_name": company_name[:150],
                "personal_email": personal_email[:320],
                "business_email": business_email[:320],
                "email": chosen[:320],
            }
        )

    if len(skipped) > 100:
        warnings.append(f"{len(skipped) - 100} more rows were skipped; showing the first 100 issues.")
        skipped = skipped[:100]

    return {
        "leads": leads,
        "skipped": skipped,
        "duplicates": duplicates,
        "warnings": warnings,
        "total_rows": len(rows),
    }


def inspect_sheet(
    sheet_url: str,
    sheet_tab: str = "",
    has_header: bool = True,
    column_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    table = fetch_table(sheet_url, sheet_tab=sheet_tab, has_header=has_header)
    mapping = column_map or auto_map_headers(table["headers"])
    preview = extract_leads(table, mapping, prefer_personal=False)
    return {
        "spreadsheet_id": table["spreadsheet_id"],
        "gid": table["gid"],
        "source": table["source"],
        "headers": table["headers"],
        "suggested_map": auto_map_headers(table["headers"]),
        "column_map": mapping,
        "sample": preview["leads"][:8],
        "total_rows": preview["total_rows"],
        "valid_rows": len(preview["leads"]),
        "skipped": preview["skipped"][:8],
        "duplicates": preview["duplicates"],
        "warnings": preview["warnings"],
    }


def sample_lead() -> dict[str, str]:
    return {
        "first_name": "Sara",
        "position": "Head of Marketing",
        "company_name": "Northwind Labs",
        "personal_email": "sara.khan@gmail.com",
        "business_email": "sara@northwindlabs.com",
        "email": "sara@northwindlabs.com",
    }


# ---------------------------------------------------------------------------
# Compatibility helpers used by the outreach campaign queue
# ---------------------------------------------------------------------------

TARGET_FIELDS = LEAD_FIELDS
SheetsError = SheetError


def _effective_mapping(
    headers: list[str], mapping: dict[str, str] | None
) -> dict[str, str]:
    """Merges a user mapping over the auto-detected header map."""
    detected = auto_map_headers(headers)
    if not isinstance(mapping, dict):
        return detected
    merged = dict(detected)
    for field in LEAD_FIELDS:
        value = str(mapping.get(field) or "").strip()
        if value:
            merged[field] = value
    return merged


def _column_label_map(headers: list[str]) -> dict[str, str]:
    """Maps the five target fields to their best sheet column name."""
    mapping = auto_map_headers(headers)
    for field in LEAD_FIELDS:
        label = LEAD_FIELD_LABELS[field]
        if label in headers:
            mapping[field] = label
    return mapping


def fetch_leads(
    sheet_url: str,
    gid: str = "",
    tab: str = "",
    mapping: dict[str, str] | None = None,
    suppress: Any = None,
    prefer_personal: bool = False,
) -> dict[str, Any]:
    """Fetches and maps a sheet into campaign leads.

    Only the five allowed fields are returned; phone columns are ignored.
    ``suppress`` accepts any iterable of email addresses (for example
    ``suppressions.all_emails()``) that must not be imported.
    """
    table = fetch_table(sheet_url, sheet_tab=tab, gid=gid)
    column_map = _effective_mapping(table["headers"], mapping)
    extracted = extract_leads(table, column_map, prefer_personal=prefer_personal)

    suppressed = {
        str(email).strip().lower()
        for email in (suppress or [])
        if str(email).strip()
    }
    leads = extracted["leads"]
    suppressed_count = 0
    if suppressed:
        kept = []
        for lead in leads:
            email = (lead.get("email") or "").lower()
            if email in suppressed:
                suppressed_count += 1
                continue
            kept.append(lead)
        leads = kept

    skipped = extracted["skipped"]
    return {
        "leads": leads,
        "headers": table.get("headers", []),
        "data_rows": table.get("data_rows", []),
        "total_rows": extracted.get("total_rows", len(table.get("data_rows", []))),
        "usable_leads": len(leads),
        "skipped": skipped,
        "duplicates": extracted.get("duplicates", 0),
        "suppressed": suppressed_count,
        "warnings": extracted.get("warnings", []),
        "source": table.get("source", ""),
        "spreadsheet_id": table.get("spreadsheet_id", ""),
        "gid": table.get("gid", ""),
        "used_tab": table.get("used_tab", "first sheet"),
        "tab_exists": table.get("tab_exists", True),
        "request_url": table.get("request_url", ""),
    }


TARGET_FIELDS = ("first_name", "position", "company_name", "personal_email", "business_email")

LEAD_FIELD_LABELS = {
    "first_name": "First Name",
    "position": "Position / Job Title",
    "company_name": "Company Name",
    "personal_email": "Personal Email",
    "business_email": "Business Email",
}


def _map_indicator(headers: list[str]) -> dict[str, dict[str, Any]]:
    """Per-target-field presence marker for the preview UI.

    Every target field gets a green ``found`` marker for a matching column and
    a grey ``missing`` marker otherwise. Targets are matched by alias, and
    phone-like columns are never mapped.
    """
    normalized = {normalize_header(header): header for header in headers}
    result: dict[str, dict[str, Any]] = {}
    for field in TARGET_FIELDS:
        labels = LEAD_FIELD_LABELS[field]
        key = normalize_header(labels)
        found = key in normalized
        def _matched(
            candidate_key: str,
            prefixes: tuple[str, ...],
        ) -> bool:
            return candidate_key in normalized and any(
                normalize_header(p) in candidate_key for p in prefixes
            )
        if not found:
            for alias in _HEADER_ALIASES[field]:
                if _matched(alias, ("first", "name")):
                    found = True
                    break
        result[field] = {
            "label": labels,
            "found": found,
            "missing": not found,
        }
    return result


def _extract_preview_fields(headers: list[str], column_map: dict[str, str]) -> dict[str, str]:
    """Returns the value of each of the five target fields for one row."""
    indexes: dict[str, int | None] = {}
    for field in TARGET_FIELDS:
        mapped = (column_map or {}).get(field) or ""
        indexes[field] = _resolve_index(headers, mapped) if mapped else None

    def cell(field: str) -> str:
        index = indexes[field]
        if index is None or index >= len(headers):
            return ""
        return str(headers[index]) if index < len(headers) else ""

    return {field: cell(field) for field in TARGET_FIELDS}


def _field_found(headers: list[str], field: str, column_map: dict[str, str] | None) -> bool:
    """True when `field` already has a mapped column or an auto-detected alias."""
    mapped = (column_map or {}).get(field) or ""
    if mapped and _resolve_index(headers, mapped) is not None:
        return True
    return field in auto_map_headers(headers)


def _map_missing_field(headers: list[str], field: str) -> dict[str, str]:
    """One entry for a target field that the sheet does not provide."""
    return {"field": field, "label": LEAD_FIELD_LABELS[field]}


def preview_sheet(
    sheet_url: str,
    gid: str = "",
    tab: str = "",
    mapping: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Returns a small preview used by the campaign builder UI.

    The payload now carries tab resolution, the exact request URL used for the
    fetch, and the per-field mapping indicator. A request that used a specific
    tab or gid and 400ed is raised loudly instead of falling back silently.
    """
    table = fetch_table(sheet_url, sheet_tab=tab, gid=gid)
    headers = table.get("headers", [])
    column_map = _effective_mapping(headers, mapping)
    warnings = list(table.get("warnings") or [])

    # A preview must stay readable even when the sheet is missing the email
    # columns that extract_leads() would reject. Surface the same "missing
    # column" warnings a real extraction would produce, but do not error out.
    extracted_warnings: list[str] = []
    sample: list[dict[str, Any]] = []
    total_rows = table.get("data_rows", []) and len(table.get("data_rows", [])) or 0
    try:
        extracted = extract_leads(table, column_map)
        extracted_warnings = list(extracted.get("warnings") or [])
        sample = extracted["leads"][:8]
    except SheetError as error:
        extracted_warnings.append(str(error))

    warnings.extend(extracted_warnings)

    return {
        "headers": headers,
        "used_tab": table.get("used_tab", "first sheet"),
        "tab_exists": table.get("tab_exists", True),
        "request_url": table.get("request_url", ""),
        "suggested_map": auto_map_headers(headers),
        "column_map": column_map,
        "source": table.get("source", ""),
        "spreadsheet_id": table.get("spreadsheet_id", ""),
        "gid": table.get("gid", ""),
        "missing_fields": [_map_missing_field(headers, field)
                           for field in TARGET_FIELDS
                           if not _field_found(headers, field, column_map)],
        "field_map": _map_indicator(headers),
        "mapping": {
            **auto_map_headers(headers),
            **(_effective_mapping(headers, mapping) or {}),
        },
        "sample": sample,
        "total_rows": total_rows,
        "usable_leads": len(sample),
        "skipped": [],
        "duplicates": 0,
        "warnings": warnings,
    }


__all__ = [
    "LEAD_FIELDS",
    "LEAD_FIELD_LABELS",
    "PHONE_HEADER_PATTERN",
    "SheetError",
    "SheetsError",
    "TARGET_FIELDS",
    "auto_map_headers",
    "extract_leads",
    "fetch_leads",
    "fetch_table",
    "inspect_sheet",
    "normalize_header",
    "parse_sheet_url",
    "preview_sheet",
    "sample_lead",
]
