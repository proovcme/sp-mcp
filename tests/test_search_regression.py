from urllib.parse import parse_qs, urlsplit

import pytest
from conftest import fcs_empty_catalogue, fetched, ros_empty_catalogue

from sp_mcp.config import Config
from sp_mcp.parsers import parse_candidates, parse_document
from sp_mcp.service import Service


def test_cold_search_finds_russian_inflection_without_model_or_preloaded_corpus(tmp_path, monkeypatch):
    class Network:
        def fetch(self, url):
            if url.startswith("https://api.faufcc.ru/"):
                return fcs_empty_catalogue(url)
            if urlsplit(url).hostname == "protect.gost.ru":
                return ros_empty_catalogue(url)
            if "new.gostinfo.ru/catalog?" in url and "%D0%BF%D0%BE%D0%B6%D0%B0%D1%80" in url:
                return fetched(
                    """<a href="/catalog/gost/999"><strong>ГОСТ Р 99000-2099</strong>
                    Установки газового пожаротушения. Синтетический пример.</a>""",
                    url,
                )
            return fetched("<html><body>Синтетический каталог без дополнительных записей.</body></html>", url)

    service = Service(Config(root=tmp_path), Network())
    monkeypatch.setattr(
        service.retrieval, "provider", lambda: (_ for _ in ()).throw(AssertionError("Unexpected model"))
    )
    result = service.search("газовое пожаротушение")
    assert any(item["code"] == "ГОСТ Р 99000-2099" for item in result["items"])
    assert result["retrieval"]["model_used"] is False
    assert not result["source_errors"]


@pytest.mark.parametrize("query", ["бытовая канализация", "производственная канализация"])
def test_sewer_search_discovers_internal_and_external_design_documents(config, monkeypatch, query):
    titles = {
        "900": "Внутренний водопровод и канализация зданий",
        "901": "Канализация. Наружные сети и сооружения",
        "902": "Наружные сети водоснабжения и канализации. Строительство",
        "903": "Внутренняя канализация. Проектирование и монтаж полипропиленовых труб",
    }
    requests = []

    class Network:
        def fetch(self, url):
            if url.startswith("https://api.faufcc.ru/"):
                return fcs_empty_catalogue(url)
            if urlsplit(url).hostname == "protect.gost.ru":
                return ros_empty_catalogue(url)
            requests.append(url)
            if urlsplit(url).path == "/catalog" and parse_qs(urlsplit(url).query).get("q") == ["канализац"]:
                return fetched(
                    "".join(
                        f'<a href="/catalog/sp/{code}"><strong>СП {code}.100.2099</strong> Действует {title}</a>'
                        for code, title in titles.items()
                    ),
                    url,
                )
            if "/mega_doc/norm/sp_svod-pravil/" in url:
                return fetched(
                    "".join(
                        f'<a href="https://meganorm.ru/mega_doc/norm/sp/0/sewer-{code}.html">СП {code}.100.2099</a>'
                        for code in titles
                        if code != "901"
                    ),
                    url,
                )
            for code, title in titles.items():
                if url.endswith(f"sewer-{code}.html"):
                    return fetched(
                        f"<title>СП {code}.100.2099. {title}</title>"
                        "<div>18.1. Бытовая канализация и производственная канализация описаны "
                        "в этом синтетическом учебном примере для проверки поиска. Это не нормативное требование.</div>"
                        '<a href="https://meganorm.ru/mega_doc/norm/sp/0/sewer-901.html">СП 901.100.2099</a>',
                        url,
                    )
            return fetched("<html>Синтетический пустой каталог</html>", url)

    service = Service(config, Network())
    monkeypatch.setattr(service.retrieval, "provider", lambda: pytest.fail("Unexpected model"))
    result = service.search(query)
    assert result["discovery_anchor"] == "канализац"
    assert list(dict.fromkeys(i["code"] for i in result["items"]))[:2] == [
        "СП 900.100.2099",
        "СП 901.100.2099",
    ]
    assert [i["code"] for i in result["text_hits"]] == ["СП 900.100.2099", "СП 901.100.2099"]
    assert all(i["clause"] == "18.1" for i in result["text_hits"])
    assert len(result["hydrated_snapshots"]) == 2
    assert result["retrieval"]["model_used"] is False
    assert not result["source_errors"]
    assert not any(parse_qs(urlsplit(url).query).get("q") == ["производственн"] for url in requests)


def test_incidental_system_mentions_and_partial_queries_are_supplementary(config):
    service = Service(config)
    examples = [
        (
            "910",
            "Вентиляция и кондиционирование",
            "7.1 Производственная канализация упомянута в учебном ограничении вентиляционных шахт.",
        ),
        (
            "911",
            "Благоустройство территории",
            "7.1 Производственный поверхностный сток описан в учебном примере.",
        ),
        ("912", "Канализация зданий", "7.1 Бытовая канализация описана в учебном примере."),
    ]
    for code, title, body in examples:
        raw = fetched(
            f"<title>СП {code}.100.2099. {title}</title><div>{body}</div>",
            f"https://meganorm.ru/mega_doc/norm/sp/0/test-{code}.html",
        )
        service.store.save(raw, parse_document(raw))
    result = service.search("производственная канализация", online=False)
    assert not result["text_hits"]
    assert not result["items"]
    assert {i["code"] for i in result["related_text_hits"]} == {
        "СП 910.100.2099",
        "СП 911.100.2099",
        "СП 912.100.2099",
    }


def test_bare_cross_reference_cannot_replace_rich_catalogue_observation(config):
    service = Service(config)
    listing = fetched(
        '<a href="/catalog/sp/999"><strong>СП 930.100.2099</strong> Действует Канализация. Наружные сети и сооружения</a>',
        "https://new.gostinfo.ru/catalog?q=test",
    )
    rich = parse_candidates(listing)[0]
    bare = parse_candidates(
        fetched('<a href="/catalog/sp/999">СП 930.100.2099</a>', "https://new.gostinfo.ru/catalog/sp/998")
    )[0]
    service.store.add_candidates([rich])
    service.store.add_candidates([bare])
    assert service.store.candidates("СП 930.100.2099")[0] == rich


def test_document_pagination_does_not_count_duplicate_discovery_links(config):
    service = Service(config)
    candidates = []
    for number in range(30):
        candidates.extend(
            parse_candidates(
                fetched(
                    f'<a href="/catalog/sp/{number}"><strong>СП 940.100.2099</strong> Действует Внутренний водопровод и канализация зданий</a>',
                    "https://new.gostinfo.ru/catalog?q=test",
                )
            )
        )
    candidates.extend(
        parse_candidates(
            fetched(
                '<a href="/catalog/sp/999"><strong>СП 941.100.2099</strong> Действует Канализация. Наружные сети и сооружения</a>',
                "https://new.gostinfo.ru/catalog?q=test",
            )
        )
    )
    service.store.add_candidates(candidates)
    result = service.search("бытовая канализация", online=False, limit=2)
    assert [i["code"] for i in result["items"]] == ["СП 940.100.2099", "СП 941.100.2099"]
    assert not result["more_results_possible"]


def test_smoke_concept_discovers_fire_ventilation_document_without_keyword_in_title(config, monkeypatch):
    class Network:
        def fetch(self, url):
            if url.startswith("https://api.faufcc.ru/"):
                return fcs_empty_catalogue(url)
            if urlsplit(url).hostname == "protect.gost.ru":
                return ros_empty_catalogue(url)
            if (
                "new.gostinfo.ru/catalog?" in url
                and "%D0%B2%D0%B5%D0%BD%D1%82%D0%B8%D0%BB%D1%8F%D1%86" in url
            ):
                return fetched(
                    """<a href="/catalog/sp/999"><strong>СП 900.100.2099</strong> Действует
                    Отопление, вентиляция и кондиционирование. Требования пожарной безопасности</a>""",
                    url,
                )
            if "/mega_doc/norm/sp_svod-pravil/" in url:
                return fetched(
                    '<a href="https://meganorm.ru/mega_doc/norm/sp/0/fire-ventilation.html">СП 900.100.2099</a>',
                    url,
                )
            if url.endswith("fire-ventilation.html"):
                return fetched(
                    """<title>СП 900.100.2099. Учебный документ</title>
                    <div>7 Противодымная вентиляция</div><div>7.1 Синтетическое требование к противодымной вентиляции
                    учебного объекта и управлению вентиляционным оборудованием. Этот текст не является нормой.</div>""",
                    url,
                )
            return fetched("<html>Синтетический пустой каталог</html>", url)

    service = Service(config, Network())
    monkeypatch.setattr(
        service.retrieval, "provider", lambda: (_ for _ in ()).throw(AssertionError("Unexpected model"))
    )
    result = service.search("дымоудаление")
    assert result["items"][0]["code"] == "СП 900.100.2099"
    assert result["text_hits"][0]["clause"] == "7.1"
    assert result["retrieval"]["model_used"] is False
    assert not result["source_errors"]
