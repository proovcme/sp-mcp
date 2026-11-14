"""Deterministic Russian morphology for retrieval, independent of embedding models."""

import functools
import re
import threading

import snowballstemmer

_local = threading.local()
STOP = set(
    "а без бы в во где для до есть и из или к как ли на не но о об от по при про с со то у что это чем нужно нужен можно какие какой".split()
)

# Retrieval associations only, never statements of normative equivalence or applicability.
CONCEPTS = (
    ("пожаротушение", "тушение", "тушить", "огнетушащий"),
    ("серверная", "серверный", "вычислительный", "датацентр", "ЦОД"),
    ("вентиляция", "воздухообмен", "воздуховод", "вентилирование"),
    ("дымоудаление", "противодымный", "дымозащита"),
    ("отопление", "теплоснабжение", "обогрев"),
    ("эвакуация", "эвакуационный", "выход"),
    ("заземление", "заземляющий", "уравнивание потенциалов"),
    ("электроснабжение", "электропитание", "электроустановка"),
    ("водоснабжение", "водопровод"),
    ("канализация", "водоотведение", "стоки"),
    ("инвалид", "маломобильный", "доступность", "МГН"),
    ("огнестойкость", "огнестойкий", "огнезащита"),
)

# Subject discovery searches source catalogues as well as already saved texts.
# A title must match every context phrase; ventilation alone is not smoke control.
CATALOG_TOPICS = (
    (
        ("дымоудаление", "противодымный", "дымозащита"),
        ("вентиляц", "противодымн"),
        ("вентиляция", "пожарная безопасность"),
    ),
)


def catalog_queries(query):
    found = set(stems(query))
    return list(
        dict.fromkeys(
            term
            for trigger, queries, _ in CATALOG_TOPICS
            if found & set(stems(" ".join(trigger)))
            for term in queries
        )
    )


def topic_relevance(query, title):
    requested, present = set(stems(query)), set(stems(title))
    return max(
        (
            1.25
            for trigger, _, context in CATALOG_TOPICS
            if requested & set(stems(" ".join(trigger)))
            and all(set(stems(phrase)) <= present for phrase in context)
        ),
        default=0,
    )


def concepts(query):
    groups = []
    for term in dict.fromkeys(stems(query)):
        group = {term}
        for values in CONCEPTS:
            related = set(stems(" ".join(values)))
            if term in related:
                group.update(related)
        groups.append(group)
    return groups


@functools.lru_cache(maxsize=20000)
def stems(text):
    if not hasattr(_local, "stemmer"):
        _local.stemmer = snowballstemmer.stemmer("russian")
    words = re.findall(r"[а-яёa-z0-9]+", text.casefold().replace("ё", "е"))
    return tuple(_local.stemmer.stemWord(word) for word in words if word not in STOP and len(word) > 1)


def relevance(query, text):
    required = set(stems(query))
    found = set(stems(text))
    if not required:
        return 0
    return len(required & found) / len(required)
