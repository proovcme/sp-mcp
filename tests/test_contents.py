from conftest import fetched

from sp_mcp.parsers import parse_document
from sp_mcp.service import Service


def test_contents_resolves_duplicate_front_matter_to_actual_body_heading(config):
    service = Service(config)
    raw = fetched("""<title>СП 900.100.2099. Учебное оглавление</title>
      <div>1 Область применения</div><div>7.11 Воздуховоды</div><div>Приложение Д. Учебная методика</div>
      <div>1 ИСПОЛНИТЕЛИ — учебные организации</div><div>1 июля 2099 года</div>
      <div>1 Область применения</div><div>7.11 Воздуховоды</div>
      <div>7.11.1 Синтетическое требование, а не заголовок.</div>
      <div>Приложение Д. Учебная методика</div>""")
    doc = service.store.save(raw, parse_document(raw))
    contents = service.contents(doc["snapshot_id"])
    titles = [item["title"] for item in contents["items"]]
    assert titles == ["1 Область применения", "7.11 Воздуховоды", "Приложение Д. Учебная методика"]
    for item in contents["items"]:
        assert item["start"] == doc["text"].rfind(item["title"])
        assert doc["text"][item["start"] : item["end"]] == item["title"]


def test_contents_ignores_abbreviated_front_toc_and_preserves_appendix_numbering(config):
    service = Service(config)
    raw = fetched("""<title>СП 900.100.2099. Учебный документ</title>
      <div>Содержание</div><div>1 Область применения</div><div>2 Метод расчета</div>
      <div>Приложение А. Расчет</div><div>1 Исходные данные</div>
      <div>1. ОБЛАСТЬ ПРИМЕНЕНИЯ</div><div>2. МЕТОД РАСЧЕТА</div>
      <div>3. ЗАЩИТНОЕ ЗАЗЕМЛЕНИЕ.</div>
      <div>2.1 Расход определяется по формуле [ИЗОБРАЖЕНИЕ]</div>
      <div>Приложение А</div><div>1 Исходные данные</div><div>2 Результаты</div>
      <div>1 - 14 - элементы на схеме</div><div>3 группе помещений</div>
      <div>А.1 Исходные условия</div><div>А.1.1 Расчетная схема</div>
      <div>А.1.1.1 Учебное требование к элементу</div>""")
    doc = service.store.save(raw, parse_document(raw))
    items = service.contents(doc["snapshot_id"])["items"]
    assert [item["title"] for item in items] == [
        "1. ОБЛАСТЬ ПРИМЕНЕНИЯ",
        "2. МЕТОД РАСЧЕТА",
        "3. ЗАЩИТНОЕ ЗАЗЕМЛЕНИЕ.",
        "Приложение А",
        "1 Исходные данные",
        "2 Результаты",
        "А.1 Исходные условия",
        "А.1.1 Расчетная схема",
    ]
    for item in items:
        assert item["start"] == doc["text"].rfind(item["title"])
