"""Public registry used by the Federal Centre for Standardization website."""

import json
import re
from datetime import date
from urllib.parse import parse_qs, urlencode, urlsplit

from .errors import NormsError
from .identity import CODE, clean, designation, parse_date

HOST = "api.faufcc.ru"
ORIGIN = f"https://{HOST}"
REGISTRY = "https://faufcc.ru/deiatelnost/normirovanie-i-standartizatsiia/reestr-svodov-pravil"
REGISTRY_ID = "c5020d9a-a239-4806-a793-ac66cb36c71a"
ENTRIES = "/api/registries/entries"
UUID = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
ASSET = re.compile(rf"/api/assets/{UUID}(?:/download)?")
ENTRY = re.compile(rf"{ENTRIES}/{UUID}")
ORDER_NUMBER = re.compile(r"(?<!\d)(\d+)\s*/\s*пр\b", re.I)


def search_url(query, page):
    filters = {"registry": REGISTRY_ID}
    if query:
        filters["search"] = query.removeprefix("СП ")
    return f"{ORIGIN}{ENTRIES}?" + urlencode(
        {"page": page, "filters": json.dumps(filters, ensure_ascii=False), "include": "asset"}
    )


def entry_url(entry_id):
    return f"{ORIGIN}{ENTRIES}/{entry_id}?include=operations.groups.fields.value"


def payload(fetched):
    try:
        result = json.loads(fetched["raw"])
        if isinstance(result, dict) and "data" in result:
            return result
    except (ValueError, UnicodeError):
        pass
    raise NormsError("SOURCE_LAYOUT_CHANGED", "FCS registry did not return its expected JSON")


def relation(item, key, expected):
    wrapper = item.get(key)
    data = wrapper.get("data") if isinstance(wrapper, dict) else None
    return data if isinstance(data, expected) else expected()


def pdf_url(asset):
    links = asset.get("links")
    url = links.get("open") if isinstance(links, dict) else None
    if asset.get("mime") != "application/pdf" or not isinstance(url, str):
        return None
    parsed = urlsplit(url)
    if parsed.scheme == "https" and parsed.netloc == HOST and ASSET.fullmatch(parsed.path):
        if not parsed.query and not parsed.fragment:
            return url
    return None


def catalogue(fetched):
    data = payload(fetched)
    items, next_urls = [], []
    try:
        rows = data["data"]
        pagination = data["meta"]["pagination"]
        params = parse_qs(urlsplit(fetched["url"]).query)
        page = int(params["page"][0])
        query = json.loads(params["filters"][0]).get("search", "")
        if not isinstance(rows, list) or pagination["currentPage"] != max(1, page):
            raise ValueError
        if pagination["totalPages"] > max(1, page):
            next_urls.append(search_url(query, max(1, page) + 1))
    except (KeyError, TypeError, ValueError) as exc:
        raise NormsError("SOURCE_LAYOUT_CHANGED", "FCS listing or pagination could not be read") from exc
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            raise NormsError("SOURCE_LAYOUT_CHANGED", "FCS listing contains an unreadable record")
        number, entry_id = row.get("number") or "", row["id"]
        if not isinstance(number, str):
            continue
        code = designation(number)
        if not code or not code.startswith("СП ") or not CODE.fullmatch(clean(number)):
            continue
        if not re.fullmatch(UUID, entry_id):
            continue
        original = pdf_url(relation(row, "asset", dict))
        items.append(
            {
                "url": original or entry_url(entry_id),
                "original_pdf": original,
                "registry_entry_url": entry_url(entry_id),
                "title": clean(f"{code} {row.get('name') or ''}"),
                "label": code,
                "code": code,
                "change_number": None,
                "source": "faufcc",
                "authority": "official_publisher",
                "text_status": "metadata_only" if not original else "original_pdf_available",
                "match_status": "candidate",
                "discovered_from": fetched["url"],
                "discovered_at": fetched["checked_at"],
            }
        )
    return items, next_urls


def iso_date(value):
    try:
        return date.fromisoformat(value.split("T")[0]).isoformat()
    except (AttributeError, ValueError):
        return None


def amendment_files(fetched, code):
    entry = payload(fetched)["data"]
    if (
        not isinstance(entry, dict)
        or entry.get("number") != code
        or entry.get("id") != urlsplit(fetched["url"]).path.rsplit("/", 1)[-1]
    ):
        raise NormsError("DISCOVERY_IDENTITY_MISMATCH", "FCS entry does not match the requested SP")
    operations = entry.get("operations")
    if not isinstance(operations, dict) or not isinstance(operations.get("data"), list):
        raise NormsError("SOURCE_LAYOUT_CHANGED", "FCS entry has no readable amendment section")
    files = []
    for operation in relation(entry, "operations", list):
        fields = {
            field["name"]: (relation(field, "value", list) or [None])[0]
            for group in relation(operation, "groups", list)
            for field in relation(group, "fields", list)
            if isinstance(field, dict) and "name" in field and not field.get("hidden")
        }
        kind = fields.get("operation-type")
        if not isinstance(kind, dict) or kind.get("name") != "modification":
            continue
        asset = fields.get("order-document") or {}
        if not isinstance(asset, dict):
            asset = {}
        files.append(
            {
                "url": pdf_url(asset),
                "title": asset.get("name"),
                "order": fields.get("order-name"),
                "order_date": iso_date(fields.get("order-date")),
                "reported_effective_from": iso_date(fields.get("active-since")),
            }
        )
    return files


def order_number(value):
    match = ORDER_NUMBER.search(value or "")
    return match[1] if match else None


def reconcile(files, cards):
    """Match FCS files to exact Rosstandard amendment cards by approval order."""
    amendments = {}
    for card in cards:
        if card["source"] != "rosstandard" or (number := card["change_number"]) is None:
            continue
        fields = card["fields"]
        order = order_number(fields.get("Сведения о регистрации изменения"))
        amendments[number] = {
            "change_number": number,
            "rosstandard_url": card["provenance"]["source_url"],
            "effective_from": card["effective_from"],
            "order_number": order,
            "approval_date": fields.get("Дата утверждения изменения"),
            "files": [],
        }
    unmatched = []
    for file in files:
        order = order_number(file["order"])
        matches = [item for item in amendments.values() if order and item["order_number"] == order]
        if len(matches) != 1:
            unmatched.append(file)
            continue
        match = matches[0]
        effective = match["effective_from"]
        reported = file["reported_effective_from"]
        approved = match["approval_date"]
        match["files"].append(
            file
            | {
                "effective_date_disagreement": bool(effective and reported and effective != reported),
                "approval_date_disagreement": bool(
                    (approval_date := parse_date(approved))
                    and file["order_date"]
                    and approval_date != file["order_date"]
                ),
            }
        )
    return {
        "amendments": [amendments[number] for number in sorted(amendments)],
        "unmatched_files": unmatched,
    }
