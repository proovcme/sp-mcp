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
    ("производственный", "промышленный"),
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
        1.25,
    ),
    (
        ("канализация", "канализационный", "водоотведение"),
        ("канализац", "водоотведен"),
        ("внутренний", "канализация"),
        1.5,
    ),
    (
        ("канализация", "канализационный", "водоотведение"),
        ("канализац", "водоотведен"),
        ("канализация", "наружные сети"),
        1.4,
    ),
)

# System nouns anchor discovery; qualifiers such as "industrial" must not replace them.
SYSTEMS = (
    ("канализация", "канализационный", "водоотведение"),
    ("пожаротушение", "тушение", "тушить", "огнетушащий"),
    ("дымоудаление", "противодымный", "дымозащита"),
    ("вентиляция", "воздухообмен", "воздуховод", "вентилирование"),
    ("водоснабжение", "водопровод"),
    ("отопление", "теплоснабжение", "обогрев"),
    ("электроснабжение", "электропитание", "электроустановка"),
    ("заземление", "заземляющий"),
)


def catalog_anchor(query):
    requested = set(stems(query))
    for system in SYSTEMS:
        for word in system:
            for term in stems(word):
                if term in requested:
                    return term
    return max(requested, key=len, default="")


def subject_compatible(query, title, fragment):
    """Keep incidental mentions in a different discipline out of the primary fragments."""
    requested, present, body = set(stems(query)), set(stems(title)), set(stems(fragment))
    systems = [set(stems(" ".join(group))) for group in SYSTEMS]
    wanted = [group for group in systems if group & requested]
    if not wanted or topic_relevance(query, title) or any(group & present for group in wanted):
        return True
    if any(group & present for group in systems if group not in wanted):
        return False
    return all(group & body for group in wanted)


def catalog_queries(query):
    found = set(stems(query))
    return list(
        dict.fromkeys(
            term
            for trigger, queries, _, _ in CATALOG_TOPICS
            if found & set(stems(" ".join(trigger)))
            for term in queries
        )
    )


def topic_relevance(query, title):
    requested, present = set(stems(query)), set(stems(title))
    score = max(
        (
            weight
            for trigger, _, context, weight in CATALOG_TOPICS
            if requested & set(stems(" ".join(trigger)))
            and all(set(stems(phrase)) <= present for phrase in context)
        ),
        default=0,
    )
    if (
        requested & set(stems("канализация водоотведение"))
        and present & set(stems("водоснабжение водопровод"))
        and not set(stems("внутренний")) <= present
    ):
        score = max(0, score - 0.1)
    if re.search(r"документаци|чертеж|технические условия|методы испытаний", title, re.I):
        score *= 0.5
    materials = set(stems("труба трубопровод полимер полипропилен полиэтилен поливинилхлорид"))
    if present & materials and not requested & materials:
        score *= 0.5
    return score


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


def match_span(query, text):
    """Smallest literal word window containing all query stems; no phrase invention."""
    required = set(stems(query))
    words = stems(text)
    counts, left, best = {}, 0, len(words) + 1000
    if not required:
        return best
    for right, term in enumerate(words):
        if term in required:
            counts[term] = counts.get(term, 0) + 1
        while len(counts) == len(required):
            best = min(best, right - left + 1)
            first = words[left]
            if first in counts:
                counts[first] -= 1
                if not counts[first]:
                    del counts[first]
            left += 1
    return best
