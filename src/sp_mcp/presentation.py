"""Structured browser reading without changing immutable text offsets or guessing table cells."""

import functools
import hashlib
import re
from collections import defaultdict
from pathlib import Path

from .errors import NormsError
from .identity import clean
from .parsers import soup_for

GRID = re.compile(r"[│┌┐└┘├┤┬┴┼]")


@functools.lru_cache(maxsize=8)
def source_grid_lines(path, digest):
    source = Path(path)
    if source.stat().st_size > 4 * 1024**2:
        return {}
    raw = source.read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise NormsError("INTEGRITY_ERROR", "Source layout checksum mismatch")
    soup = soup_for({"raw": raw})
    result = defaultdict(list)
    for node in soup.find_all(["div", "p", "pre"]):
        if node.find(["div", "p", "pre", "table"]) or node.find_parent(["p", "table"]):
            continue
        literal = node.get_text(" ", strip=True).replace("\xa0", " ")
        if GRID.search(literal):
            result[clean(literal)].append(literal)
    return dict(result)


def table_caption(document, table):
    preceding = document["blocks"][max(0, table["block_index"] - 3) : table["block_index"]]
    for block in reversed(preceding):
        if re.match(r"^Таблица\s+[А-ЯA-Z0-9]", block["text"], re.I):
            return block["text"][:250]
    return f"Таблица в тексте · {table['index'] + 1}"


def reading_blocks(document, root, start, end):
    blocks = [b for b in document["blocks"] if b["end"] > start and b["start"] < end]
    if len(blocks) > 400:
        return None
    layout = {}
    if document["text_status"] == "extracted_html" and any(GRID.search(b["text"]) for b in blocks):
        digest = document["provenance"]["sha256"]
        layout = source_grid_lines(str(root / "raw" / digest), digest)
    occurrences = defaultdict(int)
    display = {}
    for block in document["blocks"]:
        key = clean(block["text"])
        if key in layout:
            position = occurrences[key]
            if position < len(layout[key]):
                display[block["index"]] = layout[key][position]
            occurrences[key] += 1
    result = []
    for block in blocks:
        item = {
            "index": block["index"],
            "start": block["start"],
            "end": block["end"],
            "kind": block["kind"],
            "text": document["text"][max(start, block["start"]) : min(end, block["end"])],
        }
        if "page_number" in block:
            item["page_number"] = block["page_number"]
        if block["kind"] == "table" and "table_index" in block:
            table = document["tables"][block["table_index"]]
            preview = table["rows"][:20]
            if sum(len(cell["text"]) for row in preview for cell in row) > 20000:
                preview = []
            item.update(
                table_index=table["index"],
                caption=table_caption(document, table),
                rows=preview,
                total_rows=len(table["rows"]),
            )
        elif GRID.search(block["text"]):
            item.update(
                kind="preformatted",
                display_text=display.get(block["index"], block["text"]),
                layout_basis="source_whitespace" if block["index"] in display else "saved_text",
            )
        result.append(item)
    return result
