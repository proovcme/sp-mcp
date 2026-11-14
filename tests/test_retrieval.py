import pytest
from conftest import fetched

from sp_mcp.linguistics import concepts, match_span, stems
from sp_mcp.parsers import parse_document
from sp_mcp.service import Service


def test_russian_wordforms_and_concept_groups_are_deterministic():
    assert stems("газовое пожаротушение") == stems("газового пожаротушения")
    assert set(stems("тушить")) & set().union(*concepts("пожаротушение"))
    assert match_span("бытовая канализация", "Выпуски бытовой канализации здания") == 2
    assert (
        match_span(
            "бытовая канализация", "Бытовые помещения и отдельные технические помещения с канализацией"
        )
        > 2
    )


@pytest.mark.parametrize("mode", ["lexical", "concept"])
def test_model_free_search_does_not_start_an_embedding_worker(config, mode, monkeypatch):
    service = Service(config)
    raw = fetched("""<title>СП 900.100.2099. Учебный документ</title>
        <div>7.1.1 Синтетические требования к установкам газового пожаротушения для учебного примера.</div>""")
    saved = service.store.save(raw, parse_document(raw))
    monkeypatch.setattr(
        service.retrieval, "provider", lambda: pytest.fail("Model-free search launched a model")
    )
    result = service.search("газовое пожаротушение", online=False, mode=mode)
    assert result["text_hits"][0]["snapshot_id"] == saved["snapshot_id"]
    assert result["text_hits"][0]["clause"] == "7.1.1"
    assert result["retrieval"]["model_used"] is False


def test_concepts_find_related_terms_and_keep_exact_source_offsets(config):
    service = Service(config)
    raw = fetched("""<title>СП 900.100.2099. Учебный документ</title>
        <div>7.1.1 Тушение серверных помещений описано в этом полностью синтетическом примере.</div>
        <div>Приложение Д. Синтетическое тушение серверных помещений в приложении.</div>""")
    doc = service.store.save(raw, parse_document(raw))
    result = service.retrieval.search("пожаротушение серверная", mode="concept")
    hits = [item for item in result["items"] if item["kind"] == "text"]
    assert hits
    for hit in hits:
        assert doc["text"][hit["start"] : hit["end"]] == hit["text"]
        if hit["text"].startswith("Приложение"):
            assert hit["clause"] is None


def test_unprepared_neural_index_never_silently_claims_semantic_results(config):
    result = Service(config).retrieval.search("тестовый запрос", mode="hybrid")
    assert result["semantic_limitation"] == "SEMANTIC_NOT_CONFIGURED"
    assert result["model_used"] is False


def test_list_excerpt_keeps_scope_exclusion_and_literal_offsets(config):
    service = Service(config)
    raw = fetched("""<title>СП 920.100.2099. Канализация зданий</title>
        <div>1.2 Настоящий синтетический пример не распространяется на:</div>
        <div>а) производственную канализацию учебного промышленного объекта;</div>
        <div>б) бытовую канализацию учебного помещения.</div>""")
    doc = service.store.save(raw, parse_document(raw))
    result = service.retrieval.search("производственная канализация")
    hit = next(i for i in result["items"] if i["kind"] == "text" and "производственную" in i["text"])
    assert "не распространяется" in hit["text"]
    assert hit["scope_exclusion"] is True
    assert hit["clause"] == "1.2"
    assert doc["text"][hit["start"] : hit["end"]] == hit["text"]
