from conftest import fetched

from sp_mcp.parsers import parse_document
from sp_mcp.service import Service


def test_saved_html_table_is_read_in_place_with_source_spans_and_caption(config):
    service = Service(config)
    raw = fetched("""<title>СП 900.100.2099. Учебные таблицы</title><div>Таблица А.1 — Учебные величины</div>
        <table><tr><th rowspan="2">Группа</th><th colspan="2">Значения</th></tr>
        <tr><td>1,25</td><td>Не более 2</td></tr></table>""")
    doc = service.store.save(raw, parse_document(raw))
    result = service.read(doc["snapshot_id"])
    table = next(b for b in result["blocks"] if b["kind"] == "table")
    assert table["caption"] == "Таблица А.1 — Учебные величины"
    assert table["rows"][0][0]["rowspan"] == 2
    assert table["rows"][0][1]["colspan"] == 2
    assert table["rows"][1][0]["text"] == "1,25"
    assert result["text"] == doc["text"]
    assert service.document(doc["snapshot_id"])["table_captions"][0]["caption"] == table["caption"]


def test_box_table_recovers_source_spacing_without_rewriting_existing_snapshot(config):
    service = Service(config)
    line = "│   Учебная ячейка   │    1,25    │"
    raw = fetched(
        f"<title>СП 900.100.2099. Учебный документ</title><div>7.1 Учебный пункт</div><div>{line}</div>"
    )
    doc = service.store.save(raw, parse_document(raw))
    assert line not in doc["text"]  # Existing immutable extraction normalizes whitespace.
    result = service.read(doc["snapshot_id"])
    grid = next(b for b in result["blocks"] if b["kind"] == "preformatted")
    assert grid["display_text"] == line
    assert grid["layout_basis"] == "source_whitespace"
    assert service.store.get(doc["snapshot_id"])["text"] == doc["text"]
    clause = service.read_clause(doc["snapshot_id"], "7.1")
    assert clause["occurrences"][0]["blocks"][-1]["display_text"] == line
