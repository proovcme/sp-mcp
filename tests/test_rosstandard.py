import pytest
from conftest import fetched, official_html, ros_empty_catalogue

from sp_mcp.errors import NormsError
from sp_mcp.parsers import parse_document, soup_for
from sp_mcp.rosstandard import catalogue, search_url
from sp_mcp.service import Service

CODE = "СП 900.100.2099"
ROOT = "https://protect.gost.ru"
CARD = "/sp/details/00000000-0000-4000-8000-000000000001"
CHANGE = "/sp/changesdetails/00000000-0000-4000-8000-000000000002"


def listing(url, next_page=""):
    return fetched(
        '<form action="/sp" method="get"><input name="search"></form>'
        "<table><tbody><tr>"
        f'<td><a href="{CARD}">{CODE}</a></td><td>Учебная безопасность</td><td>1</td><td>✓ Действует</td>'
        "</tr></tbody></table>" + next_page,
        url,
    )


def card_html(change=False):
    title = f"Изменение №1 к {CODE}" if change else CODE
    date_label = "Дата введения изменения в действие" if change else "Дата введения в действие"
    number = "<div><span>Номер изменения</span><p>1</p></div>" if change else ""
    tab = (
        ""
        if change
        else (
            f'<div data-tab-panel="changes"><a href="{CHANGE}">Изменение №1 к {CODE}</a>'
            f'<a href="{CHANGE}">Изменение №1 к {CODE}</a></div>'
        )
    )
    return (
        f'<header><div><div><h1>{title}</h1></div></div><div class="flex-shrink-0">Действует</div></header>'
        f'<div data-tab-panel="bibliography"><div><span>Обозначение</span><p>{CODE}</p></div>'
        f"<div><span>{date_label}</span><p>01.02.2020</p></div>{number}"
        "<div><span>Обозначение заменяемого(ых)</span><p>СП 900.100.2098</p></div></div>" + tab
    )


def test_rosstandard_listing_keeps_exact_code_and_next_page():
    url = search_url(CODE, "sp", 1)
    next_url = search_url(CODE, "sp", 2)
    raw = listing(url, f'<a href="{next_url}">Следующая</a>')
    items, following = catalogue(raw, soup_for(raw))
    assert following == [next_url]
    assert (items[0]["code"], items[0]["declared_status"], items[0]["url"]) == (CODE, "active", ROOT + CARD)
    empty = ros_empty_catalogue(url)
    assert catalogue(empty, soup_for(empty)) == ([], [])
    with pytest.raises(NormsError, match="could not be identified"):
        catalogue(raw, soup_for(fetched("<html>Вход</html>", url)))


@pytest.mark.parametrize("conflicting,verification", [(False, "confirmed"), (True, "conflict")])
def test_check_combines_public_rosstandard_amendments_and_institute_cards(config, conflicting, verification):
    class Network:
        def fetch(self, url):
            if url.startswith(ROOT + "/sp?"):
                return listing(url)
            if url == ROOT + CARD:
                return fetched(card_html(), url)
            if url == ROOT + CHANGE:
                return fetched(card_html(change=True), url)
            if "/catalog?" in url:
                return fetched(
                    f'<a href="/catalog/sp/999"><strong>{CODE}</strong> Действует Учебный свод</a>', url
                )
            if url.endswith("/999"):
                return fetched(official_html(effective="02.02.2020" if conflicting else "01.02.2020"), url)
            return fetched("<html>Синтетический каталог без записей</html>", url)

    service = Service(config, Network())
    report = service.check(CODE)
    assert report["verification"] == verification
    assert report["supersedes"] == ["СП 900.100.2098"]
    assert report["required_changes"] == [1]
    assert report["source_coverage"] == {
        "rosstandard": {"cards": 2, "complete": True},
        "gostinfo": {"cards": 1, "complete": True},
    }
    assert {(record["provenance"]["source"], record["change_number"]) for record in report["evidence"]} == {
        ("rosstandard", None),
        ("rosstandard", 1),
        ("gostinfo", None),
    }
    stored = service.store.latest(ROOT + CARD)
    assert stored["text_status"] == "metadata_only"
    assert len(stored["amendment_links"]) == 1
    assert {item["source"] for item in service.search(CODE)["items"]} == {"rosstandard", "gostinfo"}


def test_rosstandard_change_identity_keeps_number_separate_from_document_code():
    raw = fetched(card_html(change=True), ROOT + CHANGE)
    doc = parse_document(raw)
    assert (doc["code"], doc["change_number"], doc["effective_from"]) == (CODE, 1, "2020-02-01")
    assert doc["edition_date"] is None
