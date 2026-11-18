import pytest
from conftest import fetched

from sp_mcp.errors import NormsError
from sp_mcp.parsers import parse_document
from sp_mcp.service import Service


def save(service, html):
    raw = fetched(html)
    return service.store.save(raw, parse_document(raw))


def test_immutable_original_read_clause_search_and_table(config, synthetic_html):
    service = Service(config)
    doc = save(service, synthetic_html)
    ident, digest = doc["snapshot_id"], doc["provenance"]["sha256"]
    clause = service.read_clause(ident, "7.1.1", digest)
    assert clause["status"] == "found"
    text = clause["occurrences"][0]["text"]
    assert "Синтетическое требование" in text
    assert "Дополнительное условие" in text
    assert "7.1.2" not in text
    assert service.search_document(ident, "Синтетическое требование")["items"]
    assert service.table(ident, 0)["rows"][0][0]["rowspan"] == 2
    assert service.store.search_text("синтетич требован")
    assert not service.store.search_text("несуществующаяфраза")
    assert service.read(ident, limit=10)["next_offset"] == 10
    with pytest.raises(NormsError, match="Expected SHA"):
        service.read(ident, expected_sha256="0" * 64)
    (config.root / "raw" / digest).write_bytes(b"modified")
    with pytest.raises(NormsError, match="checksum"):
        service.read(ident)


def test_reparse_and_new_source_content_do_not_overwrite_snapshots(config, synthetic_html):
    service = Service(config)
    first = save(service, synthetic_html)
    second = save(service, synthetic_html.replace("Синтетическое требование", "Измененное требование"))
    assert first["snapshot_id"] != second["snapshot_id"]
    assert "Синтетическое" in service.store.get(first["snapshot_id"])["text"]
    diff = service.compare(first["snapshot_id"], second["snapshot_id"])
    assert any("Измененное" in line for line in diff["lines"])
    original = fetched(synthetic_html)
    parsed = parse_document(original) | {"limitations": ["Reparsed"]}
    third = service.store.save(original, parsed)
    assert third["snapshot_id"] != first["snapshot_id"]


def test_clause_duplicates_are_explicit(config, synthetic_html):
    service = Service(config)
    doc = save(service, synthetic_html.replace("</body>", "<div>7.1.1 Другое вхождение.</div></body>"))
    assert service.read_clause(doc["snapshot_id"], "7.1.1")["status"] == "ambiguous_occurrences"


def test_network_failure_is_not_not_found(config):
    class Offline:
        def fetch(self, url):
            raise NormsError("NETWORK_ERROR", "Offline", retryable=True)

    service = Service(config, Offline())
    result = service.search("СП 900.100.2099")
    assert result["status"] == "partial"
    assert result["source_errors"][0]["error_code"] == "NETWORK_ERROR"
    report = service.check("СП 900.100.2099")
    assert report["verification"] == "unconfirmed"
    assert report["source_errors"]
    assert service.store.verification(report["verification_id"])["verification"] == "unconfirmed"


def test_search_inside_document_only_consumes_one_page_of_matches(config, monkeypatch):
    import re

    service = Service(config)
    doc = save(service, "<title>СП 900.100.2099</title><p>" + "слово " * 20000 + "</p>")
    pattern = re.compile("слово", re.I)

    class BoundedMatches:
        def finditer(self, *args):
            for index, match in enumerate(pattern.finditer(*args)):
                assert index < 4, "Matcher consumed results after the requested page and lookahead"
                yield match

    monkeypatch.setattr(service.store, "get", lambda snapshot_id: doc)
    with monkeypatch.context() as patch:
        patch.setattr("sp_mcp.service.re.compile", lambda *args: BoundedMatches())
        page = service.search_document(doc["snapshot_id"], "слово", limit=3)
    assert len(page["items"]) == 3
    assert page["next_offset"] is not None


def test_batch_preserves_failed_input_and_continues(config):
    service = Service(config)
    result = service.batch_check(
        [{"input_id": "bad", "code": "random"}, {"input_id": "unsupported", "code": "СНиП 9.99.99-99"}]
    )
    assert [r["input_id"] for r in result["items"]] == ["bad", "unsupported"]
    assert result["items"][0]["result"]["error_code"] == "INVALID_ARGUMENT"
    assert result["items"][1]["result"]["verification"] == "unconfirmed"


@pytest.mark.parametrize(
    "value",
    [
        "https://evil.invalid/",
        "file:///etc/passwd",
        "http://meganorm.ru/",
        "https://meganorm.ru@evil.invalid/",
        "https://meganorm.ru:444/a",
    ],
)
def test_only_known_https_sources(config, value):
    with pytest.raises(NormsError, match="HTTPS"):
        Service(config).open(value)
