"""Текстовые утилиты: нормализация, токенизация, стемминг, синонимы, цитаты."""

from __future__ import annotations

import re
import unicodedata

STOP_WORDS = set(
    """и в во не что он на я с со как а то все она так его но да ты к у же вы за бы по
    только ее мне было вот от меня еще нет о из ему теперь когда даже ну вдруг ли если уже
    или ни быть был него до вас нибудь опять уж вам ведь там потом себя ничего ей может они
    тут где есть надо для мы тебя их чем была сам чтоб без будто чего раз тоже себе под будет
    ж тогда кто этот того потому этого какой совсем ним здесь этом один почти мой тем чтобы нее
    сейчас были куда зачем всех никогда сегодня можно при через после согласно настоящий
    документа проект проектирование проекту объекта объектов объекте здания здание сооружения
    раздел таблица пункт требования требованиях является должны должна должен быть могут
    необходимо следует также который которая которые данном данной этого эта тот""".split()
)

# Группы синонимов/смежных терминов (стемы). Помогают найти положение СТО,
# сформулированное другими словами: «дымоудаление» ↔ «противодымная вентиляция».
SYNONYM_GROUPS: tuple[tuple[str, ...], ...] = (
    ("дымоудал", "противод", "дымосос", "дымопри", "подпор", "дымозащ"),
    ("вентиля", "воздухо", "вентшах", "воздухов", "аэродин"),
    ("шахт", "ствол", "канал", "короб"),
    ("огнесто", "огнезащ", "предело", "rei", "rej"),
    ("гидроиз", "гидрошп", "гидрофо", "водонеп"),
    ("уклон", "градиен", "отметк", "высотн"),
    ("нагрузк", "давлен", "усилие", "несущ"),
    ("освеще", "люкс", "яркост", "инсоляц"),
    ("шум", "звукоиз", "дб", "дба", "акусти"),
    ("отделк", "облицов", "покрыти", "материа"),
    ("электр", "зарядн", "гибрид", "аккумул"),
    ("огражд", "забор", "парапет", "огранич"),
    ("водосн", "хвс", "гвс", "водопро", "водоот", "канализ"),
    ("отопле", "радиат", "теплоно", "итп", "котель"),
    ("навига", "моп", "указате", "табличк"),
    ("вывеск", "нбк", "информа", "реклам"),
    ("парков", "машино", "стоянк", "автосто"),
    ("мгн", "доступн", "пандус", "инвалид", "маломоб"),
    ("пожарн", "эвакуац", "пожар", "апс", "противоп"),
    ("армиро", "армату", "стержн", "каркас"),
    ("бетон", "раствор", "марк", "класс"),
    ("гидроте", "теплоиз", "утепли", "изоляц"),
    ("гермети", "шов", "стык", "примыка"),
    ("испытан", "контрол", "приемк", "надзор"),
)

_STEM_SUFFIXES = tuple(
    sorted(
        """иями ями ами ов ев ий ый ая ое ые ыми ими ать ять ить ение ения ении ению
        ость ости остью ной ных ные ого его ому ему ей ой ых их а я ы и е у ю о ь
        ью ия ию ии""".split(),
        key=len,
        reverse=True,
    )
)


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\x00", " ")).strip()


def fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").casefold().replace("ё", "е")


def tokenize(text: str) -> list[str]:
    return re.findall(r"[a-zа-я0-9]{3,}", fold(text))


def stem(word: str) -> str:
    if not word:
        return word
    if word.isdigit():
        return word
    for suffix in _STEM_SUFFIXES:
        if len(word) - len(suffix) >= 4 and word.endswith(suffix):
            word = word[: -len(suffix)]
            break
    return word[:8]


def query_terms(text: str) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for word in tokenize(text):
        if word in STOP_WORDS:
            continue
        candidate = stem(word)
        if len(candidate) < 3 or candidate in seen:
            continue
        seen.add(candidate)
        terms.append(candidate)
    return terms


def expand_synonyms(terms: list[str]) -> set[str]:
    extra: set[str] = set()
    for term in terms:
        for group in SYNONYM_GROUPS:
            if any(term.startswith(member) or member.startswith(term) for member in group):
                extra.update(group)
    return extra


def fts_query(text: str, max_terms: int = 28) -> str:
    """Построить безопасный MATCH-запрос FTS5: префиксы + синонимы."""
    terms = query_terms(text)
    if not terms:
        return ""
    expanded = list(dict.fromkeys(terms + sorted(expand_synonyms(terms))))
    # FTS-префикс: первые 7 символов ловят падежные окончания.
    parts = []
    for term in expanded[:max_terms]:
        parts.append(f'"{term}"*')
    return " OR ".join(parts)


def _quote_normalize(text: str) -> str:
    text = fold(text)
    text = re.sub(r"[^0-9a-zа-я]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def quote_supported(quote: str, text: str) -> tuple[bool, float]:
    """Проверить цитату по исходному тексту.

    Сначала точное вхождение после нормализации пробелов/пунктуации, затем
    покрытие по предложениям (модель иногда меняет тире/кавычки или
    сокращает цитату). Возвращает (подтверждена, оценка 0..1).
    """
    q = _quote_normalize(quote)
    if len(q) < 8:
        return False, 0.0
    t = _quote_normalize(text)
    if not t:
        return False, 0.0
    if q in t:
        return True, 1.0
    # Разбиваем цитату на куски по 6 слов и проверяем их присутствие.
    words = q.split()
    if len(words) < 4:
        return False, 0.0
    pieces = [" ".join(words[i : i + 6]) for i in range(0, len(words), 6)]
    pieces = [piece for piece in pieces if len(piece.split()) >= 3]
    if not pieces:
        return False, 0.0
    found = sum(1 for piece in pieces if piece in t)
    score = found / len(pieces)
    return score >= 0.8, round(score, 3)


def looks_like_requirement(text: str) -> bool:
    marker = re.compile(
        r"должн|следует|необходимо|требуется|не допуска|запрещ|не менее|не более|"
        r"допускается|предусмотр|принимать|обеспечить|выполнить|составляет|"
        r"\d+\s*(?:мм|см|м2|м3|м|па|кпа|дб|квт|%|°c|м/с|кг)",
        re.IGNORECASE,
    )
    return bool(marker.search(text or ""))


def mostly_english(text: str, minimum_letters: int = 20, threshold: float = 0.6) -> bool:
    """Грубая проверка: текст в основном на английском.

    Используется только как предупреждение для инженера, а не для перевода.
    """
    lowered = (text or "").lower()
    cyrillic = len(re.findall(r"[а-яё]", lowered))
    latin = len(re.findall(r"[a-z]", lowered))
    if latin < minimum_letters:
        return False
    return latin / max(1, latin + cyrillic) > threshold
