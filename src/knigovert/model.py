"""Единая модель книги: все форматы читаются в неё и записываются из неё."""

from __future__ import annotations

import hashlib
import io
import re
import uuid
from dataclasses import dataclass, field, replace
from typing import Callable, Iterable, Iterator, Optional, Union

from PIL import Image as PILImage

Progress = Optional[Callable[[str, Optional[float]], None]]


class ConversionError(Exception):
    """Ошибка с понятным пользователю текстом."""


@dataclass
class Run:
    """Кусок текста с единым оформлением."""

    text: str
    bold: bool = False
    italic: bool = False
    strike: bool = False
    sup: bool = False
    sub: bool = False
    code: bool = False
    href: Optional[str] = None
    note: Optional[str] = None  # id сноски; номер берётся из Note.label

    def same_style(self, other: "Run") -> bool:
        return (
            self.bold == other.bold
            and self.italic == other.italic
            and self.strike == other.strike
            and self.sup == other.sup
            and self.sub == other.sub
            and self.code == other.code
            and self.href == other.href
            and self.note is None
            and other.note is None
        )


@dataclass
class Para:
    runs: list[Run]
    kind: str = "normal"  # normal | subtitle | quote | epigraph | author | poem | code
    align: Optional[str] = None  # center | right


@dataclass
class ImageBlock:
    image_id: str
    alt: str = ""


@dataclass
class ListBlock:
    items: list[list[Run]]
    ordered: bool = False


@dataclass
class TableBlock:
    rows: list[list[list[Run]]]
    header: bool = False


@dataclass
class Gap:
    """Пустая строка / разделитель."""


Block = Union[Para, ImageBlock, ListBlock, TableBlock, Gap]


@dataclass
class PdfRange:
    """Диапазон исходных страниц PDF (для сборки без перевёрстки)."""

    path: str
    start: int  # с нуля, включительно
    end: int  # с нуля, включительно


@dataclass
class Chapter:
    title: str = ""
    level: int = 1
    blocks: list[Block] = field(default_factory=list)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    source: str = ""
    pdf_pages: list[PdfRange] = field(default_factory=list)


@dataclass
class Note:
    id: str
    blocks: list[Block] = field(default_factory=list)
    label: str = ""


@dataclass
class ImageData:
    id: str
    data: bytes
    mime: str  # image/jpeg | image/png
    width: int
    height: int

    @property
    def ext(self) -> str:
        return "jpg" if self.mime == "image/jpeg" else "png"


@dataclass
class Meta:
    title: str = ""
    authors: list[str] = field(default_factory=list)
    lang: str = ""
    annotation: str = ""
    genres: list[str] = field(default_factory=list)
    series: str = ""
    series_number: str = ""
    date: str = ""
    publisher: str = ""
    cover_id: Optional[str] = None


@dataclass
class Book:
    meta: Meta = field(default_factory=Meta)
    chapters: list[Chapter] = field(default_factory=list)
    images: dict[str, ImageData] = field(default_factory=dict)
    notes: dict[str, Note] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def add_image(self, data: bytes) -> Optional[str]:
        """Сохраняет картинку и возвращает её id; неизвестный формат — None."""
        image = make_image(data)
        if image is None:
            return None
        self.images.setdefault(image.id, image)
        return image.id

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)


# --- Текст ---------------------------------------------------------------

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x84\x86-\x9f￾￿]")
_SPACES_RE = re.compile(r"[ \t\r\n\f\v]+")


def clean_text(text: str) -> str:
    """Убирает управляющие символы, которые ломают XML и PDF."""
    return _CONTROL_RE.sub("", text)


def collapse_spaces(text: str) -> str:
    return _SPACES_RE.sub(" ", clean_text(text))


def plain_text(runs: Iterable[Run]) -> str:
    return "".join(run.text for run in runs if run.note is None)


def add_run(runs: list[Run], run: Run) -> None:
    """Добавляет кусок, склеивая с предыдущим при одинаковом оформлении."""
    if not run.text and run.note is None:
        return
    if runs and runs[-1].same_style(run):
        runs[-1].text += run.text
    else:
        runs.append(run)


def trim_runs(runs: list[Run]) -> list[Run]:
    """Срезает пробелы по краям абзаца."""
    result = [replace(run) for run in runs]
    while result and result[0].note is None and not result[0].text.strip():
        result.pop(0)
    while result and result[-1].note is None and not result[-1].text.strip():
        result.pop()
    if result and result[0].note is None:
        result[0].text = result[0].text.lstrip()
    if result and result[-1].note is None:
        result[-1].text = result[-1].text.rstrip()
    return result


def block_runs(block: Block) -> Iterator[Run]:
    if isinstance(block, Para):
        yield from block.runs
    elif isinstance(block, ListBlock):
        for item in block.items:
            yield from item
    elif isinstance(block, TableBlock):
        for row in block.rows:
            for cell in row:
                yield from cell


def block_text(block: Block) -> str:
    if isinstance(block, Para):
        return plain_text(block.runs)
    if isinstance(block, ListBlock):
        return "\n".join(plain_text(item) for item in block.items)
    if isinstance(block, TableBlock):
        return "\n".join("\t".join(plain_text(cell) for cell in row) for row in block.rows)
    return ""


def chapter_text(chapter: Chapter) -> str:
    return "\n".join(filter(None, (block_text(block) for block in chapter.blocks)))


def word_count(chapter: Chapter) -> int:
    return len(chapter_text(chapter).split())


# --- Картинки ------------------------------------------------------------


def make_image(data: bytes) -> Optional[ImageData]:
    """Приводит картинку к JPEG/PNG — их понимают все форматы книг."""
    try:
        with PILImage.open(io.BytesIO(data)) as probe:
            width, height = probe.size
            fmt = probe.format
            if width < 1 or height < 1:
                return None
            if fmt == "JPEG" and probe.mode in ("RGB", "L"):
                mime = "image/jpeg"
            elif fmt == "PNG":
                mime = "image/png"
            else:
                converted = probe.convert("RGBA" if "A" in probe.getbands() else "RGB")
                buffer = io.BytesIO()
                converted.save(buffer, "PNG", optimize=True)
                data = buffer.getvalue()
                mime = "image/png"
    except Exception:
        return None
    return ImageData(hashlib.sha1(data).hexdigest()[:16], data, mime, width, height)


# --- Подготовка к записи -------------------------------------------------


def normalize_levels(chapters: list[Chapter]) -> list[Chapter]:
    """Убирает скачки уровней (1 → 3), иначе ломаются закладки и вложенность FB2."""
    result = []
    previous = 0
    minimum = min((chapter.level for chapter in chapters), default=1)
    for chapter in chapters:
        level = max(1, chapter.level - minimum + 1)
        level = min(level, previous + 1)
        result.append(replace(chapter, level=level))
        previous = level
    return result


def prepare_for_write(book: Book) -> Book:
    """Нумерует сноски по порядку чтения и оставляет только используемое."""
    chapters = normalize_levels(book.chapters)
    notes: dict[str, Note] = {}

    def visit(blocks: Iterable[Block]) -> None:
        for block in blocks:
            for run in block_runs(block):
                if run.note and run.note in book.notes and run.note not in notes:
                    source = book.notes[run.note]
                    notes[run.note] = Note(source.id, source.blocks, str(len(notes) + 1))
                    visit(source.blocks)

    used_images = set()
    if book.meta.cover_id:
        used_images.add(book.meta.cover_id)
    for chapter in chapters:
        visit(chapter.blocks)
    for blocks in [chapter.blocks for chapter in chapters] + [n.blocks for n in notes.values()]:
        for block in blocks:
            if isinstance(block, ImageBlock):
                used_images.add(block.image_id)

    images = {key: value for key, value in book.images.items() if key in used_images}
    meta = replace(book.meta)
    if meta.cover_id not in images:
        meta.cover_id = None
    if not meta.lang:
        meta.lang = detect_language(book)
    return Book(meta, chapters, images, notes, list(book.warnings))


_KAZAKH = set("әғқңөұүһіӘҒҚҢӨҰҮҺІ")


def detect_language(book: Book) -> str:
    sample = " ".join(chapter_text(chapter) for chapter in book.chapters[:5])[:50000]
    letters = [char for char in sample if char.isalpha()]
    if not letters:
        return "ru"
    cyrillic = sum("Ѐ" <= char <= "ӿ" for char in letters)
    if cyrillic / len(letters) < 0.3:
        return "en"
    kazakh = sum(char in _KAZAKH for char in letters)
    return "kk" if kazakh / max(cyrillic, 1) > 0.015 else "ru"


def book_title(book: Book, fallback: str = "Без названия") -> str:
    return (book.meta.title or fallback).strip() or fallback
