"""Поиск заголовков глав в тексте без разметки."""

from __future__ import annotations

import re
from typing import Iterable, Optional

from .model import Block, Chapter, Para, Run, plain_text

_ORDINALS = (
    r"перв\w*|втор\w*|трет\w*|четв[её]рт\w*|пят\w*|шест\w*|седьм\w*|восьм\w*|девят\w*|десят\w*|"
    r"\w+надцат\w*|двадцат\w*|тридцат\w*|сороков\w*|пятидесят\w*|сот\w*|"
    r"first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|one|two|three|four|five|"
    r"six|seven|eight|nine|ten|eleven|twelve|\w+teen|twenty\w*|thirty\w*"
)
_NUMBER = rf"(?:\d{{1,4}}|[ivxlcdm]{{1,8}}|{_ORDINALS})"

# «Глава 1», «Часть вторая. Весна», «Chapter IV», «1-тарау»
_NUMBERED_RE = re.compile(
    rf"^(?:глава|гл\.|часть|книга|том|раздел|chapter|part|book|section)\s+{_NUMBER}\b"
    r"(?:\s*[.:—–-]\s*.{0,100}|\s+[^.!?]{0,100})?$",
    re.IGNORECASE,
)
_KAZAKH_RE = re.compile(r"^\d{1,3}[-\s]*(?:тарау|бөлім)\b.{0,100}$", re.IGNORECASE)
_NAMED_RE = re.compile(
    r"^(?:пролог|эпилог|предисловие|послесловие|введение|заключение|вступление|от автора|"
    r"prologue|epilogue|preface|foreword|introduction|afterword|conclusion|"
    r"кіріспе|қорытынды|алғы сөз)[.:]?(?:\s*[.:—–-]\s*.{0,80})?$",
    re.IGNORECASE,
)
_BARE_NUMBER_RE = re.compile(r"^(?:\d{1,3}|[IVXLC]{1,7})\.?$")


def is_chapter_title(text: str) -> bool:
    """Строка явно выглядит как заголовок главы."""
    text = text.strip()
    if not text or len(text) > 120:
        return False
    return bool(
        _NUMBERED_RE.match(text) or _KAZAKH_RE.match(text) or _NAMED_RE.match(text)
    )


def is_bare_number(text: str) -> bool:
    """Строка из одного номера: «12» или «XIV». Заголовок только если таких много."""
    return bool(_BARE_NUMBER_RE.match(text.strip()))


def split_by_titles(
    blocks: Iterable[Block],
    title_level: dict[int, int],
    source: str = "",
) -> list[Chapter]:
    """Режет блоки на главы по индексам заголовков {индекс блока: уровень}."""
    chapters: list[Chapter] = []
    current: Optional[Chapter] = None
    for index, block in enumerate(blocks):
        if index in title_level and isinstance(block, Para):
            title = plain_text(block.runs).strip()
            if (
                chapters
                and not chapters[-1].blocks
                and _same_level_title(chapters[-1], title_level[index])
                and not is_chapter_title(title)
            ):
                # Заголовок из двух строк: «Глава 1» + «Начало»
                chapters[-1].title = f"{chapters[-1].title}. {title}"
                current = chapters[-1]
                continue
            current = Chapter(title=title, level=title_level[index], source=source)
            chapters.append(current)
            continue
        if current is None:
            current = Chapter(title="", level=1, source=source)
            chapters.append(current)
        current.blocks.append(block)
    return chapters


def _same_level_title(chapter: Chapter, level: int) -> bool:
    return chapter.level == level and is_chapter_title(chapter.title) and len(chapter.title) < 40


def find_chapter_titles(blocks: list[Block]) -> dict[int, int]:
    """Ищет заголовки по тексту в потоке абзацев без стилей."""
    found: dict[int, int] = {}
    bare: list[int] = []
    for index, block in enumerate(blocks):
        if not isinstance(block, Para) or block.kind != "normal":
            continue
        text = plain_text(block.runs).strip()
        if is_chapter_title(text):
            found[index] = _title_level(text)
        elif is_bare_number(text):
            bare.append(index)
    # Голые номера считаем главами, только если их несколько и они идут по порядку.
    if len(bare) >= 3 and not found:
        for index in bare:
            found[index] = 1
    # Если встречаются только части или только главы — это один уровень.
    if found and len(set(found.values())) == 1:
        found = {index: 1 for index in found}
    elif found:
        top = min(found.values())
        found = {index: level - top + 1 for index, level in found.items()}
    return found


def _title_level(text: str) -> int:
    lowered = text.lower()
    if lowered.startswith(("книга", "том", "book")):
        return 1
    if lowered.startswith(("часть", "part", "бөлім")) or "бөлім" in lowered[:12]:
        return 1
    if lowered.startswith(("глава", "гл.", "chapter", "раздел", "section")) or "тарау" in lowered[:12]:
        return 2
    return 2


def title_run(text: str) -> list[Run]:
    return [Run(text)]
