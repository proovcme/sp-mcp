import pytest
from conftest import fetched, official_html

from sp_mcp.errors import NormsError
from sp_mcp.identity import change_number, designation, parse_date, status_value
from sp_mcp.parsers import parse_candidates, parse_document, pdf_cover_identity


def test_pdf_cover_identity_needs_unique_title_evidence():
    reference = "Учебная ссылка на СП 901.100.2099 в предисловии."
    assert pdf_cover_identity(reference + "\nСП\n900.100.2099", None) == "СП 900.100.2099"
    for cover, title, expected in (
        (reference, None, None),
        ("СП 900.100.2099\nСП 901.100.2099", None, None),
        ("СП 900.100.2099", "СП 901.100.2099", None),
        ("СП 900.100.2099", None, "СП 901.100.2099"),
    ):
        with pytest.raises(NormsError, match="unique PDF designation"):
            pdf_cover_identity(cover, title, expected)


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("СП 900.100.2099. Свод правил", "СП 900.100.2099"),
        ("ГОСТ\u00a0Р 21.999—2099", "ГОСТ Р 21.999-2099"),
        ("сп900.100.2099", "СП 900.100.2099"),
        ("СП 900-107-2099. Учебный пример", "СП 900-107-2099"),
        ("СанПиН 9.9.9999-99", "САНПИН 9.9.9999-99"),
        ("Произвольное описание", None),
    ],
)
def test_identity(text, code):
    assert designation(text) == code


def test_change_number_preserves_numero_and_base_identity():
    assert change_number("Изменение №6 к СП 900.100.2099") == 6
    assert change_number("Изменение No6 к СП 900.100.2099") == 6
    assert change_number("СП 900.100.2099 (с изменением №6)") is None


def test_dates_and_status_are_not_guessed():
    assert parse_date("1 июля 2021 года") == "2021-07-01"
    assert parse_date("31.02.2020") is None
    assert parse_date("с даты опубликования") is None
    assert status_value("не действует") == "cancelled"
    assert status_value("Заменен") == "replaced"
    assert status_value("действует частично") == "unknown"


def test_div_paragraphs_images_tables_and_edition(synthetic_html):
    doc = parse_document(fetched(synthetic_html))
    assert doc["code"] == "СП 900.100.2099"
    assert doc["edition_date"] == "2099-02-01"
    assert doc["included_changes"] == [1, 2]
    assert doc["revisions"][0]["date"] == "2099-03-02"
    assert "7.1.1 Синтетическое" in doc["text"]
    assert "ИЗОБРАЖЕНИЕ: https://meganorm.ru/mega_doc/norm/sp/0/formula.gif" in doc["text"]
    assert doc["tables"][0]["rows"][0][0]["rowspan"] == 2
    assert doc["tables"][0]["rows"][1][0]["colspan"] == 2
    for block in doc["blocks"]:
        assert doc["text"][block["start"] : block["end"]] == block["text"]


def test_official_amendment_is_not_base_card():
    doc = parse_document(fetched(official_html(change=6), "https://new.gostinfo.ru/catalog/sp/999"))
    assert doc["change_number"] == 6
    assert doc["declared_status"] == "active"
    assert doc["effective_from"] == "2020-02-01"
    assert doc["text_status"] == "metadata_only"


def test_candidates_keep_discovery_distinct_from_confirmed_status():
    html = """<a href="/catalog/sp/1"><strong>СП 900.100.2099</strong>Действует</a>
    <a href="/catalog/sp/2"><strong>Изменение №1 к СП 900.100.2099</strong></a>"""
    items = parse_candidates(fetched(html, "https://new.gostinfo.ru/catalog?catalog=sp&q=test"))
    assert len(items) == 2
    assert [i["change_number"] for i in items] == [None, 1]
    assert all(i["match_status"] == "candidate" for i in items)
