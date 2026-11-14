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
