from conftest import fetched

from sp_mcp.config import Config
from sp_mcp.service import Service


def test_cold_search_finds_russian_inflection_without_model_or_preloaded_corpus(tmp_path, monkeypatch):
    class Network:
        def fetch(self, url):
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


def test_smoke_concept_discovers_fire_ventilation_document_without_keyword_in_title(config, monkeypatch):
    class Network:
        def fetch(self, url):
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
