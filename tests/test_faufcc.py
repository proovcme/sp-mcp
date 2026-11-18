import json
from types import SimpleNamespace

import pytest
from conftest import fetched
from test_rosstandard import CARD, CHANGE, CODE, ROOT, card_html, listing

from sp_mcp import faufcc
from sp_mcp.errors import NormsError
from sp_mcp.network import validate_url
from sp_mcp.service import Service

ENTRY_ID = "00000000-0000-4000-8000-000000000010"
BASE_PDF = f"{faufcc.ORIGIN}/api/assets/00000000-0000-4000-8000-000000000011"
CHANGE_PDF = f"{faufcc.ORIGIN}/api/assets/00000000-0000-4000-8000-000000000012"


def asset(url, name):
    return {"mime": "application/pdf", "name": name, "links": {"open": url}}


def operation(order, url):
    fields = {
        "operation-type": {"name": "modification"},
        "order-name": order,
        "order-date": "2020-01-01T00:00:00+03:00",
        "active-since": "2020-01-01T00:00:00+03:00",
        "order-document": asset(url, "Синтетическое вложение.pdf"),
    }
    return {
        "groups": {
            "data": [
                {"fields": {"data": [{"name": key, "value": {"data": [v]}} for key, v in fields.items()]}}
            ]
        }
    }


def test_open_fcs_original_and_reconcile_amendments(config, monkeypatch):
    class Network:
        def fetch(self, url):
            if url == faufcc.search_url(CODE, 1):
                row = {
                    "id": ENTRY_ID,
                    "number": CODE,
                    "name": "Учебный СП",
                    "asset": {"data": asset(BASE_PDF, "")},
                }
                data = {"data": [row], "meta": {"pagination": {"currentPage": 1, "totalPages": 1}}}
                return fetched(json.dumps(data), url)
            if url == faufcc.entry_url(ENTRY_ID):
                data = {
                    "id": ENTRY_ID,
                    "number": CODE,
                    "operations": {
                        "data": [
                            operation("Минстроя № 55/пр", CHANGE_PDF),
                            operation("Минстроя № 56/пр", None),
                        ]
                    },
                }
                return fetched(json.dumps({"data": data}), url)
            if url in {BASE_PDF, CHANGE_PDF}:
                return fetched("%PDF-" + url, url)
            if url.startswith(ROOT + "/sp?"):
                return listing(url)
            if url == ROOT + CARD:
                return fetched(card_html(), url)
            if url == ROOT + CHANGE:
                return fetched(
                    card_html(change=True).replace(
                        "<span>Номер изменения",
                        "<span>Сведения о регистрации изменения</span><p>55/пр</p></div><div><span>Номер изменения",
                    ),
                    url,
                )
            return fetched("<html>Пусто</html>", url)

    def pdf_reader(file):
        cover = (
            f"СВОД ПРАВИЛ {CODE}\nСНиП 90-01-99"
            if file.getvalue().endswith(BASE_PDF.encode())
            else f"Изменение № 1 к {CODE}\n1.2 Слова заменить."
        )
        return SimpleNamespace(
            is_encrypted=False,
            metadata=None,
            pages=[SimpleNamespace(extract_text=lambda: cover)],
        )

    monkeypatch.setattr("sp_mcp.parsers.PdfReader", pdf_reader)
    service = Service(config, Network())
    result = service.search(CODE)
    assert result["items"][0]["url"] == BASE_PDF
    original = service.open(BASE_PDF)
    assert original["source"] == "faufcc"
    assert original["included_changes"] == []
    reading = service.read(original["snapshot_id"], expected_sha256=original["provenance"]["sha256"])
    assert reading["blocks"][0]["page_number"] == 1
    assert "СВОД ПРАВИЛ" in reading["text"]

    report = service.check(CODE, original["snapshot_id"])
    assert report["missing_changes"] == [1]
    assert report["text_revision_status"] == "outdated"
    publisher = report["publisher"]
    assert publisher["originals"][0]["url"] == BASE_PDF
    assert publisher["amendments"][0]["rosstandard_url"] == ROOT + CHANGE
    matched = publisher["amendments"][0]["files"][0]
    assert matched["url"] == CHANGE_PDF and matched["effective_date_disagreement"]
    assert publisher["unmatched_files"][0]["order"] == "Минстроя № 56/пр"
    assert service.open(CHANGE_PDF)["change_number"] == 1


def test_fcs_attachment_url_cannot_escape_source():
    assert faufcc.pdf_url(asset("https://example.org/other.pdf", "")) is None
    with pytest.raises(NormsError, match="public FCS registry"):
        validate_url(f"{faufcc.ORIGIN}/api/me")
