import difflib
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode, urljoin

from .config import Config
from .errors import NormsError
from .identity import clean, designation
from .linguistics import catalog_queries, relevance, stems, topic_relevance
from .network import Network, validate_url
from .parsers import parse_candidates, parse_document, soup_for
from .presentation import reading_blocks, table_caption
from .retrieval import Retrieval
from .storage import Store
from .validity import observed_validity, validity_index
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
        self.retrieval = Retrieval(self.store)

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
            "search_modes": [
                "official_catalog",
                "russian_morphology_bm25",
                "concept_dictionary",
                "optional_local_embeddings",
            ],
            "semantic_search": {
                "default": "concept_dictionary_without_model",
                "optional_embeddings": self.retrieval.status(),
            },
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

    def search(
        self, query, limit=20, page=1, online=True, refresh=False, mode="concept", include_inactive=False
    ):
        query = clean(query)
        if not query or len(query) > 300:
            raise NormsError("INVALID_ARGUMENT", "query must contain 1..300 characters")
        bound(limit, 1, 50, "limit")
        bound(page, 1, 1000, "page")
        if mode not in {"concept", "lexical", "hybrid", "semantic"}:
            raise NormsError("INVALID_ARGUMENT", "Unknown search mode")
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
                    result = self._official_search(code or query, category, 1, refresh)
                    remote.extend(result["items"])
                    provenance.append(result["provenance"])
                except NormsError as exc:
                    errors.append({"source": "gostinfo", **exc.payload()})
            # The official q parameter is literal substring matching, not morphology.
            if not code:
                terms = sorted(set(stems(query)), key=len, reverse=True)
                if terms and terms[0] != query.casefold():
                    for category in categories:
                        for p in range(1, 4):
                            try:
                                result = self._official_search(terms[0], category, p, refresh)
                                remote.extend(result["items"])
                                provenance.append(result["provenance"])
                                if not result["next_urls"]:
                                    break
                            except NormsError as exc:
                                errors.append({"source": "gostinfo", **exc.payload()})
                                break
                if mode != "lexical":
                    for expanded in catalog_queries(query):
                        try:
                            result = self._official_search(expanded, "sp", 1, refresh)
                            remote.extend(result["items"])
                            provenance.append(result["provenance"])
                        except NormsError as exc:
                            errors.append({"source": "gostinfo", **exc.payload()})
            if not code or code.startswith("СП "):
                try:
                    for p in range(2):
                        self.browse(page=p, refresh=refresh)
                except NormsError as exc:
                    errors.append({"source": "meganorm", **exc.payload()})
            # Fire-safety SPs are often filed under the approving MChS order, not the base SP rubric.
            if any(term.startswith(("пожар", "туш", "дым", "огнетуш")) for term in stems(query)):
                try:
                    self._catalog("https://meganorm.ru/mega_doc/fire/normy/normy_0.html", refresh)
                except NormsError as exc:
                    errors.append({"source": "meganorm", **exc.payload()})
        local = self.store.candidates(code or query, limit=150)
        combined = {item["url"]: item for item in remote + local}
        values = list(combined.values())
        if code:
            values = [item for item in values if item["code"] == code]
        topic_codes = {
            item["code"] for item in values if mode != "lexical" and topic_relevance(query, item["title"])
        }
        for matched_code in topic_codes:
            for candidate in self.store.candidates(matched_code, limit=30):
                if candidate["code"] == matched_code and candidate["url"] not in combined:
                    values.append(candidate)
                    combined[candidate["url"]] = candidate
        values.sort(
            key=lambda item: (
                item["code"] != code if code else -relevance(query, item["title"]),
                item.get("change_number") is not None,
            )
        )
        hydrated = []
        if online and not code and page == 1:
            topic = max(stems(query), key=len, default="")
            topical = [
                item
                for item in values
                if item["source"] == "meganorm"
                and item.get("change_number") is None
                and (topic in stems(item["title"]) or item["code"] in topic_codes)
            ]
            if "газов" in stems(query):
                topical = [
                    item
                    for item in topical
                    if not ({"водян", "пенн", "порошков", "аэрозольн"} & set(stems(item["title"])))
                ]

            def year(item):
                years = re.findall(r"(?:19|20)\d{2}", item["code"])
                return int(years[-1]) if years else 0

            topical.sort(
                key=lambda item: (
                    item["code"] not in topic_codes,
                    bool(re.search(r"методические рекомендации|письмо|проект свода", item["title"], re.I)),
                    "проектирован" not in stems(item["title"]),
                    -year(item),
                )
            )
            hydrated_codes = set()
            attempts = 0
            for item in topical:
                if item["code"] in hydrated_codes:
                    continue
                if attempts >= 3:
                    break
                attempts += 1
                try:
                    summary = self.open(item["url"], latest_revision=True)
                    hydrated.append(summary["snapshot_id"])
                    hydrated_codes.add(item["code"])
                    if len(hydrated_codes) == 2:
                        break
                except NormsError as exc:
                    errors.append({"source": "meganorm", **exc.payload()})
        retrieval = self.retrieval.search(query, mode=mode if not code else "lexical", limit=limit * 3)
        text_hits = []
        for item in retrieval["items"]:
            if code and item["code"] != code:
                continue
            if item["kind"] == "text":
                text_hits.append(item | {"excerpt": item["text"], "query": query})
            elif item["kind"] == "title" and not any(v["url"] == item["url"] for v in values):
                values.append(
                    {
                        "url": item["url"],
                        "code": item["code"],
                        "title": item["title"],
                        "source": item["source"],
                        "change_number": None,
                        "match_status": "candidate",
                    }
                )

        text_hits.sort(
            key=lambda item: (
                -item.get("word_coverage", 0),
                not bool(item.get("clause")),
                len(item["text"]) < 90,
                -item.get("score", 0),
            )
        )
        unique_hits = {}
        for item in text_hits:
            unique_hits.setdefault(item["code"], item)
        text_hits = list(unique_hits.values())

        status_index = validity_index(self.store, self.config.freshness_days)
        cancelled_codes = {code for code, status in status_index.items() if status["hidden_by_default"]}
        excluded_codes = {item["code"] for item in values + text_hits if item["code"] in cancelled_codes}
        for item in values + text_hits:
            item["validity"] = observed_validity(status_index, item["code"])
        if not include_inactive:
            values = [item for item in values if item["code"] not in cancelled_codes]
            text_hits = [item for item in text_hits if item["code"] not in cancelled_codes]
        values.sort(
            key=lambda item: (
                item["code"] in cancelled_codes,
                item["code"] not in topic_codes,
                not item["code"].startswith(("СП ", "ГОСТ")),
                item["code"] != code if code else -relevance(query, item["title"]),
                item.get("change_number") is not None,
            )
        )
        for item in values:
            if item["code"] in cancelled_codes:
                item["catalogue_warning"] = "cancelled_or_replaced"
        text_hits.sort(
            key=lambda item: (
                item["code"] in cancelled_codes,
                item["code"] not in topic_codes,
                not item["code"].startswith(("СП ", "ГОСТ")),
                -item.get("word_coverage", 0),
            )
        )
        related = []
        if not code:
            direct_text_codes = {item["code"] for item in text_hits if item.get("word_coverage", 0) >= 1}
            primary = []
            for item in values:
                (
                    primary
                    if relevance(query, item["title"]) >= 1
                    or item["code"] in direct_text_codes
                    or item["code"] in topic_codes
                    else related
                ).append(item)
            values = primary
        start = (page - 1) * limit
        return {
            "status": "partial" if errors else "ok",
            "query": query,
            "page": page,
            "items": values[start : start + limit],
            "text_hits": text_hits[:limit],
            "related_items": related[:limit],
            "source_errors": errors,
            "search_provenance": provenance,
            "more_results_possible": len(values) > start + limit,
            "retrieval": {k: v for k, v in retrieval.items() if k != "items"},
            "hydrated_snapshots": hydrated,
            "validity_filter": {
                "include_inactive": include_inactive,
                "excluded_count": 0 if include_inactive else len(excluded_codes),
                "unknown_status_policy": "visible_marked_unverified",
            },
            "coverage": "Discovered catalogue titles and saved source fragments; not the complete Russian normative corpus.",
            "match_status": "candidate" if values or text_hits else "not_found_in_searched_sources",
        }

    def search_status(self):
        return self.retrieval.status()

    def prepare_search(self):
        return self.retrieval.start()

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
            "blocks": reading_blocks(document, self.store.root, offset, end),
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
                    "blocks": reading_blocks(
                        document, self.store.root, selected[0]["start"], selected[-1]["end"]
                    ),
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
            "caption": table_caption(document, table),
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

    def library(self, query="", limit=30, offset=0, include_inactive=False):
        bound(limit, 1, 100, "limit")
        bound(offset, 0, 1000000, "offset")
        index = validity_index(self.store, self.config.freshness_days)
        excluded = {code for code, status in index.items() if status["hidden_by_default"]}
        result = self.store.library(
            query, limit, offset, excluded_codes=excluded if not include_inactive else None
        )
        for item in result["items"]:
            item["validity"] = observed_validity(index, item["code"])
        result["validity_filter"] = {
            "include_inactive": include_inactive,
            "unknown_status_policy": "visible_marked_unverified",
        }
        return result

    def document(self, snapshot_id):
        doc = self.store.get(snapshot_id)
        return self.summary(doc) | {
            "table_captions": [
                {"index": t["index"], "caption": table_caption(doc, t), "total_rows": len(t["rows"])}
                for t in doc["tables"]
            ],
            "saved_editions": self.store.library(limit=100, code=doc["code"])["items"],
            "last_verification": next(
                iter(self.store.verification_history(snapshot_id, limit=1)["items"]), None
            ),
        }

    def contents(self, snapshot_id, expanded=False, query="", offset=0, limit=300):
        from .contents import document_contents

        bound(offset, 0, 100000, "offset")
        bound(limit, 1, 500, "limit")
        if len(query) > 100:
            raise NormsError("INVALID_ARGUMENT", "Contents query must be at most 100 characters")
        return document_contents(self.store.get(snapshot_id), expanded, query, offset, limit)

    def verifications(self, snapshot_id=None, limit=30, offset=0):
        bound(limit, 1, 100, "limit")
        bound(offset, 0, 1000000, "offset")
        return self.store.verification_history(snapshot_id, limit, offset)
