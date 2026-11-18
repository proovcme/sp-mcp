from datetime import date, datetime, timedelta, timezone

import pytest

from sp_mcp.verification import verify

CODE = "СП 900.100.2099"


def card(change=None, start="2020-01-01", status="active", checked=None):
    return {
        "code": CODE,
        "change_number": change,
        "declared_status": status,
        "effective_from": start,
        "effective_to": None,
        "fields": {},
        "snapshot_id": str(change),
        "provenance": {
            "source_url": "https://new.gostinfo.ru/catalog/sp/999",
            "sha256": "a" * 64,
            "checked_at": checked or datetime.now(timezone.utc).isoformat(),
        },
    }


def loaded(changes=None, text=""):
    return {
        "code": CODE,
        "change_number": None,
        "included_changes": changes or [],
        "text_status": "extracted_html",
        "text": text,
        "provenance": {"source_url": "https://meganorm.ru/example.html"},
    }


def test_active_document_can_have_outdated_text():
    report = verify(CODE, [card(), card(1), card(2)], loaded([1]), search_complete=True)
    assert report["document_status"] == "active"
    assert report["text_revision_status"] == "outdated"
    assert report["missing_changes"] == [2]
    assert report["applicability"] == "not_assessed"


def test_amendment_card_cannot_prove_base_status():
    report = verify(CODE, [card(1)], search_complete=True)
    assert report["document_status"] == "unconfirmed"


def test_conflicting_effective_dates_remain_visible():
    text = "Изменение N 2, утв. приказом, введено в действие с 13.07.2020."
    report = verify(CODE, [card(), card(2, start="2020-07-07")], loaded([2], text), search_complete=True)
    assert report["verification"] == "conflict"
    assert report["conflicts"][0]["official"] == "2020-07-07"
    assert report["conflicts"][0]["text_source"] == "2020-07-13"


def test_historical_and_future_status_not_inferred_from_current_catalog():
    historical = verify(CODE, [card()], as_of="2022-01-01", search_complete=True)
    assert historical["document_status"] == "unconfirmed"
    future = verify(CODE, [card()], as_of="2099-01-01", search_complete=True)
    assert future["document_status"] == "unconfirmed"


def test_cancelled_document_is_not_reported_active():
    report = verify(CODE, [card(status="cancelled")], search_complete=True)
    assert report["document_status"] == "cancelled"


def test_stale_or_partial_source_cannot_confirm():
    stale = (datetime.now(timezone.utc) - timedelta(days=9)).isoformat()
    assert verify(CODE, [card(checked=stale)], search_complete=True)["verification"] == "unconfirmed"
    assert verify(CODE, [card()], search_complete=False)["verification"] == "unconfirmed"
    assert (
        verify(CODE, [card()], search_complete=True, errors=[{"error_code": "NETWORK_ERROR"}])["verification"]
        == "unconfirmed"
    )


def test_future_amendment_is_not_a_required_current_change():
    start = (date.today() + timedelta(days=100)).isoformat()
    report = verify(CODE, [card(), card(1, start=start)], loaded([1]), search_complete=True)
    assert report["required_changes"] == []
    assert report["text_revision_status"] == "contains_future_changes"


@pytest.mark.parametrize(
    "target,expected", [("2019-12-31", "not_yet_effective"), ("2199-01-02", "cancelled")]
)
def test_explicit_base_dates_cannot_confirm_past_or_future_status(target, expected):
    report = verify(CODE, [card() | {"effective_to": "2199-01-01"}], as_of=target, search_complete=True)
    assert (report["document_status"], report["verification"]) == (expected, "unconfirmed")


def test_cancelled_status_with_future_end_is_conflicting():
    report = verify(CODE, [card(status="cancelled") | {"effective_to": "2199-01-01"}], search_complete=True)
    assert report["verification"] == "conflict"
    assert report["document_status"] == "unconfirmed"


@pytest.mark.parametrize("amendments", [[], [card(1)]])
def test_missing_official_amendment_is_not_confirmed(amendments):
    report = verify(CODE, [card(), *amendments], loaded([1, 2]), search_complete=True)
    assert report["unknown_changes"] == ([2] if amendments else [1, 2])
    assert report["text_revision_status"] == "unconfirmed"
    assert report["document_status"] == "active"


def test_undated_or_conflicting_official_amendment_does_not_confirm_text():
    incomplete = verify(CODE, [card(), card(1), card(2, start=None)], loaded([1, 2]), search_complete=True)
    assert incomplete["undated_changes"] == [2]
    assert incomplete["text_revision_status"] == "unconfirmed"
    conflicting = verify(
        CODE, [card(), card(1), card(1, start="2021-01-01")], loaded([1]), search_complete=True
    )
    assert conflicting["verification"] == conflicting["text_revision_status"] == "conflict"
    current = verify(CODE, [card(), card(1)], loaded([1]), search_complete=True)
    assert current["text_revision_status"] == "amendments_accounted_for"
