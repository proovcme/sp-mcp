from contextlib import contextmanager

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


def test_index_updates_only_changed_documents_and_removes_old_derived_rows(config, monkeypatch):
    service = Service(config)
    first = fetched("""<title>СП 900.100.2099. Учебный документ</title>
    <div>7.1 Синтетический прежний текст для проверки поискового индекса и очистки старых данных.</div>""")
    new = fetched("""<title>СП 900.100.2099. Учебный документ (ред. от 01.01.2099)</title>
    <div>7.1 Синтетическая новая редакция для проверки поискового индекса и обновления результатов.</div>""")
    old = service.store.save(first, parse_document(first))
    service.retrieval.sync()
    with service.store.db() as db:
        old_entry = db.execute("SELECT id FROM retrieval_segments WHERE kind='text'").fetchone()[0]
        db.execute("INSERT INTO retrieval_vectors VALUES (?, 'synthetic', x'00')", (old_entry,))
    with monkeypatch.context() as patch:
        patch.setattr(service.store, "get", lambda *args: pytest.fail("Warm index reopened a snapshot"))
        assert service.retrieval.search("прежний", mode="lexical")["items"]
    # A separate service can save changes to the same shared database.
    replacement = Service(config).store.save(new, parse_document(new))
    hits = service.retrieval.search("редакция", mode="lexical")["items"]
    assert [hit["snapshot_id"] for hit in hits] == [replacement["snapshot_id"]]
    assert not service.retrieval.search("прежний", mode="lexical")["items"]
    with service.store.db() as db:
        assert db.execute("SELECT count(*) FROM retrieval_vectors").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM retrieval_fts").fetchone()[0] == 1
    assert service.store.get(old["snapshot_id"])["snapshot_id"] == old["snapshot_id"]


def test_search_pages_group_fragments_with_documents(config):
    service = Service(config)
    for number in range(900, 903):
        raw = fetched(
            f"""<title>СП {number}.100.2099. Бытовая канализация</title>
        <div>7.1 Бытовая канализация описана в синтетическом примере для проверки постраничной выдачи.</div>""",
            f"https://meganorm.ru/mega_doc/norm/sp/0/demo-{number}.html",
        )
        service.store.save(raw, parse_document(raw))
    found = set()
    for page in range(1, 4):
        result = service.search("бытовая канализация", online=False, page=page, limit=1)
        assert len(result["text_hits"]) == 1
        code = result["text_hits"][0]["code"]
        assert code not in found
        found.add(code)
        assert result["more_results_possible"] is (page < 3)


def test_index_build_reads_snapshots_in_its_write_transaction(config, monkeypatch):
    service = Service(config)
    connect = service.store.db

    @contextmanager
    def small_cache():
        with connect() as db:
            db.execute("PRAGMA cache_size=8")
            db.execute("PRAGMA busy_timeout=50")
            yield db

    monkeypatch.setattr(service.store, "db", small_cache)
    for number in (900, 901):
        paragraphs = "".join(
            f"<p>7.{i} Синтетическое требование проверяет индексацию сохранённых документов.</p>"
            for i in range(200)
        )
        raw = fetched(
            f"<title>СП {number}.100.2099</title>{paragraphs}",
            f"https://meganorm.ru/mega_doc/norm/sp/0/demo-{number}.html",
        )
        service.store.save(raw, parse_document(raw))
    assert service.retrieval.search("синтетическое")["index"]["total_segments"] == 400
