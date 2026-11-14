"""Dated search filtering from explicit source observations, never from a document's age."""

import json
import re
from datetime import date, datetime, timedelta, timezone

from .identity import candidate_code, parse_date, status_value

INACTIVE = {"cancelled", "replaced", "not_yet_effective"}


def validity_index(store, freshness_days=7):
    today = date.today().isoformat()
    cutoff = datetime.now(timezone.utc) - timedelta(days=freshness_days)
    observations = {}

    def add(code, state, priority, checked, url, basis, excerpt=""):
        if state not in INACTIVE | {"active", "unknown"} or not checked:
            return
        if datetime.fromisoformat(checked) < cutoff:
            return
        observations.setdefault(code, []).append(
            {
                "state": state,
                "priority": priority,
                "checked_at": checked,
                "source_url": url,
                "basis": basis,
                "excerpt": excerpt,
            }
        )

    with store.db() as db:
        candidates = db.execute("SELECT payload FROM candidates").fetchall()
        snapshots = db.execute("""SELECT json_extract(payload,'$.code') code,
            json_extract(payload,'$.change_number') change_number,
            json_extract(payload,'$.authority') authority,
            json_extract(payload,'$.declared_status') declared_status,
            json_extract(payload,'$.raw_status') raw_status,
            json_extract(payload,'$.effective_from') effective_from,
            json_extract(payload,'$.effective_to') effective_to,
            json_extract(payload,'$.provenance.source_url') url,
            MAX(o.checked_at) checked_at, substr(json_extract(payload,'$.text'),1,4000) head
            FROM snapshots s JOIN observations o ON o.snapshot_id=s.id GROUP BY s.id ORDER BY checked_at DESC""").fetchall()
        reports = db.execute("SELECT payload FROM verifications ORDER BY created_at DESC").fetchall()
    for row in candidates:
        item = json.loads(row[0])
        item["code"] = candidate_code(item)
        if item.get("authority") != "official_catalog" or item.get("change_number") is not None:
            continue
        match = re.match(
            re.escape(item["code"])
            + r"\s+(Действует|Отмен[её]н|Замен[её]н|Утратил силу|Не действует|Не введ[её]н)\b",
            item["title"],
            re.I,
        )
        if match:
            add(
                item["code"],
                status_value(match[1]),
                2,
                item.get("discovered_at"),
                item["url"],
                "official_catalog_label",
                match[0],
            )
    seen_sources = set()
    for item in snapshots:
        if item["change_number"] is not None:
            continue
        identity = (item["code"], item["url"])
        if identity in seen_sources:
            continue
        seen_sources.add(identity)
        state = item["declared_status"]
        if item["effective_from"] and item["effective_from"] > today:
            state = "not_yet_effective"
        elif item["effective_to"] and item["effective_to"] <= today:
            state = state if state in {"cancelled", "replaced"} else "cancelled"
        elif item["effective_to"] and item["effective_to"] > today and state in INACTIVE:
            state = "unknown"
        if item["authority"] == "official_catalog":
            add(item["code"], state, 3, item["checked_at"], item["url"], "official_card", item["raw_status"])
        else:
            if state != "unknown":
                add(
                    item["code"],
                    state,
                    1,
                    item["checked_at"],
                    item["url"],
                    "secondary_card",
                    item["raw_status"],
                )
            match = re.search(
                r"(?m)^Документ (?:утратил силу|отмен[её]н|не действует|замен[её]н)[^\n]*",
                item["head"] or "",
                re.I,
            )
            if match:
                excerpt = match[0]
                effective = parse_date(excerpt)
                if not re.search(r"в части|частично|за исключением", excerpt, re.I) and (
                    not effective or effective <= today
                ):
                    add(
                        item["code"],
                        status_value(excerpt),
                        1,
                        item["checked_at"],
                        item["url"],
                        "secondary_document_note",
                        excerpt,
                    )
    seen_reports = set()
    for row in reports:
        report = json.loads(row[0])
        code = report["code"]
        if code in seen_reports or report.get("as_of") != today:
            continue
        seen_reports.add(code)
        if report.get("verification") == "confirmed":
            evidence = report.get("evidence", [])
            add(
                code,
                report["document_status"],
                4,
                report.get("checked_at"),
                evidence[0]["provenance"]["source_url"] if evidence else "",
                "saved_verification",
            )
    result = {}
    for code, items in observations.items():
        priority = max(item["priority"] for item in items)
        current = [item for item in items if item["priority"] == priority]
        states = {item["state"] for item in current}
        if len(states) > 1:
            result[code] = {"state": "conflict", "hidden_by_default": False, "evidence": current}
        else:
            observation = max(current, key=lambda item: item["checked_at"])
            result[code] = {**observation, "hidden_by_default": observation["state"] in INACTIVE}
    return result


def observed_validity(index, code):
    return index.get(code, {"state": "unknown", "hidden_by_default": False, "basis": "not_checked"})
