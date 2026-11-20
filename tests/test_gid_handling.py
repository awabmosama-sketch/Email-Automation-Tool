"""Regression tests for the campaign sheet gid.

The bug: a pasted URL with ``gid=679250682`` was stored with a fabricated
``0`` (the old modal prefill / backend default), and because ``0`` is a truthy
string the URL's real gid was never read. ``0`` itself is a valid gid, so the
fix is to stop inventing it -- not to treat it as invalid.
"""

import pytest

from backend import outreach, sheets

SPREADSHEET_ID = "1HifKrUQM3uII05g14NEmitiNuVedGWT3vjRenZLlp-o"
REAL_GID = "679250682"
USER_URL = (
    f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}"
    f"/edit?gid={REAL_GID}#gid={REAL_GID}"
)
FRAGMENT_ONLY_URL = (
    f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit#gid={REAL_GID}"
)
BARE_URL = f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit"


class _Resp:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text
        self.content = text.encode()

    def json(self):
        return {"values": []}


def _empty_state():
    return {
        "senders": [],
        "brands": [],
        "templates": [],
        "campaigns": [],
        "recipients": [],
        "events": [],
    }


def _create_campaign(monkeypatch, **payload):
    """Runs create_campaign against an in-memory state document."""
    state = _empty_state()
    monkeypatch.setattr(outreach, "_mutate", lambda mutator: mutator(state))
    payload.setdefault("name", "Gid campaign")
    result = outreach.create_campaign(payload)
    return state["campaigns"][0], result


def _capture_fetch(monkeypatch):
    """Replaces the sheet download and returns the list of requested URLs."""
    urls = []

    def fake_get(url, *args, **kwargs):
        urls.append(url)
        return _Resp(200, "First Name,Business Email\nAda,ada@example.com\n")

    monkeypatch.setattr(sheets.httpx, "get", fake_get)
    return urls


@pytest.mark.unit
def test_paste_url_with_gid_fragment_captures_the_gid():
    spreadsheet_id, gid = sheets.parse_sheet_url(FRAGMENT_ONLY_URL)
    assert spreadsheet_id == SPREADSHEET_ID
    assert gid == REAL_GID


@pytest.mark.unit
def test_paste_url_with_query_and_fragment_gid_uses_the_real_value():
    spreadsheet_id, gid = sheets.parse_sheet_url(USER_URL)
    assert spreadsheet_id == SPREADSHEET_ID
    assert gid == REAL_GID


@pytest.mark.unit
def test_blank_gid_is_not_coerced_to_zero(monkeypatch):
    campaign, result = _create_campaign(monkeypatch, sheet_url=BARE_URL)

    assert campaign["sheet_gid"] is None
    assert result["sheet_gid"] is None

    urls = _capture_fetch(monkeypatch)
    table = sheets.fetch_table(BARE_URL, gid=campaign["sheet_gid"], sheet_tab="")
    assert table["gid"] in (None, "")
    assert "gid=" not in table["request_url"]
    assert urls == [table["request_url"]]


@pytest.mark.unit
def test_explicit_gid_is_preserved(monkeypatch):
    campaign, result = _create_campaign(
        monkeypatch, sheet_url=BARE_URL, sheet_gid=REAL_GID
    )

    assert campaign["sheet_gid"] == REAL_GID
    assert result["sheet_gid"] == REAL_GID

    _capture_fetch(monkeypatch)
    table = sheets.fetch_table(
        BARE_URL, gid=campaign["sheet_gid"], sheet_tab=""
    )
    assert table["request_url"].endswith(f"&gid={REAL_GID}")


@pytest.mark.unit
def test_blank_field_uses_the_gid_from_the_pasted_url(monkeypatch):
    """The reported bug: blank field + URL gid must fetch that gid, not 0."""
    campaign, _ = _create_campaign(monkeypatch, sheet_url=USER_URL)
    # The gid is resolved and stored at write time, so the record names its tab.
    assert campaign["sheet_gid"] == REAL_GID
    assert campaign["sheet_url"] == USER_URL

    _capture_fetch(monkeypatch)
    table = sheets.fetch_table(
        campaign["sheet_url"], gid=campaign["sheet_gid"], sheet_tab=""
    )
    assert table["gid"] == REAL_GID
    assert f"&gid={REAL_GID}" in table["request_url"]
    assert "gid=0" not in table["request_url"]


@pytest.mark.unit
def test_legacy_campaign_with_fabricated_zero_adopts_its_url_gid(monkeypatch):
    """Rows saved by the old app hold a fabricated "0"; the URL gid repairs it once."""
    legacy = {
        "id": "cmp_legacy",
        "name": "Legacy",
        "sheet_url": USER_URL,
        "sheet_gid": "0",
    }
    state = _empty_state()
    state["campaigns"].append(legacy)
    monkeypatch.setattr(outreach, "_mutate", lambda mutator: mutator(state))
    # load_state normalises every campaign, which is where the legacy row is
    # repaired. Mirror that instead of returning the raw document.
    monkeypatch.setattr(outreach, "load_state", lambda: outreach._normalise(state))

    repaired = outreach.get_campaign("cmp_legacy")
    assert repaired["sheet_gid"] == REAL_GID

    # A deliberate 0 written after the repair is not overwritten again.
    legacy["sheet_gid"] = "0"
    assert outreach.get_campaign("cmp_legacy")["sheet_gid"] == "0"


@pytest.mark.unit
def test_legacy_zero_without_url_gid_falls_back_to_first_sheet(monkeypatch):
    """The reported campaign: URL has no gid, so fabricated 0 must become None."""
    legacy = {
        "id": "cmp_legacy_plain",
        "name": "Legacy plain",
        "sheet_url": f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit?usp=sharing",
        "sheet_gid": "0",
    }
    state = _empty_state()
    state["campaigns"].append(legacy)
    monkeypatch.setattr(outreach, "load_state", lambda: outreach._normalise(state))

    assert outreach.get_campaign("cmp_legacy_plain")["sheet_gid"] is None


@pytest.mark.unit
def test_repair_is_idempotent():
    """The repair marker lives on the campaign record, so a restart re-runs the
    repair; that must be a no-op once the record is repaired."""
    record = {"id": "cmp_repeat", "sheet_url": USER_URL, "sheet_gid": "0"}

    outreach._repair_fabricated_gid(record)
    assert record["sheet_gid"] == REAL_GID
    assert record["sheet_gid_repaired"] is True

    # Simulate the marker being consulted again even if the gid looks stale.
    record["sheet_gid"] = "0"
    outreach._repair_fabricated_gid(record)
    assert record["sheet_gid"] == "0"

    # A fresh process sees the persisted marker and does not rewrite either.
    assert record["sheet_gid_repaired"] is True


@pytest.mark.unit
def test_repair_is_stable_if_rerun_from_scratch():
    """Losing the marker must only cost a repeat of the same, deterministic repair."""
    first = outreach._repair_fabricated_gid(
        {"id": "cmp_a", "sheet_url": USER_URL, "sheet_gid": "0"}
    )
    record = {"id": "cmp_a", "sheet_url": USER_URL, "sheet_gid": "0"}
    outreach._repair_fabricated_gid(record)
    assert first is None  # helper mutates in place
    assert record["sheet_gid"] == REAL_GID
    outreach._repair_fabricated_gid(record)
    assert record["sheet_gid"] == REAL_GID


@pytest.mark.unit
def test_legacy_zero_matching_a_url_gid_of_zero_is_kept(monkeypatch):
    """A URL that really says gid=0 means 0 was deliberate -- never rewrite it."""
    legacy = {
        "id": "cmp_legacy_zero",
        "name": "Legacy zero",
        "sheet_url": f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit?gid=0#gid=0",
        "sheet_gid": "0",
    }
    state = _empty_state()
    state["campaigns"].append(legacy)
    monkeypatch.setattr(outreach, "load_state", lambda: outreach._normalise(state))

    assert outreach.get_campaign("cmp_legacy_zero")["sheet_gid"] == "0"
