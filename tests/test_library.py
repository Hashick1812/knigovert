"""Сборка книги из глав разных файлов."""

from __future__ import annotations

import pymupdf
import pytest

from conftest import sample_book
from knigovert.convert import read_book, write_book
from knigovert.formats.pdf_out import merge_pages
from knigovert.library import Library
from knigovert.model import Book, Chapter, ConversionError, Meta, Note, Para, Run, chapter_text, plain_text


def second_book() -> Book:
    book = Book(meta=Meta(title="Вторая"))
    # Намеренно тот же id сноски, что и в первой книге.
    book.notes["n1"] = Note("n1", [Para([Run("Сноска второй книги.")])])
    book.chapters = [
        Chapter(title="Рассказ", blocks=[Para([Run("Текст рассказа"), Run("", note="n1")])]),
        Chapter(title="Послесловие", blocks=[Para([Run("Текст послесловия")])]),
    ]
    return book


def order(chapters):
    return [{"id": chapter.id, "title": chapter.title, "level": chapter.level} for chapter in chapters]


def test_merge_files_keeps_order_and_separates_notes(tmp_path):
    library = Library()
    first = library.add_book(sample_book())
    second = library.add_book(second_book())
    assert len(first) == 3 and len(second) == 2
    # Глава второй книги идёт между главами первой.
    arrangement = [first[0], second[0], first[1], first[2], second[1]]
    book = library.compose(Meta(title="Сборник"), order(arrangement))
    path = tmp_path / "s.fb2"
    write_book(book, path, "fb2")
    result = read_book(path)
    assert [chapter.title for chapter in result.chapters] == [chapter.title for chapter in arrangement]
    texts = [plain_text(note.blocks[0].runs) for note in result.notes.values()]
    assert "Текст первой сноски." in texts and "Сноска второй книги." in texts


def test_reorder_rename_and_change_level(tmp_path):
    library = Library()
    chapters = library.add_book(sample_book())
    plan = [
        {"id": chapters[2].id, "title": "Новое имя", "level": 1},
        {"id": chapters[0].id, "title": chapters[0].title, "level": 2},
    ]
    path = tmp_path / "r.fb2"
    write_book(library.compose(Meta(title="Книга"), plan), path, "fb2")
    result = read_book(path)
    assert [(chapter.title, chapter.level) for chapter in result.chapters] == [("Новое имя", 1), ("Глава 1. Начало", 2)]
    assert "Финал" not in " ".join(chapter.title for chapter in result.chapters)


def test_source_chapter_is_not_changed_by_rename():
    library = Library()
    chapters = library.add_book(sample_book())
    library.compose(Meta(), [{"id": chapters[0].id, "title": "Другое", "level": 3}])
    assert library.get(chapters[0].id).title == "Глава 1. Начало"
    assert library.get(chapters[0].id).level == 1


def test_merge_chapters_keeps_titles_as_subtitles_and_originals():
    library = Library()
    chapters = library.add_book(sample_book())
    merged = library.merge([chapters[0].id, chapters[1].id])
    assert merged.title == "Глава 1. Начало"
    subtitles = [plain_text(block.runs) for block in merged.blocks if isinstance(block, Para) and block.kind == "subtitle"]
    assert subtitles == ["Глава 2. Продолжение"]
    assert "Обычный текст после стиха" in chapter_text(merged)
    # Исходные главы остаются: на этом держится отмена действия.
    assert library.get(chapters[0].id).blocks is not merged.blocks
    assert chapters[1].id in library.chapters


def test_merge_chapters_without_titles():
    library = Library()
    chapters = library.add_book(sample_book())
    merged = library.merge([chapters[0].id, chapters[1].id], title="Новая", keep_titles=False)
    assert merged.title == "Новая"
    assert not [block for block in merged.blocks if isinstance(block, Para) and block.kind == "subtitle"]


def test_merge_needs_two():
    library = Library()
    chapters = library.add_book(sample_book())
    with pytest.raises(ConversionError):
        library.merge([chapters[0].id])


def test_split_chapter():
    library = Library()
    chapter = library.add_book(sample_book())[0]
    total = len(chapter.blocks)
    first, second = library.split(chapter.id, 2, "Вторая половина")
    assert len(first.blocks) == 2 and len(second.blocks) == total - 2
    assert first.title == chapter.title and second.title == "Вторая половина"
    assert len(library.get(chapter.id).blocks) == total


@pytest.mark.parametrize("position", [0, 5, 999, -1])
def test_split_bad_position(position):
    library = Library()
    chapter = library.add_book(sample_book())[0]
    with pytest.raises(ConversionError):
        library.split(chapter.id, position)


def test_add_file_as_one_chapter():
    library = Library()
    chapters = library.add_book(sample_book(), split=False)
    assert len(chapters) == 1
    assert chapters[0].title == "Лето в Алматы"
    text = chapter_text(chapters[0])
    assert "Глава 1. Начало" in text and "Глава 3. Финал" in text


def test_compose_empty_is_an_error():
    with pytest.raises(ConversionError):
        Library().compose(Meta(), [])


def test_unused_files_do_not_leak_into_result(tmp_path):
    """Сноски и картинки удалённых глав не должны попасть в итоговый файл."""
    library = Library()
    first = library.add_book(sample_book())
    book = library.compose(Meta(title="Только третья"), order([first[2]]))
    path = tmp_path / "u.fb2"
    write_book(book, path, "fb2")
    content = path.read_text(encoding="utf-8")
    assert "<binary" not in content
    assert "сноски" not in content


def pdf_file(path, count, label):
    document = pymupdf.open()
    for number in range(count):
        page = document.new_page()
        page.insert_text((60, 300), f"{label} page {number}")
    document.save(path)
    document.close()
    return path


def test_merge_pdf_pages_without_retypesetting(tmp_path):
    first = pdf_file(tmp_path / "a.pdf", 2, "AAA")
    second = pdf_file(tmp_path / "b.pdf", 3, "BBB")
    library = Library()
    chapters = library.add_book(read_book(first), split=False) + library.add_book(read_book(second), split=False)
    plan = [
        {"id": chapters[1].id, "title": "Вторая", "level": 1},
        {"id": chapters[0].id, "title": "Первая", "level": 1},
    ]
    output = tmp_path / "merged.pdf"
    merge_pages(library.compose(Meta(title="Склейка"), plan), output)
    with pymupdf.open(output) as document:
        assert document.page_count == 5
        assert "BBB page 0" in document[0].get_text()
        assert "AAA page 0" in document[3].get_text()
        assert document.get_toc() == [[1, "Вторая", 1], [1, "Первая", 4]]
        assert document.metadata["title"] == "Склейка"
