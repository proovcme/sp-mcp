from urllib.parse import urlsplit

import pytest

from sp_mcp.config import Config
from sp_mcp.storage import now


@pytest.fixture
def config(tmp_path):
    return Config(root=tmp_path, interval=0.1)


def fetched(html, url="https://meganorm.ru/mega_doc/norm/sp/0/example.html"):
    return {"url": url, "raw": html.encode("utf-8"), "checked_at": now(), "content_type": "text/html"}


@pytest.fixture
def synthetic_html():
    return """<html><head><meta charset="utf-8"><title>СП 900.100.2099.
    Учебный документ (ред. от 01.02.2099)</title></head><body>
    <div class="wrapper"><div>7.1 Учебный раздел</div>
    <div>7.1.1 Синтетическое требование к проходу.</div>
    <div>Дополнительное условие и <img src="formula.gif" alt="Формула">.</div>
    <table><tr><th rowspan="2">Группа</th><th>Параметр</th></tr>
    <tr><td colspan="2">Учебное значение</td></tr></table>
    <div>7.1.2 Следующее требование.</div></div>
    <a href="/mega_doc/update/sp/0/example.html">редакция от 02.03.2099</a>
    <table><tr><td>Список изменяющих документов (в ред. Изменения N 1,
    Изменения N 2)</td></tr></table></body></html>"""


def official_html(code="СП 900.100.2099", change=None, effective="01.02.2020", status="Действует"):
    label = f"Изменение №{change} к {code}" if change else code
    date_label = "Дата введения изменения в действие" if change else "Дата введения в действие"
    return f"""<html><head><meta charset="utf-8"></head><body><main><h1>{label}</h1><dl>
    <dt>Обозначение</dt><dd>{label}</dd><dt>Статус</dt><dd>{status}</dd>
    <dt>{date_label}</dt><dd>{effective}</dd></dl></main></body></html>"""


def ros_empty_catalogue(url):
    path = urlsplit(url).path
    return fetched(
        f'<form method="GET" action="{path}"><input name="search"></form><h3>Документы не найдены</h3>', url
    )


def fcs_empty_catalogue(url):
    return fetched('{"data":[],"meta":{"pagination":{"currentPage":1,"totalPages":1}}}', url)
