"""Conservative, literal automatic contents; duplicate TOC entries resolve to body headings."""

import re

from .identity import clean


def document_contents(document):
    if document["text_status"] == "metadata_only":
        return {"items": [], "reason": "metadata_only", "provenance": document["provenance"]}
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
            section = re.match(r"^(\d+(?:\.\d+){0,2})\.?\s+(.+)$", line)
            appendix = re.match(r"^Приложение\s+([А-ЯA-Z0-9]+)(?:[.\s]|$)", line, re.I)
            if appendix:
                number, kind = appendix[1], "appendix"
            elif section:
                number, title = section[1], section[2]
                level = number.count(".")
                if (
                    level > 1
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
        selected[key] = item
    items = sorted(selected.values(), key=lambda x: x["start"])
    return {
        "items": items,
        "total": len(items),
        "method": "literal_heading_patterns_body_and_appendix_scopes",
        "provenance": document["provenance"],
        "limitations": [
            "Automatically detected headings in extracted text; not a certified official table of contents."
        ],
    }
