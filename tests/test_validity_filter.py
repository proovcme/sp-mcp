from datetime import datetime, timedelta, timezone

from conftest import fetched, official_html

from sp_mcp.parsers import parse_candidates, parse_document
from sp_mcp.service import Service
from sp_mcp.validity import validity_index
from sp_mcp.verification import today_moscow


def catalog(service, status="Отменен", change=False, checked=None):
    label = ("Изменение №1 к " if change else "") + "СП 900.100.2099"
    raw = fetched(
        f'<a href="/catalog/sp/999"><strong>{label}</strong> {status} Пожаротушение. Учебный пример</a>',
        "https://new.gostinfo.ru/catalog?q=synthetic",
    )
    if checked:
        raw["checked_at"] = checked
    service.store.add_candidates(parse_candidates(raw))


def text(service, code="СП 900.100.2099", extra=""):
    raw = fetched(
        f"<title>{code}. Учебное пожаротушение</title><div>{extra}</div>"
        "<div>7.1.1 Синтетическое пожаротушение в учебном примере.</div>",
        f"https://meganorm.ru/mega_doc/norm/sp/0/{code.replace(' ', '_')}.html",
    )
    return service.store.save(raw, parse_document(raw))


def test_inactive_filter_applies_to_titles_fragments_and_library_with_explicit_opt_out(config):
    service = Service(config)
    old = text(service)
    text(service, "СП 901.100.2099")
    catalog(service)
    result = service.search("пожаротушение", online=False)
    assert all(
        i["code"] != old["code"] for i in result["items"] + result["text_hits"] + result["related_items"]
    )
    assert result["validity_filter"]["excluded_count"] == 1
    assert service.library()["total"] == 1
    assert service.library(include_inactive=True)["total"] == 2
    restored = service.search("пожаротушение", online=False, include_inactive=True)
    assert any(
        i["code"] == old["code"] and i["validity"]["state"] == "cancelled" for i in restored["text_hits"]
    )
    assert service.document(old["snapshot_id"])["snapshot_id"] == old["snapshot_id"]


def test_unknown_is_visible_and_never_labelled_active(config):
    service = Service(config)
    text(service)
    result = service.search("пожаротушение", online=False)
    assert result["text_hits"][0]["validity"]["state"] == "unknown"


def test_amendment_status_and_expired_observation_do_not_hide_base_document(config):
    service = Service(config)
    text(service)
    catalog(service, change=True)
    assert service.library()["total"] == 1
    catalog(service, checked=(datetime.now(timezone.utc) - timedelta(days=9)).isoformat())
    assert service.library()["total"] == 1


def test_current_official_card_overrides_secondary_cancellation_note(config):
    service = Service(config)
    text(service, extra="Документ утратил силу с 01.01.2020 в учебном примере.")
    assert service.library()["total"] == 0
    raw = fetched(official_html(), "https://new.gostinfo.ru/catalog/sp/999")
    service.store.save(raw, parse_document(raw))
    index = validity_index(service.store)
    assert index["СП 900.100.2099"]["state"] == "active"
    assert service.library()["total"] == 2


def test_future_and_partial_cancellation_notes_do_not_hide_whole_document(config):
    service = Service(config)
    text(service, extra="Документ утратил силу с 01.01.2199.")
    text(service, "СП 901.100.2099", "Документ утратил силу в части требований с 01.01.2020.")
    assert service.library()["total"] == 2


def test_historical_verification_does_not_hide_today_but_current_one_does(config):
    service = Service(config)
    text(service)
    report = {"code": "СП 900.100.2099", "document_status": "cancelled", "verification": "confirmed"}
    service.store.save_verification(report | {"as_of": "2020-01-01"})
    assert service.library()["total"] == 1
    service.store.save_verification(report | {"as_of": today_moscow().isoformat()})
    assert service.library()["total"] == 0


def test_conflicting_official_statuses_stay_visible_as_conflict(config):
    service = Service(config)
    for ident, status in [(998, "Действует"), (999, "Отменен")]:
        raw = fetched(official_html(status=status), f"https://new.gostinfo.ru/catalog/sp/{ident}")
        service.store.save(raw, parse_document(raw))
    assert service.library()["total"] == 2
    assert all(i["validity"]["state"] == "conflict" for i in service.library()["items"])


def test_future_cancellation_date_is_not_treated_as_already_cancelled(config):
    service = Service(config)
    catalog(service)
    raw = fetched(official_html(status="Отменен"), "https://new.gostinfo.ru/catalog/sp/999")
    document = parse_document(raw)
    document["effective_to"] = "2199-01-01"
    service.store.save(raw, document)
    assert service.library()["total"] == 1
    assert service.library()["items"][0]["validity"]["state"] == "unknown"
