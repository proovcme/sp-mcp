import io
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from pypdf import PdfReader

from . import faufcc, rosstandard
from .errors import NormsError
from .identity import CODE, change_number, clean, designation, parse_date, status_value
from .network import decode


def soup_for(fetched):
    return BeautifulSoup(decode(fetched), "html.parser")


def source_for(url):
    host = urlsplit(url).hostname
    if host == "meganorm.ru":
        return "meganorm", "secondary"
    if host == "protect.gost.ru":
        return "rosstandard", "official_catalog"
    if host == faufcc.HOST:
        return "faufcc", "official_publisher"
    return "gostinfo", "official_catalog"


def parse_candidates(fetched):
    soup = soup_for(fetched)
    source, authority = source_for(fetched["url"])
    if source == "rosstandard":
        if rosstandard.card_kind(fetched["url"]):
            return rosstandard.amendment_candidates(fetched, soup)[0]
        return rosstandard.catalogue(fetched, soup)[0]
    items, seen = [], set()
    for a in soup.find_all("a", href=True):
        url = urljoin(fetched["url"], a["href"])
        title = clean(a.get_text(" ", strip=True))
        if source == "gostinfo":
            if not re.search(r"/catalog/(?:gost|sp|rst)/\d+/?$", url):
                continue
            strong = a.find("strong")
            label = clean(strong.get_text(" ", strip=True)) if strong else title
        else:
            if not re.search(r"/mega_doc/.+/\d+/.+\.html$|/(?:Index2?|Data2?)/.+\.(?:htm|pdf)$", url):
                continue
            label = clean(a.get("title", "")) or title
        code = designation(label)
        if not code or url in seen:
            continue
        seen.add(url)
        items.append(
            {
                "url": url,
                "title": title,
                "label": label,
                "code": code,
                "change_number": change_number(label),
                "source": source,
                "authority": authority,
                "match_status": "candidate",
                "discovered_from": fetched["url"],
                "discovered_at": fetched["checked_at"],
            }
        )
    return items


def parse_document(fetched, *, expected_code=None):
    if source_for(fetched["url"])[0] == "rosstandard":
        return rosstandard.document(fetched, soup_for(fetched))
    if fetched["raw"].startswith(b"%PDF-"):
        return parse_pdf(fetched, expected_code=expected_code)
    if source_for(fetched["url"])[0] == "faufcc":
        raise NormsError("NOT_A_DOCUMENT", "FCS did not return an original PDF")
    soup = soup_for(fetched)
    source, authority = source_for(fetched["url"])
    root = soup.find("main") or soup.body or soup
    h1 = root.find("h1")
    title = (
        clean(h1.get_text(" ", strip=True))
        if h1
        else clean(soup.title.get_text(" ", strip=True) if soup.title else "")
    )
    fields = {}
    for dt in root.find_all("dt"):
        dd = dt.find_next_sibling("dd")
        if dd:
            fields[clean(dt.get_text(" ", strip=True))] = clean(dd.get_text(" ", strip=True))
    # Legacy MegaNorm metadata consists of label/value table rows.
    for row in root.find_all("tr"):
        cells = row.find_all(["td", "th"], recursive=False)
        if len(cells) == 2:
            label = clean(cells[0].get_text(" ", strip=True)).rstrip(":")
            if len(label) <= 80 and label in {
                "Обозначение",
                "Статус",
                "Дата введения",
                "Дата отмены",
                "Заменен на",
                "Дата актуализации текста",
                "Дата актуализации описания",
                "Название рус.",
            }:
                fields[label] = clean(cells[1].get_text(" ", strip=True))
    code = designation(fields.get("Обозначение", title))
    revisions, downloads = [], []
    for a in root.find_all("a", href=True):
        label = clean(a.get_text(" ", strip=True))
        url = urljoin(fetched["url"], a["href"])
        if re.fullmatch(r"редакция от \d{2}\.\d{2}\.\d{4}", label, re.I):
            revisions.append({"date": parse_date(label), "url": url, "label": label})
        if urlsplit(url).path.lower().endswith(".pdf"):
            downloads.append(url)
    changes = []
    for table in root.find_all("table"):
        content = clean(table.get_text(" ", strip=True))
        if "Список изменяющих документов" in content:
            changes.extend(int(n) for n in re.findall(r"Изменени[яе]\s*(?:N|№)\s*(\d+)", content))
    edition_match = re.search(r"ред\.\s*от\s*(\d{2}\.\d{2}\.\d{4})", title, re.I)
    # Preserve image locations, including formulae, instead of silently losing them.
    images = []
    for element in root.find_all(["script", "style", "nav", "footer", "header"]):
        element.decompose()
    for image in root.find_all("img"):
        url = urljoin(fetched["url"], image.get("src", ""))
        images.append({"url": url, "alt": image.get("alt", "")})
        image.replace_with(f"[ИЗОБРАЖЕНИЕ: {url}]")
    blocks, tables, position = [], [], 0
    for node in root.find_all(["div", "p", "h1", "h2", "h3", "h4", "dt", "dd", "li", "table"]):
        if node.find_parent(["p", "li", "table"]):
            continue
        if node.name == "div" and node.find(["div", "p", "h1", "h2", "h3", "h4", "dl", "table"]):
            continue
        text = clean(node.get_text(" ", strip=True))
        if not text:
            continue
        block = {
            "index": len(blocks),
            "start": position,
            "end": position + len(text),
            "text": text,
            "kind": "table" if node.name == "table" else "paragraph",
        }
        if node.name == "table":
            table = {"index": len(tables), "block_index": len(blocks), "rows": []}
            for tr in node.find_all("tr"):
                if tr.find_parent("table") is not node:
                    continue
                cells = []
                for td in tr.find_all(["td", "th"], recursive=False):

                    def span(name):
                        try:
                            return max(1, min(int(td.get(name, 1)), 10000))
                        except ValueError:
                            return 1

                    cells.append(
                        {
                            "text": clean(td.get_text(" ", strip=True)),
                            "header": td.name == "th",
                            "rowspan": span("rowspan"),
                            "colspan": span("colspan"),
                        }
                    )
                table["rows"].append(cells)
            tables.append(table)
            block["table_index"] = table["index"]
        blocks.append(block)
        position += len(text) + 1
    if not blocks:
        text = clean(root.get_text(" ", strip=True))
        blocks = [{"index": 0, "start": 0, "end": len(text), "text": text, "kind": "paragraph"}]
    text = "\n".join(b["text"] for b in blocks)
    if not code:
        # Catalogue/landing pages are not normative document cards.
        raise NormsError("NOT_A_DOCUMENT", "No document designation in the page title or identity field")
    edition_date = parse_date(edition_match[1]) if edition_match else None
    raw_status = fields.get("Статус", "")
    effective = next(
        (
            parse_date(fields[k])
            for k in ("Дата введения изменения в действие", "Дата введения в действие", "Дата введения")
            if k in fields
        ),
        None,
    )
    return {
        "source": source,
        "authority": authority,
        "code": code,
        "title": title,
        "change_number": change_number(fields.get("Обозначение", title)),
        "fields": fields,
        "declared_status": status_value(raw_status),
        "raw_status": raw_status,
        "effective_from": effective,
        "effective_to": parse_date(fields.get("Дата отмены")),
        "edition_date": edition_date,
        "included_changes": sorted(set(changes)),
        "revisions": revisions,
        "downloads": sorted(set(downloads)),
        "images": images,
        "text": text,
        "blocks": blocks,
        "tables": tables,
        "text_status": "metadata_only" if source == "gostinfo" else "extracted_html",
        "limitations": ["Embedded images are preserved as links; their contents are not OCR-verified."]
        if images
        else [],
    }


def pdf_cover_identity(cover, title, expected_code=None, *, sp_edition=False):
    # Body references do not identify a PDF. Use a designation on its own cover line
    # or one that starts an explicit PDF title, and reject contradictory cover evidence.
    lines = [clean(line) for line in cover[:4000].splitlines() if line.strip()]
    if sp_edition:
        lines = [re.sub(r"^СВОД ПРАВИЛ\s+", "", line, flags=re.I) for line in lines]
        lines = [line for line in lines if not line.upper().startswith("СНИП ")]
    cover_codes = {designation(line) for line in lines if CODE.fullmatch(line)}
    for family, number in zip(lines, lines[1:]):
        joined = family + " " + number
        if not any(char.isdigit() for char in family) and CODE.fullmatch(joined):
            cover_codes.add(designation(joined))
    title = clean(title or "")
    if CODE.match(title):
        cover_codes.add(designation(title))
    if len(cover_codes) != 1 or (expected_code and expected_code not in cover_codes):
        raise NormsError(
            "PDF_IDENTITY_UNRESOLVED", "Cannot establish a unique PDF designation from title evidence"
        )
    return cover_codes.pop()


def fcs_pdf_identity(cover, title, expected_code):
    amendments = {
        (designation(line), number)
        for line in cover[:1500].splitlines() + [title or ""]
        if (number := change_number(line)) is not None
    }
    if not amendments:
        return pdf_cover_identity(cover, title, expected_code, sp_edition=True), None
    if len(amendments) == 1:
        code, number = amendments.pop()
        if code and code.startswith("СП ") and expected_code in (None, code):
            return code, number
    raise NormsError("PDF_IDENTITY_UNRESOLVED", "Cannot identify the FCS amendment on the cover")


def parse_pdf(fetched, *, expected_code=None):
    source, authority = source_for(fetched["url"])
    try:
        reader = PdfReader(io.BytesIO(fetched["raw"]))
        if reader.is_encrypted or len(reader.pages) > 300:
            raise NormsError("PDF_LIMIT", "Encrypted PDFs and PDFs over 300 pages require separate intake")
        cover = (reader.pages[0].extract_text() or "") if reader.pages else ""
        title = reader.metadata.title if reader.metadata else None
        if source == "faufcc":
            code, number = fcs_pdf_identity(cover, title, expected_code)
        else:
            code, number = pdf_cover_identity(cover, title, expected_code), None
        blocks, position = [], 0
        for i, page in enumerate(reader.pages):
            text = (page.extract_text() or "") if i else cover
            blocks.append(
                {
                    "index": i,
                    "page_number": i + 1,
                    "kind": "page",
                    "start": position,
                    "end": position + len(text),
                    "text": text,
                }
            )
            position += len(text) + 1
    except NormsError:
        raise
    except Exception as exc:
        raise NormsError("PDF_PARSE_ERROR", f"PDF could not be read ({type(exc).__name__})") from exc
    text = "\n".join(b["text"] for b in blocks)
    if not text.strip():
        raise NormsError("PDF_TEXT_UNAVAILABLE", "PDF has no readable text layer")
    if source == "faufcc" and not code.startswith("СП "):
        raise NormsError("PDF_IDENTITY_UNRESOLVED", "The FCS PDF does not identify a construction SP")
    return {
        "source": source,
        "authority": authority,
        "code": code,
        "title": f"Изменение № {number} к {code}" if number is not None else code,
        "change_number": number,
        "fields": {"Обозначение": code},
        "identity_verified_from_cover": True,
        "raw_status": "",
        "declared_status": "unknown",
        "effective_from": None,
        "effective_to": None,
        "edition_date": None,
        "included_changes": [],
        "revisions": [],
        "downloads": [],
        "images": [],
        "text": text,
        "blocks": blocks,
        "tables": [],
        "text_status": "extracted_pdf",
        "limitations": [
            "PDF reading order, tables and formulae need visual verification; no OCR.",
            "PDF metadata does not establish edition completeness or current status.",
        ],
    }
