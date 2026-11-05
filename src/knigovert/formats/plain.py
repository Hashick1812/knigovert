"""Простой текст: TXT."""

from __future__ import annotations

import re
import statistics
from pathlib import Path
from typing import Optional

from ..chapters import find_chapter_titles, split_by_titles
from ..model import (
    Block,
    Book,
    Chapter,
    ConversionError,
    Gap,
    ImageBlock,
    ListBlock,
    Para,
    Run,
    TableBlock,
    book_title,
    clean_text,
    plain_text,
    prepare_for_write,
)
from .markup import decode_text

_INDENT_RE = re.compile(r"^(?:\s{2,}|\t|　)")
_SEPARATOR_RE = re.compile(r"^[\s*\-–—~=#_]{3,}$")


def _looks_hard_wrapped(lines: list[str]) -> bool:
    """Строки обрезаны по ширине (старые TXT-книги), а не по абзацам."""
    filled = [len(line.rstrip()) for line in lines if line.strip()]
    if len(filled) < 20:
        return False
    if max(filled) > 200:
        return False
    median = statistics.median(filled)
    if median < 40:
        return False
    close = sum(abs(length - median) < 12 for length in filled)
    return close / len(filled) > 0.5


def _paragraphs(text: str) -> list[tuple[str, int]]:
    """Возвращает абзацы и число пустых строк перед каждым."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    wrapped = _looks_hard_wrapped(lines)
    result: list[tuple[str, int]] = []
    current: list[str] = []
    blanks = 0
    pending = 0

    def flush() -> None:
        nonlocal current, pending
        if current:
            result.append((" ".join(current), pending))
            current = []
            pending = 0

    for line in lines:
        stripped = line.strip()
        if not stripped:
            flush()
            blanks += 1
            continue
        if not wrapped:
            flush()
            pending = blanks
            result.append((stripped, pending))
            pending = 0
            blanks = 0
            continue
        if blanks or (_INDENT_RE.match(line) and current):
            flush()
        if not current:
            pending = blanks
        blanks = 0
        if current and current[-1].endswith(("-", "­")) and stripped[:1].islower():
            current[-1] = current[-1].rstrip("-­") + stripped
        else:
            current.append(stripped)
    flush()
    return result


def read(path: Path, progress=None) -> Book:
    text = clean_text(decode_text(path.read_bytes())).lstrip("﻿")
    paragraphs = _paragraphs(text)
    if not paragraphs:
        raise ConversionError("Файл пустой: в нём нет текста.")
    book = Book()
    book.meta.title = path.stem
    blocks: list[Block] = [
        Gap() if _SEPARATOR_RE.match(item) else Para([Run(item)]) for item, _ in paragraphs
    ]
    titles = find_chapter_titles(blocks)
    # Строка, отделённая двумя и более пустыми строками, — тоже заголовок.
    spaced = [
        index
        for index, (item, before) in enumerate(paragraphs)
        if before >= 2
        and len(item) <= 100
        and not item.endswith((".", ",", ";"))
        and not _SEPARATOR_RE.match(item)
    ]
    if len(spaced) >= 2:
        for index in spaced:
            titles.setdefault(index, 2 if titles else 1)
        if len(set(titles.values())) == 1:
            titles = {index: 1 for index in titles}
    if titles:
        book.chapters = split_by_titles(blocks, titles, path.name)
        top = min(chapter.level for chapter in book.chapters)
        for chapter in book.chapters:
            chapter.level = chapter.level - top + 1
    else:
        book.chapters = [Chapter(blocks=blocks, source=path.name)]
    _lift_header(book)
    return book


def _lift_header(book: Book) -> None:
    """Короткие строки перед первой главой — обычно название и автор."""
    first = book.chapters[0]
    if len(book.chapters) < 2 or first.title or len(first.blocks) > 2:
        return
    texts = [plain_text(block.runs) for block in first.blocks if isinstance(block, Para)]
    if not texts or any(len(item) > 100 for item in texts):
        return
    book.meta.title = texts[0]
    if len(texts) > 1:
        book.meta.authors = [item.strip() for item in texts[1].split(",") if item.strip()]
    book.chapters.pop(0)


def _text(runs: list[Run], book: Book) -> str:
    parts = []
    for run in runs:
        if run.note:
            note = book.notes.get(run.note)
            if note:
                parts.append(f"[{note.label}]")
        else:
            parts.append(run.text)
    return "".join(parts)


def _blocks(blocks: list[Block], book: Book) -> list[str]:
    lines: list[str] = []
    for block in blocks:
        if isinstance(block, Para):
            lines.append(_text(block.runs, book))
        elif isinstance(block, ImageBlock):
            lines.append("[Изображение]")
        elif isinstance(block, Gap):
            lines.append("* * *")
        elif isinstance(block, ListBlock):
            for number, item in enumerate(block.items, 1):
                marker = f"{number}." if block.ordered else "•"
                lines.append(f"{marker} {_text(item, book)}")
        elif isinstance(block, TableBlock):
            for row in block.rows:
                lines.append("\t".join(_text(cell, book).replace("\n", " ") for cell in row))
    return lines


def write(source: Book, path: Path, options: Optional[dict] = None, progress=None) -> None:
    book = prepare_for_write(source)
    parts: list[str] = [book_title(book)]
    if book.meta.authors:
        parts.append(", ".join(book.meta.authors))
    text = "\n\n".join(parts)
    for chapter in book.chapters:
        section: list[str] = []
        if chapter.title:
            section.append(chapter.title)
        section.extend(_blocks(chapter.blocks, book))
        if section:
            text += "\n\n\n" + "\n\n".join(section)
    if book.notes:
        lines = [f"[{note.label}] " + " ".join(_blocks(note.blocks, book)) for note in book.notes.values()]
        text += "\n\n\nПримечания\n\n" + "\n\n".join(lines)
    path.write_text(text + "\n", encoding="utf-8")
