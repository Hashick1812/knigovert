"""Сборка книги из глав разных файлов.

Главы неизменяемы: объединение и разделение создают новые главы, а порядок и
названия хранит интерфейс. Так отмена действий получается простой и надёжной.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import replace
from typing import Iterable, Optional

from .model import (
    Block,
    Book,
    Chapter,
    ConversionError,
    ImageBlock,
    ImageData,
    Meta,
    Note,
    Para,
    Run,
    block_runs,
    word_count,
)


class Library:
    def __init__(self) -> None:
        self.chapters: dict[str, Chapter] = {}
        self.images: dict[str, ImageData] = {}
        self.notes: dict[str, Note] = {}
        self._lock = threading.Lock()

    def add_book(self, book: Book, split: bool = True, source_name: str = "") -> list[Chapter]:
        """Добавляет книгу. Сноски получают свой префикс, чтобы не пересекаться с другими файлами."""
        prefix = uuid.uuid4().hex[:8]
        rename = {note_id: f"{prefix}:{note_id}" for note_id in book.notes}

        def rewrite(blocks: Iterable[Block]) -> None:
            for block in blocks:
                for run in block_runs(block):
                    if run.note:
                        run.note = rename.get(run.note, run.note)

        for chapter in book.chapters:
            rewrite(chapter.blocks)
        renamed_notes = {}
        for note_id, note in book.notes.items():
            rewrite(note.blocks)
            renamed_notes[rename[note_id]] = Note(rename[note_id], note.blocks)

        chapters = book.chapters
        if not split and len(chapters) > 1:
            chapters = [
                _merged(chapters, title=book.meta.title or source_name, keep_titles=True, source=source_name)
            ]
        with self._lock:
            self.images.update(book.images)
            self.notes.update(renamed_notes)
            for chapter in chapters:
                self.chapters[chapter.id] = chapter
        return chapters

    def get(self, chapter_id: str) -> Chapter:
        chapter = self.chapters.get(chapter_id)
        if chapter is None:
            raise ConversionError("Глава не найдена. Добавьте файл ещё раз.")
        return chapter

    def add_cover(self, image: ImageData) -> str:
        self.images[image.id] = image
        return image.id

    def merge(
        self,
        chapter_ids: list[str],
        title: Optional[str] = None,
        keep_titles: bool = True,
        titles: Optional[list[str]] = None,
    ) -> Chapter:
        if len(chapter_ids) < 2:
            raise ConversionError("Для объединения выберите хотя бы две главы.")
        chapters = [self.get(chapter_id) for chapter_id in chapter_ids]
        # Названия мог изменить пользователь: они живут в интерфейсе, а не в исходных главах.
        if titles and len(titles) == len(chapters):
            chapters = [replace(chapter, title=str(name)) for chapter, name in zip(chapters, titles)]
        merged = _merged(
            chapters,
            title=chapters[0].title if title is None else title,
            keep_titles=keep_titles,
            source=chapters[0].source,
        )
        self.chapters[merged.id] = merged
        return merged

    def split(self, chapter_id: str, at_block: int, title: str = "") -> tuple[Chapter, Chapter]:
        chapter = self.get(chapter_id)
        if not 0 < at_block < len(chapter.blocks):
            raise ConversionError("Разделить можно только между абзацами главы.")
        first = Chapter(
            title=chapter.title, level=chapter.level, blocks=chapter.blocks[:at_block], source=chapter.source
        )
        second = Chapter(title=title, level=chapter.level, blocks=chapter.blocks[at_block:], source=chapter.source)
        self.chapters[first.id] = first
        self.chapters[second.id] = second
        return first, second

    def compose(self, meta: Meta, order: list[dict]) -> Book:
        """Собирает книгу в порядке, заданном интерфейсом."""
        chapters = []
        for item in order:
            source = self.get(item["id"])
            chapters.append(
                replace(
                    source,
                    title=str(item.get("title", source.title)),
                    level=int(item.get("level", source.level)),
                )
            )
        if not chapters:
            raise ConversionError("В книге нет ни одной главы.")
        return Book(meta=meta, chapters=chapters, images=dict(self.images), notes=dict(self.notes))

    def describe(self, chapter: Chapter) -> dict:
        return {
            "id": chapter.id,
            "title": chapter.title,
            "level": chapter.level,
            "source": chapter.source,
            "words": word_count(chapter),
            "images": sum(isinstance(block, ImageBlock) for block in chapter.blocks),
            "blocks": len(chapter.blocks),
            "pdf_pages": bool(chapter.pdf_pages),
        }


def _merged(chapters: list[Chapter], title: str, keep_titles: bool, source: str) -> Chapter:
    blocks: list[Block] = []
    pdf_pages = []
    keep_pdf = all(chapter.pdf_pages for chapter in chapters)
    for chapter in chapters:
        if keep_titles and chapter.title and (blocks or chapter.title != title):
            # Название присоединённой главы остаётся подзаголовком внутри текста.
            blocks.append(Para([Run(chapter.title)], "subtitle"))
        blocks.extend(chapter.blocks)
        if keep_pdf:
            pdf_pages.extend(chapter.pdf_pages)
    return Chapter(title=title, level=chapters[0].level, blocks=blocks, source=source, pdf_pages=pdf_pages)
