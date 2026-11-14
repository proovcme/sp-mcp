"""Optional contract check against an installed Uvyazka, using only a synthetic temporary project."""

import json
import os
import subprocess

import pytest
from test_uvyazka import setup_link

from sp_mcp.uvyazka import bridge_call, prepare_link


@pytest.mark.uvyazka
@pytest.mark.skipif(not os.getenv("SP_TEST_UVYAZKA_PYTHON"), reason="Local Uvyazka installation not selected")
def test_installed_uvyazka_accepts_versioned_link_and_rejects_changed_project_source(
    config, synthetic_html, tmp_path, monkeypatch
):
    executable = os.environ["SP_TEST_UVYAZKA_PYTHON"]
    root, home = tmp_path / "project-source", tmp_path / "uvyazka-state"
    root.mkdir()
    source = root / "synthetic-reference.txt"
    source.write_text("Учебная ссылка на СП 900.100.2099. Данные синтетические.", encoding="utf-8")
    script = """import json,sys
from uvyazka.core import Workspace
from uvyazka.ingest import scan
w=Workspace(sys.argv[1]);w.create_project('synthetic-project','Synthetic integration',sys.argv[2])
scan(w,'synthetic-project')
print(json.dumps(w.find_documents('synthetic-project'),ensure_ascii=True))
"""
    created = subprocess.run(
        [executable, "-X", "utf8", "-c", script, str(home), str(root)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    documents = json.loads(created.stdout)
    doc = documents["documents"][0]
    monkeypatch.setenv("SP_MCP_UVYAZKA_COMMAND", executable)
    monkeypatch.setenv("SP_MCP_UVYAZKA_ARGS", '["-X","utf8","-m","uvyazka.server"]')
    monkeypatch.setenv("SP_MCP_UVYAZKA_HOME", str(home))
    service, snapshot, report = setup_link(config, synthetic_html)
    payload = prepare_link(
        service,
        "synthetic-project",
        {"doc_id": doc["doc_id"], "sha256": doc["sha256"]},
        snapshot["snapshot_id"],
        report["verification_id"],
        clause="7.1.1",
    )
    saved = bridge_call(payload["tool"], payload["arguments"])
    assert saved["accepted"] is False
    assert saved["revision"] == 1
    retry = bridge_call(payload["tool"], payload["arguments"])
    assert retry["revision"] == 1
    source.write_text("Changed synthetic project reference.", encoding="utf-8")
    changed = bridge_call(
        "get_project_structure",
        {"project_id": "synthetic-project", "kind": "analyses", "analysis_id": saved["analysis_id"]},
    )
    assert changed["items"][0]["current_sources"] is False
