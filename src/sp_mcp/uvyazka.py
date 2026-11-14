"""Optional MCP-to-MCP adapter. Never accesses Uvyazka's SQLite database directly."""

import asyncio
import json
import os
import re

from .errors import NormsError
from .storage import dump, sha


def prepare_link(
    service,
    project_id,
    project_source,
    snapshot_id,
    verification_id,
    clause=None,
    expected_revision=0,
    reason="",
):
    if not isinstance(project_id, str) or not re.fullmatch(r"[\w.-]{1,100}", project_id):
        raise NormsError("INVALID_ARGUMENT", "Use an exact registered Uvyazka project_id")
    if not isinstance(project_source, dict) or set(project_source) - {"doc_id", "sha256", "page_number"}:
        raise NormsError(
            "INVALID_ARGUMENT", "project_source requires registered doc_id, sha256, optional page_number"
        )
    if not project_source.get("doc_id") or not re.fullmatch(
        r"[0-9a-f]{64}", project_source.get("sha256", "")
    ):
        raise NormsError("INVALID_ARGUMENT", "Project source needs its exact original SHA-256")
    if "page_number" in project_source and (
        type(project_source["page_number"]) is not int or project_source["page_number"] < 1
    ):
        raise NormsError("INVALID_ARGUMENT", "page_number must be a physical PDF page, starting at 1")
    if (
        type(expected_revision) is not int
        or expected_revision < 0
        or expected_revision
        and not reason.strip()
    ):
        raise NormsError("INVALID_ARGUMENT", "Revisions require expected_revision and a correction reason")
    doc = service.store.get(snapshot_id)
    report = service.store.verification(verification_id)
    if report.get("snapshot_id") != snapshot_id or report["code"] != doc["code"]:
        raise NormsError(
            "VERIFICATION_IDENTITY_MISMATCH", "Verification must address this exact loaded snapshot"
        )
    excerpt = service.read_clause(snapshot_id, clause) if clause else None
    if excerpt and excerpt["status"] != "found":
        raise NormsError("CLAUSE_UNRESOLVED", "Resolve a unique clause occurrence before linking to Uvyazka")
    if excerpt and len(dump(excerpt).encode()) > 40000:
        raise NormsError("CLAUSE_TOO_LARGE", "Clause too large for a single project analysis")
    evidence = [
        {
            "snapshot_id": e["snapshot_id"],
            "provenance": e["provenance"],
            "change_number": e.get("change_number"),
        }
        for e in report.get("evidence", [])
    ]
    compact = {k: v for k, v in report.items() if k not in {"evidence", "search_evidence"}}
    compact["evidence"] = evidence
    data = {
        "schema": "sp-mcp.uvyazka-link/1",
        "code": doc["code"],
        "edition_date": doc["edition_date"],
        "snapshot_id": snapshot_id,
        "source": doc["provenance"],
        "verification": compact,
        "clause": excerpt,
        "applicability": "not_assessed",
        "accepted": False,
        "review_state": "unreviewed_external_normative_evidence",
        "freshness_note": "Project source changes are tracked by Uvyazka. Norm changes require a new SP check.",
    }
    stable = [project_id, project_source["doc_id"], doc["code"], clause]
    analysis_id = "sp-" + sha(dump(stable).encode())[:40]
    request = {
        "project_id": project_id,
        "analysis_id": analysis_id,
        "table_id": None,
        "title": f"Нормативная ссылка: {doc['code']}" + (f", п. {clause}" if clause else ""),
        "summary": (
            f"Статус: {report['document_status']}; проверка: {report['verification']}; "
            f"редакция текста: {report.get('text_revision_status', 'unconfirmed')}. "
            "Применимость к проекту не оценена; результат требует рассмотрения."
        ),
        "data": data,
        "reader": "model",
        "expected_revision": expected_revision,
        "reason": reason,
        "related_sources": [project_source | {"reason": "Нормативная ссылка в исходном документе проекта"}],
    }
    request["request_id"] = "sp-" + sha(dump(request).encode())
    if len(dump(request).encode()) > 120 * 1024:
        raise NormsError("LINK_TOO_LARGE", "Integration payload exceeds Uvyazka analysis limit")
    return {
        "tool": "save_structure_analysis",
        "arguments": request,
        "published": False,
        "limitations": [
            "Only Uvyazka can validate project membership and current project source bytes.",
            "Publishing stores unapproved evidence; it does not accept engineering requirements.",
        ],
    }


def bridge_call(name, arguments):
    command = os.getenv("SP_MCP_UVYAZKA_COMMAND")
    if not command:
        raise NormsError(
            "UVYAZKA_NOT_CONFIGURED", "Configure the optional Uvyazka MCP command or use the prepared payload"
        )
    try:
        args = json.loads(os.getenv("SP_MCP_UVYAZKA_ARGS", "[]"))
        if not isinstance(args, list) or not all(isinstance(x, str) for x in args):
            raise ValueError
    except ValueError as exc:
        raise NormsError("UVYAZKA_CONFIG_ERROR", "SP_MCP_UVYAZKA_ARGS must be a JSON string array") from exc
    if name not in {"list_projects", "save_structure_analysis", "get_project_structure"}:
        raise NormsError(
            "UVYAZKA_METHOD_NOT_ALLOWED", "This bridge exposes only registered project/link operations"
        )

    async def run():
        from mcp import Client
        from mcp.client.stdio import StdioServerParameters

        env = dict(os.environ)
        if os.getenv("SP_MCP_UVYAZKA_HOME"):
            env["UVYAZKA_HOME"] = os.environ["SP_MCP_UVYAZKA_HOME"]
        parameters = StdioServerParameters(command=command, args=args, env=env)
        # Uvyazka's established server uses the initialize/legacy handshake.
        # Selecting it explicitly avoids a failed modern discovery request in its logs.
        async with Client(parameters, mode="legacy") as client:
            result = await client.call_tool(name, arguments)
            if result.is_error:
                raise NormsError(
                    "UVYAZKA_REJECTED",
                    "Uvyazka rejected the request: "
                    + " ".join(c.text for c in result.content if hasattr(c, "text"))[:1500],
                )
            if getattr(result, "structured_content", None):
                return result.structured_content
            texts = [c.text for c in result.content if hasattr(c, "text")]
            if not texts:
                raise NormsError("UVYAZKA_EMPTY_RESPONSE", "No structured response from Uvyazka")
            return json.loads(texts[0])

    try:
        return asyncio.run(asyncio.wait_for(run(), timeout=90))
    except NormsError:
        raise
    except Exception as exc:
        raise NormsError(
            "UVYAZKA_CONNECTION_ERROR", f"Uvyazka bridge failed ({type(exc).__name__})", retryable=True
        ) from exc
