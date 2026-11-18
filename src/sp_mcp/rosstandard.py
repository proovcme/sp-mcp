"""Public HTML catalogue of the Federal Information Fund of Standards."""

import re
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from .errors import NormsError
from .identity import CODE, change_number, clean, designation, parse_date, status_value

ORIGIN = "https://protect.gost.ru"
CARD = re.compile(r"/(sp|gost)/(details|changesdetails)/[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}/?", re.I)


def search_url(query, category, page):
    return f"{ORIGIN}/{category}?" + urlencode({"search": query, "page": page})


def text(node):
    return clean(node.get_text(" ", strip=True)) if node else ""


def card_kind(url):
    parsed = urlsplit(url)
    match = CARD.fullmatch(parsed.path)
    if parsed.netloc == "protect.gost.ru" and parsed.scheme == "https" and match and not parsed.query:
        return match[2].lower()
    return None


def raw_status(label):
    return re.sub(r"^[^А-Яа-яЁё]+", "", label)


def observed_status(label):
    label = raw_status(label).casefold().replace("ё", "е")
    if label == "действует только в рф":
        return "active"
    if label == "срок действия истек":
        return "cancelled"
    return status_value(label)


def candidate(fetched, anchor, title, status=""):
    label = text(anchor)
    url = urljoin(fetched["url"], anchor["href"])
    kind = card_kind(url)
    number, code = change_number(label), designation(label)
    if kind is None or not code or (kind == "changesdetails" and number is None):
        return None
    return {
        "url": url,
        "label": label,
        "title": clean(" ".join((label, raw_status(status), title))),
        "code": code,
        "change_number": number,
        "raw_status": raw_status(status),
        "declared_status": observed_status(status),
        "source": "rosstandard",
        "authority": "official_catalog",
        "match_status": "candidate",
        "discovered_from": fetched["url"],
        "discovered_at": fetched["checked_at"],
    }


def catalogue(fetched, soup):
    parsed = urlsplit(fetched["url"])
    path = parsed.path.rstrip("/")
    form = soup.find("form", action=path, method=re.compile(r"^get$", re.I))
    if path not in {"/sp", "/gost"} or not form or not form.find("input", attrs={"name": "search"}):
        raise NormsError("SOURCE_LAYOUT_CHANGED", "Rosstandard listing could not be identified")
    items = {}
    for row in soup.select("table tbody tr"):
        cells = row.find_all("td", recursive=False)
        if len(cells) < 4 or not (link := cells[0].find("a", href=True)):
            continue
        item = candidate(fetched, link, text(cells[1]), text(cells[-1]))
        if item and item["change_number"] is None:
            items[item["url"]] = item
    if not items and not any(text(h) == "Документы не найдены" for h in soup.find_all("h3")):
        raise NormsError("SOURCE_LAYOUT_CHANGED", "No readable Rosstandard results or explicit empty result")
    params = parse_qs(parsed.query, keep_blank_values=True)
    page = int(params.get("page", ["1"])[0])
    following = set()
    for link in soup.find_all("a", href=True):
        url = urljoin(fetched["url"], link["href"])
        dest = urlsplit(url)
        values = parse_qs(dest.query, keep_blank_values=True)
        if (
            dest.netloc == parsed.netloc
            and dest.scheme == parsed.scheme
            and dest.path.rstrip("/") == path
            and values.get("search", [""]) == params.get("search", [""])
            and values.get("page") == [str(page + 1)]
        ):
            following.add(url)
    return list(items.values()), sorted(following)


def amendment_candidates(fetched, soup):
    panel = soup.select_one('[data-tab-panel="changes"]')
    items, other = {}, {}
    if not panel:
        return [], [], False
    for anchor in panel.find_all("a", href=True):
        url = urljoin(fetched["url"], anchor["href"])
        if card_kind(url) != "changesdetails":
            continue
        item = candidate(fetched, anchor, "")
        if item:
            items[url] = item
        else:
            other[url] = {"url": url, "label": text(anchor)}
    empty = any(text(h) == "Документы не найдены" for h in panel.find_all("h3"))
    return list(items.values()), list(other.values()), bool(items or other or empty)


def designations(value):
    return list(dict.fromkeys(designation(m[0]) for m in CODE.finditer(value)))


def document(fetched, soup):
    kind = card_kind(fetched["url"])
    heading = soup.find("h1")
    panel = soup.select_one('[data-tab-panel="bibliography"]')
    if not kind or not heading or not panel:
        raise NormsError("NOT_A_DOCUMENT", "This Rosstandard page is not a supported bibliographic card")
    fields = {}
    for label in panel.find_all("span"):
        value = label.find_next_sibling("p")
        if value and (key := text(label)):
            fields[key] = text(value)
    code = designation(fields.get("Обозначение", ""))
    title_label = text(heading)
    number = change_number(title_label)
    if not code or code != designation(title_label) or (kind == "changesdetails") != (number is not None):
        raise NormsError(
            "OFFICIAL_CARD_IDENTITY_MISMATCH",
            "Rosstandard title and designation do not identify the same card",
        )
    declared_number = fields.get("Номер изменения", "")
    if number is not None and declared_number and declared_number != str(number):
        raise NormsError(
            "OFFICIAL_CARD_IDENTITY_MISMATCH", "Rosstandard amendment number differs from its card title"
        )
    status = ""
    if kind == "details":
        header = heading.parent.parent.parent
        status = text(header.select_one(".flex-shrink-0"))
    fields["Статус"] = status
    amendments, other, changes_observed = amendment_candidates(fetched, soup)
    title = clean(
        title_label + " · " + fields.get("Заглавие на русском языке", fields.get("Заглавие на русском", ""))
    ).rstrip(" ·")
    blocks, position = [], 0
    for key, value in fields.items():
        line = f"{key}: {value}".rstrip()
        blocks.append(
            {
                "index": len(blocks),
                "start": position,
                "end": position + len(line),
                "kind": "paragraph",
                "text": line,
            }
        )
        position += len(line) + 1
    return {
        "source": "rosstandard",
        "authority": "official_catalog",
        "code": code,
        "title": title,
        "change_number": number,
        "fields": fields,
        "declared_status": observed_status(status),
        "raw_status": status,
        "effective_from": parse_date(
            fields.get("Дата введения изменения в действие") or fields.get("Дата введения в действие")
        ),
        "effective_to": parse_date(
            fields.get("Дата отмены") or fields.get("Дата ограничения срока действия")
        ),
        "supersedes": designations(fields.get("Обозначение заменяемого(ых)", "")),
        "superseded_by": designations(fields.get("Обозначение заменяющего", "")),
        "amendment_links": amendments,
        "other_change_links": other,
        "change_list_observed": changes_observed,
        "edition_date": None,
        "included_changes": [],
        "revisions": [],
        "downloads": [],
        "text": "\n".join(b["text"] for b in blocks),
        "blocks": blocks,
        "tables": [],
        "images": [],
        "text_status": "metadata_only",
        "limitations": ["Public bibliographic card. Open the original page to use the official viewer."],
    }
