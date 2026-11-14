"""Conservative, literal automatic contents; duplicate TOC entries resolve to body headings."""

import re

from .identity import clean


def document_contents(document, expanded=False, query="", offset=0, limit=300):
    if document["text_status"] == "metadata_only":
        return {
            "items": [],
            "total": 0,
            "next_offset": None,
            "reason": "metadata_only",
            "provenance": document["provenance"],
        }
    candidates = []
    excluded = re.compile(
        r"^(?:ИСПОЛНИТЕЛИ|ВНЕСЕН|ПОДГОТОВЛЕН|УТВЕРЖДЕН|ЗАРЕГИСТРИРОВАН|ВВЕДЕН|ПЕРЕСМОТР|Утвердить|Признать|Контроль)",
        re.I,
    )
    for block in document["blocks"]:
        position = block["start"]
        for raw in block["text"].splitlines(keepends=True):
            line = clean(raw)
            start = position + len(raw) - len(raw.lstrip())
            position += len(raw)
            if not line or len(line) > 210:
                continue
            number, level, kind = None, 0, "heading"
            section = re.match(r"^((?:[А-ЯA-ZЁ]\.)?\d+(?:\.\d+){0,2})\.?\s+(.+)$", line)
            appendix = re.match(r"^Приложение\s+([А-ЯA-Z0-9]+)(?:[.\s]|$)", line, re.I)
            if appendix:
                number, kind = appendix[1], "appendix"
            elif section:
                number, title = section[1], section[2]
                numeric_part = re.sub(r"^[А-ЯA-ZЁ]\.", "", number)
                level = numeric_part.count(".") + (numeric_part != number)
                if (
                    numeric_part.count(".") > 1
                    or not re.match(r"[А-ЯA-ZЁ]", title)
                    or excluded.match(title)
                    or (title.endswith((".", ";", ":")) and not title.isupper())
                    or re.search(
                        r"\[ИЗОБРАЖЕНИЕ|\b(?:определяется|рассчитывается|составляет|должен|должна|должно|должны|следует)\b",
                        title,
                        re.I,
                    )
                    or re.match(
                        r"\d*\s*(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)",
                        title,
                        re.I,
                    )
                ):
                    continue
                kind = "section"
            elif line.casefold() not in {
                "предисловие",
                "введение",
                "библиография",
                "нормативные ссылки",
                "термины и определения",
            }:
                continue
            candidates.append(
                {
                    "title": line,
                    "number": number,
                    "level": level,
                    "kind": kind,
                    "start": start,
                    "end": start + len(raw.strip()),
                    "block_index": block["index"],
                }
            )
    # Printed contents may abbreviate or change the case of body headings.
    # Locate the start of the body before assigning appendix-local numbering.
    first_sections = [item for item in candidates if item["kind"] == "section" and item["number"] == "1"]
    scope_sections = [
        item for item in first_sections if re.search(r"область\s+применения", item["title"], re.I)
    ]
    body_start = 0
    if scope_sections:
        body_start = scope_sections[-1]["start"]
    elif first_sections:
        first_title = first_sections[0]["title"].casefold()
        repeated = [item for item in first_sections if item["title"].casefold() == first_title]
        body_start = repeated[-1]["start"]

    selected = {}
    scope = "main"
    for item in candidates:
        if item["start"] < body_start and item["kind"] != "heading":
            continue
        if item["kind"] == "appendix":
            scope = "appendix:" + item["number"].upper()
            key = ("appendix", scope)
        elif item["kind"] == "section":
            key = (scope, "section", item["number"])
        else:
            key = ("heading", item["title"].casefold())
        item["scope"] = scope
        selected[key] = item
    items = sorted(selected.values(), key=lambda x: x["start"])
    if expanded:
        items = expanded_items(document, items)
    if query:
        if re.fullmatch(r"(?:[А-ЯA-ZЁ]\.)?\d+(?:\.\d+)*", query, re.I):
            items = [
                item
                for item in items
                if (item.get("number") or "").casefold() == query.casefold()
                or (item.get("number") or "").casefold().startswith(query.casefold() + ".")
                or (item.get("parent_number") or "").casefold() == query.casefold()
                or (item.get("parent_number") or "").casefold().startswith(query.casefold() + ".")
            ]
        else:
            items = [
                item
                for item in items
                if query.casefold() in document["text"][item["start"] : item["end"]].casefold()
            ]
    return {
        "items": items[offset : offset + limit],
        "total": len(items),
        "next_offset": offset + limit if offset + limit < len(items) else None,
        "expanded": expanded,
        "method": "literal_numbered_clauses_and_subpoints"
        if expanded
        else "literal_heading_patterns_body_and_appendix_scopes",
        "provenance": document["provenance"],
        "limitations": [
            "Automatically detected headings in extracted text; not a certified official table of contents."
        ],
    }


def expanded_items(document, headings):
    main = [
        item
        for item in headings
        if item["kind"] == "section" and item["scope"] == "main" and item["number"] == "1"
    ]
    body_start = main[0]["start"] if main else 0
    special = {item["start"]: item for item in headings if item["kind"] in {"heading", "appendix"}}
    selected = {("heading", item["title"]): item for item in special.values() if item["start"] < body_start}
    scope, parent, parent_level = "main", None, 0
    for block in document["blocks"]:
        if block["kind"] == "table":
            continue
        position = block["start"]
        for raw in block["text"].splitlines(keepends=True):
            line = clean(raw)
            start = position + len(raw) - len(raw.lstrip())
            position += len(raw)
            if start < body_start:
                continue
            if start in special:
                item = dict(special[start])
                if item["kind"] == "appendix":
                    scope, parent = item["scope"], None
                selected[(item["kind"], scope, item["number"], item["title"])] = item
                continue
            match = re.match(r"^((?:[А-ЯA-ZЁ]\.)?\d+(?:\.\d+){0,7})([.)]?)\s+(.+)$", line)
            letter = re.match(r"^([а-яa-zё])\)\s+(.+)$", line, re.I)
            if match:
                number, punctuation, title = match.groups()
                if re.match(r"[-–—=]|\d+\s*[-–—]", title):
                    continue
                if punctuation == ")":
                    if parent is None:
                        continue
                    number += ")"
                    kind, level, parent_number = "subpoint", parent_level + 1, parent
                else:
                    numeric = re.sub(r"^[А-ЯA-ZЁ]\.", "", number)
                    level = numeric.count(".") + (numeric != number)
                    kind = "clause"
                    parent_number = number.rsplit(".", 1)[0] if "." in number else None
                    parent, parent_level = number, level
            elif letter and parent is not None:
                number, title = letter[1] + ")", letter[2]
                kind, level, parent_number = "subpoint", parent_level + 1, parent
            else:
                continue
            key = (scope, kind, parent_number, number)
            selected[key] = {
                "title": line[:170],
                "title_truncated": len(line) > 170,
                "number": number,
                "parent_number": parent_number,
                "level": level,
                "kind": kind,
                "scope": scope,
                "start": start,
                "end": start + len(raw.strip()),
                "block_index": block["index"],
            }
    return sorted(selected.values(), key=lambda item: item["start"])
