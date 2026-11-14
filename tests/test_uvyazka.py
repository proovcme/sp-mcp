import json

import pytest
from conftest import fetched

from sp_mcp.errors import NormsError
from sp_mcp.parsers import parse_document
from sp_mcp.service import Service
from sp_mcp.uvyazka import bridge_call, prepare_link


def setup_link(config, synthetic_html):
    service = Service(config)
    raw = fetched(synthetic_html)
    doc = service.store.save(raw, parse_document(raw))
    report = service.store.save_verification(
        {
            "code": doc["code"],
            "snapshot_id": doc["snapshot_id"],
            "document_status": "unconfirmed",
            "verification": "unconfirmed",
            "text_revision_status": "unconfirmed",
            "evidence": [],
            "as_of": "2026-01-01",
        }
    )
    return service, doc, report


def test_prepared_uvyazka_link_keeps_norm_and_project_sources_separate(config, synthetic_html):
    service, doc, report = setup_link(config, synthetic_html)
    project_source = {"doc_id": "source-1", "sha256": "b" * 64}
    result = prepare_link(
        service,
        "synthetic-project",
        project_source,
        doc["snapshot_id"],
        report["verification_id"],
        clause="7.1.1",
    )
    assert not result["published"]
    args = result["arguments"]
    assert args["table_id"] is None
    assert args["related_sources"][0]["sha256"] == "b" * 64
    assert args["data"]["source"]["sha256"] == doc["provenance"]["sha256"]
    assert args["data"]["accepted"] is False
    assert args["data"]["applicability"] == "not_assessed"
    assert args["data"]["clause"]["occurrences"][0]["text"].startswith("7.1.1")
    assert (
        prepare_link(
            service,
            "synthetic-project",
            project_source,
            doc["snapshot_id"],
            report["verification_id"],
            clause="7.1.1",
        )["arguments"]["request_id"]
        == args["request_id"]
    )


def test_cannot_attach_status_of_another_snapshot(config, synthetic_html):
    service, doc, report = setup_link(config, synthetic_html)
    wrong = service.store.save_verification({"code": doc["code"], "snapshot_id": "wrong"})
    with pytest.raises(NormsError, match="exact loaded snapshot"):
        prepare_link(
            service,
            "synthetic-project",
            {"doc_id": "s", "sha256": "b" * 64},
            doc["snapshot_id"],
            wrong["verification_id"],
        )


def test_uvyazka_correction_requires_reason(config, synthetic_html):
    service, doc, report = setup_link(config, synthetic_html)
    with pytest.raises(NormsError, match="correction reason"):
        prepare_link(
            service,
            "synthetic-project",
            {"doc_id": "s", "sha256": "b" * 64},
            doc["snapshot_id"],
            report["verification_id"],
            expected_revision=1,
        )


def test_unconfigured_bridge_is_explicit(monkeypatch):
    monkeypatch.delenv("SP_MCP_UVYAZKA_COMMAND", raising=False)
    with pytest.raises(NormsError, match="optional Uvyazka"):
        bridge_call("list_projects", {})


def test_modified_verification_fails_integrity(config, synthetic_html):
    service, doc, report = setup_link(config, synthetic_html)
    with service.store.db() as db:
        payload = json.loads(db.execute("SELECT payload FROM verifications").fetchone()[0])
        payload["verification"] = "confirmed"
        db.execute("UPDATE verifications SET payload=?", (json.dumps(payload),))
    with pytest.raises(NormsError, match="modified"):
        service.store.verification(report["verification_id"])
