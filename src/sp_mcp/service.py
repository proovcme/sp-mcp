import difflib
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode, urljoin

from .config import Config
from .errors import NormsError
from .identity import clean, designation
from .network import Network, validate_url
from .parsers import parse_candidates, parse_document, soup_for
from .storage import Store
from .verification import verify

CATALOGS = {
    "sp": "https://meganorm.ru/mega_doc/norm/sp_svod-pravil/sp_svod-pravil_0.html",
    "gost": "https://meganorm.ru/mega_doc/norm/gost_gosudarstvennyj-standart/gost_gosudarstvennyj-standart_0.html",
    "gost_r": "https://meganorm.ru/mega_doc/norm/gost-r_gosudarstvennyj-standart/gost-r_gosudarstvennyj-standart_0.html",
    "snip": "https://meganorm.ru/mega_doc/norm/snip_stroitelnye-normy-i-pravila/snip_stroitelnye-normy-i-pravila_0.html",
}


def bound(value, lower, upper, name):
    if not lower <= value <= upper:
        raise NormsError("INVALID_ARGUMENT", f"{name} must be {lower}..{upper}")


class Service:
    def __init__(self, config: Config, network=None):
        self.config = config
        self.store = Store(config.root)
        self.network = network or Network(config)

    def sources(self):
        return {
            "items": [
                {
                    "id": "meganorm",
                    "role": "secondary_text_and_revision_links",
                    "catalogs": CATALOGS,
                    "limitations": [
                        "Separate dated databases; discovery is bounded, not whole-site indexing.",
                        "Internal search.htm is excluded by robots.txt and is never requested.",
                        "Public accessibility is not a redistribution licence.",
                    ],
                },
                {
                    "id": "gostinfo",
                    "role": "official_catalog_status_and_amendments",
                    "url": "https://new.gostinfo.ru/catalog/",
                    "limitations": [
                        "Current catalogue cards do not prove historical project applicability.",
                        "No universal coverage of orders, SanPiN or other legal acts.",
                    ],
                },
            ],
            "local": self.store.stats(),
            "search_modes": ["official_catalog", "local_prefix_full_text"],
            "semantic_search": False,
            "planned_adapters": ["Minstroy", "MChS", "Rospotrebnadzor", "official legal publication"],
        }

    def _catalog(self, url, refresh=False):
        url = validate_url(url)
        cached = self.store.catalog(url)
        if cached and not refresh:
            checked = datetime.fromisoformat(cached["provenance"]["checked_at"])
            if datetime.now(timezone.utc) - checked < timedelta(hours=24):
                return cached
        fetched = self.network.fetch(url)
        items = parse_candidates(fetched)
        soup = soup_for(fetched)
        next_urls = []
        for a in soup.find_all("a", href=True):
            label = clean(a.get_text(" ", strip=True))
            href = urljoin(fetched["url"], a["href"])
            if "Следующая" in label or re.search(r"_\d+\.html$", href) and label.isdigit():
                if href != url:
                    next_urls.append(href)
        for item in items:
            item["discovery_sha256"] = __import__("hashlib").sha256(fetched["raw"]).hexdigest()
        self.store.add_candidates(items)
        return self.store.save_catalog(fetched, {"items": items, "next_urls": sorted(set(next_urls))})

    def browse(self, source="meganorm", catalog="sp", page=0, refresh=False):
        bound(page, 0, 1000, "page")
        if source != "meganorm" or catalog not in CATALOGS:
            raise NormsError("INVALID_ARGUMENT", "Use source=meganorm and catalog=sp|gost|gost_r|snip")
        url = CATALOGS[catalog].replace("_0.html", f"_{page}.html")
        return self._catalog(url, refresh)

    def _official_search(self, query, category, page=1, refresh=False):
        url = "https://new.gostinfo.ru/catalog?" + urlencode({"catalog": category, "q": query, "page": page})
        return self._catalog(url, refresh)

    def search(self, query, limit=20, page=1, online=True, refresh=False):
        query = clean(query)
        if not query or len(query) > 300:
            raise NormsError("INVALID_ARGUMENT", "query must contain 1..300 characters")
        bound(limit, 1, 50, "limit")
        bound(page, 1, 1000, "page")
        errors, remote, provenance = [], [], []
        code = designation(query)
        if online:
            categories = (
                ["sp"]
                if code and code.startswith("СП ")
                else (["gost"] if code and code.startswith("ГОСТ") else ["sp", "gost"])
            )
            for category in categories:
                try:
                    result = self._official_search(code or query, category, page, refresh)
                    remote.extend(result["items"])
                    provenance.append(result["provenance"])
                except NormsError as exc:
                    errors.append({"source": "gostinfo", **exc.payload()})
            # СП catalogue contains all base СП titles in two pages; revisions are followed explicitly.
            if page == 1 and (not code or code.startswith("СП ")):
                try:
                    for p in range(2):
                        self.browse(page=p, refresh=refresh)
                except NormsError as exc:
                    errors.append({"source": "meganorm", **exc.payload()})
        local = self.store.candidates(code or query, limit=limit)
        combined = {item["url"]: item for item in remote + local}
        values = list(combined.values())
        if code:
            values.sort(key=lambda item: (item["code"] != code, item["change_number"] is not None))
        text_hits = self.store.search_text(query, limit, (page - 1) * limit)
        return {
            "status": "partial" if errors else "ok",
            "query": query,
            "page": page,
            "items": values[:limit],
            "text_hits": text_hits,
            "source_errors": errors,
            "search_provenance": provenance,
            "more_results_possible": len(values) > limit,
            "coverage": "Bounded live official catalogues and indexed local sources; not all Russian norms.",
            "match_status": "candidate" if values or text_hits else "not_found_in_searched_sources",
        }

    @staticmethod
    def summary(document):
        return {k: v for k, v in document.items() if k not in {"text", "blocks", "tables", "images"}} | {
            "text_length": len(document["text"]),
            "block_count": len(document["blocks"]),
            "table_count": len(document["tables"]),
            "image_count": len(document["images"]),
        }

    def open(self, url, refresh=False, latest_revision=False):
        url = validate_url(url)
        cached = self.store.latest(url)
        use_cached = (
            cached
            and not refresh
            and (
                datetime.now(timezone.utc) - datetime.fromisoformat(cached["provenance"]["checked_at"])
                < timedelta(hours=1)
            )
        )
        if use_cached:
            document = cached
        else:
            fetched = self.network.fetch(url)
            document = self.store.save(fetched, parse_document(fetched))
            if not fetched["raw"].startswith(b"%PDF-"):
                self.store.add_candidates(parse_candidates(fetched))
        if latest_revision and document["revisions"]:
            newest = max(document["revisions"], key=lambda r: r["date"])
            if newest["date"] > (document.get("edition_date") or ""):
                opened = self.open(newest["url"], refresh=refresh)
                replacement = self.store.get(opened["snapshot_id"])
                if replacement["code"] != document["code"]:
                    raise NormsError("REVISION_IDENTITY_MISMATCH", "Revision link points to another document")
                return opened | {
                    "selected_via": url,
                    "selection_note": "Newest linked edition; effective dates still need verification.",
                }
        return self.summary(document)

    def read(self, snapshot_id, offset=0, limit=12000, expected_sha256=None):
        bound(offset, 0, 100000000, "offset")
        bound(limit, 1, 30000, "limit")
        document = self.store.get(snapshot_id, expected_sha256)
        end = min(offset + limit, len(document["text"]))
        return {
            "snapshot_id": snapshot_id,
            "code": document["code"],
            "text": document["text"][offset:end],
            "offset": offset,
            "next_offset": end if end < len(document["text"]) else None,
            "total_characters": len(document["text"]),
            "text_status": document["text_status"],
            "provenance": document["provenance"],
            "limitations": document["limitations"],
        }

    def outline(self, snapshot_id, offset=0, limit=40):
        bound(offset, 0, 1000000, "offset")
        bound(limit, 1, 100, "limit")
        document = self.store.get(snapshot_id)
        items = [
            {k: b[k] for k in b if k != "text"} | {"preview": b["text"][:250]}
            for b in document["blocks"][offset : offset + limit]
        ]
        return {
            "items": items,
            "next_offset": offset + limit if offset + limit < len(document["blocks"]) else None,
            "provenance": document["provenance"],
        }

    def read_clause(self, snapshot_id, number, expected_sha256=None):
        if not re.fullmatch(r"(?:[А-ЯA-Z]\.)?\d+(?:\.\d+)*", number):
            raise NormsError("INVALID_ARGUMENT", "number must be a literal clause number, e.g. 7.11.1")
        document = self.store.get(snapshot_id, expected_sha256)
        pattern = re.compile(r"^" + re.escape(number) + r"(?:\s|[.)]\s)")
        matches = [i for i, b in enumerate(document["blocks"]) if pattern.match(b["text"])]
        if not matches:
            return {
                "status": "clause_not_found_in_extracted_blocks",
                "number": number,
                "text_status": document["text_status"],
                "provenance": document["provenance"],
            }
        occurrences = []
        for start in matches[:10]:
            selected = []
            for i, block in enumerate(document["blocks"][start:], start=start):
                marker = re.match(r"^((?:[А-ЯA-Z]\.)?\d+(?:\.\d+)*)(?:\s|[.)]\s)", block["text"])
                if i > start and marker and not marker[1].startswith(number + "."):
                    break
                selected.append(block)
                if sum(len(b["text"]) for b in selected) >= 30000:
                    break
            occurrences.append(
                {
                    "start": selected[0]["start"],
                    "end": selected[-1]["end"],
                    "text": document["text"][selected[0]["start"] : selected[-1]["end"]],
                    "block_indices": [b["index"] for b in selected],
                }
            )
        return {
            "status": "found" if len(matches) == 1 else "ambiguous_occurrences",
            "number": number,
            "occurrences": occurrences,
            "provenance": document["provenance"],
            "limitations": document["limitations"],
        }

    def search_document(self, snapshot_id, query, offset=0, limit=10):
        bound(offset, 0, 100000000, "offset")
        bound(limit, 1, 50, "limit")
        if not clean(query):
            raise NormsError("INVALID_ARGUMENT", "query is empty")
        document = self.store.get(snapshot_id)
        pattern = re.compile(r"\s+".join(re.escape(t) for t in query.split()), re.I)
        matches = list(pattern.finditer(document["text"], offset))[: limit + 1]
        return {
            "items": [
                {
                    "start": m.start(),
                    "end": m.end(),
                    "excerpt": document["text"][max(0, m.start() - 250) : m.end() + 400],
                }
                for m in matches[:limit]
            ],
            "next_offset": matches[limit - 1].end() if len(matches) > limit else None,
            "text_status": document["text_status"],
            "provenance": document["provenance"],
        }

    def table(self, snapshot_id, table_index, row_offset=0, limit=20):
        bound(table_index, 0, 100000, "table_index")
        bound(row_offset, 0, 100000, "row_offset")
        bound(limit, 1, 100, "limit")
        document = self.store.get(snapshot_id)
        if table_index >= len(document["tables"]):
            raise NormsError("TABLE_NOT_FOUND", "Use norms_document_outline to locate source table indices")
        table = document["tables"][table_index]
        return {
            "table_index": table_index,
            "row_offset": row_offset,
            "rows": table["rows"][row_offset : row_offset + limit],
            "total_rows": len(table["rows"]),
            "next_row_offset": row_offset + limit if row_offset + limit < len(table["rows"]) else None,
            "provenance": document["provenance"],
            "merged_cells": "original source spans retained",
        }

    def check(self, code, snapshot_id=None, as_of=None, refresh=True):
        normalized = designation(code)
        if not normalized or clean(code).upper().replace("—", "-") != normalized:
            raise NormsError("INVALID_ARGUMENT", "Pass a complete document designation without extra text")
        if as_of:
            date.fromisoformat(as_of)
        if not normalized.startswith(("СП ", "ГОСТ")):
            return self.store.save_verification(
                {
                    "code": normalized,
                    "verification": "unconfirmed",
                    "document_status": "unconfirmed",
                    "as_of": as_of or date.today().isoformat(),
                    "snapshot_id": snapshot_id,
                    "reason": "No official status adapter implemented for this document family",
                    "applicability": "not_assessed",
                }
            )
        category = "sp" if normalized.startswith("СП ") else "gost"
        cards, errors, search_complete = [], [], False
        search_evidence = []
        for page in range(1, 6):
            try:
                result = self._official_search(normalized, category, page, refresh)
                search_evidence.append(result["provenance"])
                for item in result["items"]:
                    if item["code"] != normalized:
                        continue
                    try:
                        summary = self.open(item["url"], refresh=refresh)
                        doc = self.store.get(summary["snapshot_id"])
                        if doc["authority"] == "official_catalog" and doc["code"] == normalized:
                            cards.append(doc)
                    except NormsError as exc:
                        errors.append({"url": item["url"], **exc.payload()})
                if not result["next_urls"]:
                    search_complete = True
                    break
            except NormsError as exc:
                errors.append({"source": "gostinfo", **exc.payload()})
                break
        loaded = self.store.get(snapshot_id) if snapshot_id else None
        result = verify(
            normalized,
            cards,
            loaded,
            as_of=as_of,
            freshness_days=self.config.freshness_days,
            search_complete=search_complete,
            errors=errors,
        )
        result["search_evidence"] = search_evidence
        result["snapshot_id"] = snapshot_id
        return self.store.save_verification(result)

    def batch_check(self, items, as_of=None, refresh=True):
        bound(len(items), 1, 10, "items count")
        results = []
        for item in items:
            input_id = item.get("input_id")
            try:
                if input_id is None or not item.get("code"):
                    raise NormsError("INVALID_ARGUMENT", "Each item requires input_id and code")
                result = self.check(item["code"], item.get("snapshot_id"), as_of, refresh)
            except (NormsError, ValueError) as exc:
                result = (
                    exc.payload()
                    if isinstance(exc, NormsError)
                    else {"status": "error", "error_code": "INVALID_ARGUMENT", "message": str(exc)}
                )
            results.append({"input_id": input_id, "result": result})
        return {"items": results}

    def compare(self, left_snapshot_id, right_snapshot_id, offset=0, limit=100):
        bound(offset, 0, 1000000, "offset")
        bound(limit, 1, 300, "limit")
        left, right = self.store.get(left_snapshot_id), self.store.get(right_snapshot_id)
        if left["code"] != right["code"]:
            raise NormsError("IDENTITY_MISMATCH", "Text comparison requires the same document designation")
        delta = list(
            difflib.unified_diff(
                left["text"].splitlines(),
                right["text"].splitlines(),
                fromfile=left_snapshot_id,
                tofile=right_snapshot_id,
                lineterm="",
            )
        )
        return {
            "code": left["code"],
            "lines": delta[offset : offset + limit],
            "total_lines": len(delta),
            "next_offset": offset + limit if offset + limit < len(delta) else None,
            "left_provenance": left["provenance"],
            "right_provenance": right["provenance"],
            "limitations": ["Literal extraction diff; not an official amendment or legal interpretation."],
        }
