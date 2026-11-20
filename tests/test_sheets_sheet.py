import logging
import pytest

from backend import sheets

logger = logging.getLogger(__name__)


class _Resp:
    def __init__(self, status_code, text, js=None):
        self.status_code = status_code
        self.text = text
        self.content = text.encode()
        self._js = js

    def json(self):
        return self._js or {"values": []}


def _dispatch(sheets_get=None, csv_text="a,b,c\n1,2,3\n", csv_status=200):
    """Return a ``httpx.get`` side-effect that answers the right shape per URL.

    When the requested URL is the Sheets API metadata (``spreadsheets.get``)
    endpoint the call returns ``sheets.properties.title``; every other call
    (the public CSV / gviz fetch) returns ``csv_text``.
    """

    def side_effect(*args, **kwargs):
        request_url = (args[0] if args else kwargs.get("url", ""))
        if "sheets.googleapis.com" in request_url:
            return _Resp(200, "", js=sheets_get)
        return _Resp(csv_status, csv_text)

    return side_effect


@pytest.mark.unit
def test_specific_gid_400_raises_and_names_gid():
    """A requested gid that 400s must raise a loud SheetError with no fallback."""
    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(
            sheets.httpx, "get",
            lambda *a, **k: _Resp(400, "Invalid requests"),
        )
        with pytest.raises(sheets.SheetError) as excinfo:
            sheets._sheet_info(
                "abc123", gid="679250682", sheet_tab="",
                use_service_account=False, bearer_token="",
            )
    message = str(excinfo.value)
    assert "679250682" in message
    assert "abc123" in message
    assert "Click the correct tab" in message
    # The public CSV path must not fall back to the first sheet.
    assert "first sheet" not in message


@pytest.mark.unit
def test_no_gid_falls_back_to_first_sheet_without_warning():
    """A request with no gid uses the first sheet and logs the URL."""
    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(
            sheets.httpx, "get",
            lambda *a, **k: _Resp(200, "a,b,c\n1,2,3\n"),
        )
        result = sheets._sheet_info(
            "abc123", gid="", sheet_tab="",
            use_service_account=False, bearer_token="",
        )
    assert result["request_url"] == "https://docs.google.com/spreadsheets/d/abc123/export?format=csv"
    assert result["used_tab"] == "first sheet"
    assert result["tab_exists"] is True
    # Public CSV with no gid reads the first sheet by design; no error is raised.


@pytest.mark.unit
def test_tab_name_that_does_not_exist_raises_with_available_tabs():
    """Requesting a tab that is not in the sheet must raise naming the tab and
    listing the tabs that do exist (from spreadsheets.get)."""
    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(
            sheets.httpx, "get",
            _dispatch(
                sheets_get={
                    "sheets": [
                        {"properties": {"title": "Leads"}},
                        {"properties": {"title": "Archive"}},
                    ]
                },
                csv_text="",
            ),
        )
        with pytest.raises(sheets.SheetError) as excinfo:
            sheets._sheet_info(
                "abc123", gid="", sheet_tab="Lead",
                use_service_account=False, bearer_token="",
            )
    message = str(excinfo.value)
    assert "Lead" in message
    assert "abc123" in message
    assert "Leads" in message
    assert "Archive" in message


@pytest.mark.unit
def test_preview_reports_missing_fields():
    """A sheet whose headers miss three of the five target fields must flag
    each missing field and mark the matching fields as found."""
    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(
            sheets.httpx, "get",
            _dispatch(
                sheets_get={"sheets": [{"properties": {"title": "Leads"}}]},
                csv_text="First Name,Company Name,Position / Job Title\r\n1,2,3\r\n",
            ),
        )
        table = sheets.preview_sheet(
            "https://docs.google.com/spreadsheets/d/abc12345678901234567890",
            gid="", tab="Leads",
        )
        assert table["headers"] == ["First Name", "Company Name", "Position / Job Title"]
    field_map = table["field_map"]
    assert field_map["first_name"]["found"] is True
    assert field_map["company_name"]["found"] is True
    assert field_map["position"]["found"] is True
    assert field_map["personal_email"]["missing"] is True
    assert field_map["business_email"]["missing"] is True
    assert table["used_tab"] == "Leads"
    assert table["tab_exists"] is True
    assert table["request_url"] != ""
    assert {_map["label"] for _map in table["missing_fields"]} == {
        "Personal Email", "Business Email",
    }


@pytest.mark.unit
def test_preview_greys_out_phone_columns():
    """A 'Phone' column must not map to any target field and must not appear in
    the mapping or usable columns."""
    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(
            sheets.httpx, "get",
            _dispatch(
                sheets_get={"sheets": [{"properties": {"title": "Leads"}}]},
                csv_text="First Name,Business Email,Phone,Something\r\n1,2,3,4\r\n",
            ),
        )
        result = sheets.preview_sheet(
            "https://docs.google.com/spreadsheets/d/abc12345678901234567890",
            gid="", tab="Leads",
        )
    field_map = result["field_map"]
    assert field_map["first_name"]["found"] is True
    assert field_map["business_email"]["found"] is True
    assert field_map["position"]["missing"] is True
    assert field_map["personal_email"]["missing"] is True
    assert field_map["company_name"]["missing"] is True
    # Phone is not a target field, so it must not appear in the mapping.
    assert "phone" not in result["mapping"]
    assert "Phone" not in result["mapping"].values()
    assert result["used_tab"] == "Leads"
    assert result["tab_exists"] is True
    assert result["request_url"] != ""


@pytest.mark.unit
def test_request_url_exposed_in_preview():
    """The preview response carries the exact URL the app will fetch."""
    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(
            sheets.httpx, "get",
            _dispatch(
                sheets_get={"sheets": [{"properties": {"title": "Leads"}}]},
                csv_text="a,b,c\r\n1,2,3\r\n",
            ),
        )
        result = sheets.preview_sheet(
            "https://docs.google.com/spreadsheets/d/abc12345678901234567890",
            gid="679250682", tab="",
        )
    assert result["request_url"] == (
        "https://docs.google.com/spreadsheets/d/abc12345678901234567890"
        "/export?format=csv&gid=679250682"
    )
