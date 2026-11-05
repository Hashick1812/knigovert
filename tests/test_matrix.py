"""Каждый формат должен читаться из каждого и записываться в каждый."""

from __future__ import annotations

from dataclasses import replace

import pytest

from conftest import AUTHOR, TITLE, jpeg_bytes, sample_book
from knigovert.convert import OUTPUTS, read_book, write_book
from knigovert.model import ImageBlock, chapter_text

SOURCES = ["pdf", "docx", "fb2", "fb2.zip", "epub", "txt", "html", "md"]
TARGETS = list(OUTPUTS)
# Текст, который обязан выжить при любом преобразовании.
MUST_SURVIVE = ["Первый абзац", "жирным", "курсивом", "Казахские буквы: әғқңөұүһі", "Обычный текст после стиха"]
TITLES = ["Глава 1. Начало", "Глава 2. Продолжение", "Глава 3. Финал"]


@pytest.fixture(scope="module")
def sources(tmp_path_factory):
    folder = tmp_path_factory.mktemp("sources")
    result = {}
    for key in SOURCES:
        path = folder / f"book{OUTPUTS[key].extension}"
        write_book(sample_book(), path, key)
        result[key] = path
    return result


def full_text(book) -> str:
    text = "\n".join(chapter.title + "\n" + chapter_text(chapter) for chapter in book.chapters)
    return " ".join(text.split())


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("source", SOURCES)
def test_every_pair(sources, source, target, tmp_path):
    book = read_book(sources[source])
    output = tmp_path / f"out{OUTPUTS[target].extension}"
    write_book(book, output, target)
    assert output.stat().st_size > 100

    result = read_book(output)
    text = full_text(result)
    for fragment in MUST_SURVIVE:
        assert fragment in text, f"{source} → {target}: потерян текст «{fragment}»"
    assert "<тег> & «кавычки»" in text, f"{source} → {target}: испорчены спецсимволы"
    # В TXT границы глав определяются заново и обязаны совпасть.
    titles = [chapter.title for chapter in result.chapters]
    for title in TITLES:
        assert title in titles, f"{source} → {target}: нет главы «{title}», есть {titles}"


@pytest.mark.parametrize("source", SOURCES)
def test_read_keeps_chapters_and_title(sources, source):
    book = read_book(sources[source])
    titles = [chapter.title for chapter in book.chapters]
    # В PDF и TXT сноски — обычный текст в конце книги, поэтому там остаётся такая глава.
    expected = TITLES + ["Примечания"] if source in ("pdf", "txt") else TITLES
    assert titles == expected, f"{source}: {titles}"
    assert book.meta.title == TITLE, source
    assert book.meta.authors == [AUTHOR], source


@pytest.mark.parametrize("source", ["pdf", "docx", "fb2", "epub", "html", "md"])
def test_cover_survives_and_is_not_a_chapter(source, tmp_path):
    book = sample_book()
    book.meta.cover_id = book.add_image(jpeg_bytes())
    path = tmp_path / f"cover{OUTPUTS[source].extension}"
    write_book(book, path, source)
    result = read_book(path)
    assert result.meta.cover_id, f"{source}: обложка потеряна"
    assert result.chapters[0].title == TITLES[0], f"{source}: {[c.title for c in result.chapters]}"


@pytest.mark.parametrize("target", ["pdf", "docx", "fb2", "epub", "html", "md"])
def test_book_without_chapter_titles_stays_one_chapter(target, tmp_path):
    """Титульная страница не должна превращаться в отдельную главу."""
    book = sample_book()
    book.chapters = [replace(book.chapters[0], title="")]
    path = tmp_path / f"plain{OUTPUTS[target].extension}"
    write_book(book, path, target)
    result = read_book(path)
    # В PDF сноски остаются обычным текстом: это отдельная глава в конце.
    titles = [chapter.title for chapter in result.chapters if chapter.title != "Примечания"]
    assert titles == [""], f"{target}: {titles}"
    assert result.meta.title == TITLE, target


@pytest.mark.parametrize("source", ["pdf", "docx", "fb2", "fb2.zip", "epub", "html", "md"])
def test_images_survive(sources, source):
    book = read_book(sources[source])
    images = [block for chapter in book.chapters for block in chapter.blocks if isinstance(block, ImageBlock)]
    assert len(images) == 1, source
    assert images[0].image_id in book.images


@pytest.mark.parametrize("source", ["docx", "fb2", "epub", "html", "md"])
def test_notes_survive(sources, source):
    book = read_book(sources[source])
    assert len(book.notes) == 2, f"{source}: {list(book.notes)}"
    texts = [" ".join(block.runs[0].text for block in note.blocks) for note in book.notes.values()]
    assert any("первой сноски" in text for text in texts), f"{source}: {texts}"
    references = [
        run.note
        for chapter in book.chapters
        for block in chapter.blocks
        for run in getattr(block, "runs", [])
        if run.note
    ]
    assert len(references) == 2, source


@pytest.mark.parametrize("source", ["docx", "fb2", "epub", "html", "md"])
def test_bold_and_italic_survive(sources, source):
    book = read_book(sources[source])
    runs = book.chapters[0].blocks[0].runs
    assert any(run.bold and "жирным" in run.text for run in runs), f"{source}: {runs}"
    assert any(run.italic and "курсивом" in run.text for run in runs), f"{source}: {runs}"
