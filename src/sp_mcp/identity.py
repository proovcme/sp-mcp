import re
import unicodedata
from datetime import date
from urllib.parse import urlsplit

CODE = re.compile(
    r"(?<![\w])(?:ГОСТ(?:\s+Р)?(?:\s+(?:ИСО|ISO|МЭК|IEC|EN|РВ))?(?:[/ ](?:МЭК|IEC))?"
    r"|СП|СНиП|СанПиН|ГН|НПБ|ВСН|РД)\s*\d[\d.]*\s*(?:[-–—]\s*\d{2,4}){0,2}",
    re.I,
)


def clean(text):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text)).strip()


def designation(text):
    match = CODE.search(clean(text))
    if not match:
        return None
    value = re.sub(r"\s*[-–—]\s*", "-", clean(match.group()).rstrip(".")).upper()
    value = re.sub(r"(?<=[А-ЯA-Z])(?=\d)", " ", value)
    return clean(value).upper()


def change_number(text):
    match = re.search(r"^Изменени[ея]\s*(?:№|N(?:o|о)?)?\s*(\d+)\s+к", clean(text), re.I)
    return int(match[1]) if match else None


def candidate_code(item):
    """Re-read a cached discovery label without rewriting immutable document snapshots."""
    return designation(item.get("label") or item.get("title", "")) or item["code"]


def prefer_candidate(previous, incoming):
    """Keep a catalogue observation intact when a bare cross-reference is discovered."""
    if previous is None:
        return incoming

    def quality(item):
        origin = urlsplit(item.get("discovered_from", ""))
        official_listing = item.get("source") == "gostinfo" and origin.path.rstrip("/") == "/catalog"
        title = clean(item.get("title", ""))
        label = clean(item.get("label", ""))
        rich = any(len(value) > len(candidate_code(item) or "") + 12 for value in (title, label))
        return official_listing, rich, item.get("discovered_at", ""), len(title) + len(label)

    return incoming if quality(incoming) >= quality(previous) else previous


def parse_date(text):
    if not text:
        return None
    match = re.search(r"(?<!\d)(\d{1,2})\.(\d{1,2})\.(\d{4})(?!\d)", text)
    if match:
        try:
            return date(int(match[3]), int(match[2]), int(match[1])).isoformat()
        except ValueError:
            return None
    months = "января февраля марта апреля мая июня июля августа сентября октября ноября декабря".split()
    match = re.search(r"(\d{1,2})\s+(" + "|".join(months) + r")\s+(\d{4})", text.lower())
    if match:
        try:
            return date(int(match[3]), months.index(match[2]) + 1, int(match[1])).isoformat()
        except ValueError:
            return None
    return None


def status_value(text):
    text = clean(text).lower()
    if any(word in text for word in ("утратил", "отмен", "не действ")):
        return "cancelled"
    if "заменен" in text or "заменён" in text:
        return "replaced"
    if "не введен" in text or "не введён" in text:
        return "not_yet_effective"
    if text in {"действует", "действующий", "действующая", "введен впервые"}:
        return "active"
    return "unknown"
