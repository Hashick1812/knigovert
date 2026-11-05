"""Особенности отдельных форматов на файлах, созданных не нашей программой."""

from __future__ import annotations

import zipfile

import docx
import pymupdf
import pytest
from lxml import etree

from conftest import LONG, jpeg_bytes, png_bytes, sample_book
from knigovert.convert import detect_format, read_book, safe_filename, stem_of, unique_path, write_book
from knigovert.formats import fb2 as fb2_format
from knigovert.model import ConversionError, Gap, ImageBlock, ListBlock, Para, Run, TableBlock, plain_text


# --- FB2 ---


def test_fb2_foreign_file_cp1251(raw_fb2):
    book = read_book(raw_fb2)
    assert book.meta.title == "Тестовая книга"
    assert book.meta.authors == ["Иван Петрович Сидоров"]
    assert book.meta.series == "Серия"
    assert book.meta.cover_id in book.images
    titles = [(chapter.title, chapter.level) for chapter in book.chapters]
    assert ("Часть первая", 1) in titles
    assert ("Глава 1. Название", 2) in titles
    assert ("Глава 2", 2) in titles
    chapter = next(item for item in book.chapters if item.title == "Глава 1. Название")
    kinds = {block.kind for block in chapter.blocks if isinstance(block, Para)}
    assert {"epigraph", "subtitle", "quote", "author", "poem", "normal"} <= kinds
    assert any(isinstance(block, ImageBlock) for block in chapter.blocks)
    assert any(isinstance(block, TableBlock) for block in chapter.blocks)
    assert any(isinstance(block, Gap) for block in chapter.blocks)
    first = next(block for block in chapter.blocks if isinstance(block, Para) and block.kind == "normal")
    assert any(run.note for run in first.runs)
    link = [run for block in chapter.blocks if isinstance(block, Para) for run in block.runs if run.href]
    assert link and link[0].href == "http://example.com/"
    assert any("Текст сноски" in plain_text(note.blocks[0].runs) for note in book.notes.values())


def test_fb2_output_follows_schema(tmp_path, book):
    """Секция содержит либо текст, либо подсекции — иначе читалки ругаются."""
    book.chapters[1].level = 2
    book.chapters[2].level = 2
    path = tmp_path / "out.fb2"
    write_book(book, path, "fb2")
    root = etree.parse(str(path)).getroot()
    ns = {"f": fb2_format.FB_NS}
    assert root.tag == f"{{{fb2_format.FB_NS}}}FictionBook"
    for section in root.iterfind(".//f:section", ns):
        tags = [etree.QName(child).localname for child in section]
        if "section" in tags:
            assert set(tags) <= {"title", "epigraph", "image", "annotation", "section"}, tags
    binaries = root.findall("f:binary", ns)
    assert binaries
    hrefs = {
        value.lstrip("#")
        for element in root.iter()
        for name, value in element.attrib.items()
        if name.endswith("}href") and value.startswith("#")
    }
    ids = {element.get("id") for element in root.iter() if element.get("id")}
    assert hrefs <= ids, hrefs - ids


def test_fb2_zip_with_wrong_extension(tmp_path, raw_fb2):
    path = tmp_path / "book.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.write(raw_fb2, "внутри.fb2")
    assert detect_format(path) == "fb2"
    assert read_book(path).meta.title == "Тестовая книга"


def test_fb2_broken_markup_is_recovered(tmp_path):
    path = tmp_path / "broken.fb2"
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?><FictionBook><description><title-info><book-title>Сломанная</book-title>'
        "</title-info></description><body><section><title><p>Глава</p></title><p>Текст <strong>без закрытия</p>"
        "<p>Второй</p></section></body></FictionBook>",
        encoding="utf-8",
    )
    book = read_book(path)
    assert book.meta.title == "Сломанная"
    assert "Текст" in plain_text(book.chapters[0].blocks[0].runs)


def test_fb2_does_not_resolve_external_entities(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("ТАЙНА", encoding="utf-8")
    path = tmp_path / "xxe.fb2"
    path.write_text(
        f'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "{secret.as_uri()}">]>'
        "<FictionBook><body><section><p>начало &e; конец</p></section></body></FictionBook>",
        encoding="utf-8",
    )
    book = read_book(path)
    assert "ТАЙНА" not in plain_text(book.chapters[0].blocks[0].runs)


# --- DOCX ---


def make_docx(path, builder):
    document = docx.Document()
    builder(document)
    document.save(path)
    return path


def test_docx_russian_style_names(tmp_path):
    """В русском Word стили называются «Заголовок 1», а не Heading 1."""

    def build(document):
        heading = document.styles["Heading 1"]
        document.add_paragraph("Первая часть", style=heading)
        document.add_paragraph("Текст первой главы.")
        document.add_paragraph("Вторая часть", style=heading)
        document.add_paragraph("Текст второй главы.")
        name = heading.element.xpath("w:name")[0]
        name.set("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val", "Заголовок 1")
        # Без наследуемого уровня структуры стиль опознаётся только по русскому имени.
        for outline in heading.element.xpath("w:pPr/w:outlineLvl"):
            outline.getparent().remove(outline)

    book = read_book(make_docx(tmp_path / "ru.docx", build))
    assert [chapter.title for chapter in book.chapters] == ["Первая часть", "Вторая часть"]


def test_docx_without_styles_finds_chapters_by_text(tmp_path):
    def build(document):
        for title in ("Глава 1", "Глава 2", "Глава третья. Финал"):
            document.add_paragraph(title)
            document.add_paragraph(f"Текст: {title} продолжается здесь.")

    book = read_book(make_docx(tmp_path / "plain.docx", build))
    assert [chapter.title for chapter in book.chapters] == ["Глава 1", "Глава 2", "Глава третья. Финал"]


def test_docx_lists_tables_images_links(tmp_path):
    def build(document):
        document.add_paragraph("Заголовок документа", style="Title")
        document.add_paragraph("Раздел", style="Heading 1")
        document.add_paragraph("Пункт А", style="List Bullet")
        document.add_paragraph("Пункт Б", style="List Bullet")
        document.add_paragraph("Номер один", style="List Number")
        table = document.add_table(rows=2, cols=2)
        table.cell(0, 0).text = "ячейка 1"
        table.cell(1, 1).text = "ячейка 4"
        import io

        document.add_picture(io.BytesIO(jpeg_bytes()))

    book = read_book(make_docx(tmp_path / "rich.docx", build))
    assert book.meta.title == "Заголовок документа"
    blocks = book.chapters[0].blocks
    lists = [block for block in blocks if isinstance(block, ListBlock)]
    assert [item[0].text for item in lists[0].items] == ["Пункт А", "Пункт Б"]
    assert lists[0].ordered is False and lists[1].ordered is True
    table = next(block for block in blocks if isinstance(block, TableBlock))
    assert table.rows[0][0][0].text == "ячейка 1"
    assert any(isinstance(block, ImageBlock) for block in blocks)


def test_docx_output_has_real_footnotes_and_opens(tmp_path, book):
    path = tmp_path / "out.docx"
    write_book(book, path, "docx")
    with zipfile.ZipFile(path) as archive:
        assert "word/footnotes.xml" in archive.namelist()
        footnotes = archive.read("word/footnotes.xml").decode("utf-8")
        assert "Текст первой сноски." in footnotes
        types = archive.read("[Content_Types].xml").decode("utf-8")
        assert "footnotes+xml" in types
        document_xml = archive.read("word/document.xml").decode("utf-8")
        assert "w:footnoteReference" in document_xml
        assert "w:fldChar" in document_xml
    reopened = docx.Document(str(path))
    assert any(paragraph.style.name == "Heading 1" for paragraph in reopened.paragraphs)


def test_old_doc_format_gets_clear_message(tmp_path):
    path = tmp_path / "old.doc"
    path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 100)
    with pytest.raises(ConversionError, match="docx"):
        read_book(path)


# --- PDF ---


def foreign_pdf(path, pages, toc=None):
    """PDF, созданный другой библиотекой, с колонтитулами и номерами страниц."""
    document = pymupdf.open()
    for number, content in enumerate(pages, 1):
        page = document.new_page(width=595, height=842)
        writer = pymupdf.TextWriter(page.rect)
        font = pymupdf.Font("cour")
        writer.append((280, 40), "HEADER", font=font, fontsize=9)
        writer.append((290, 810), str(number), font=font, fontsize=9)
        y = 90
        for text, size in content:
            writer.append((72, y), text, font=font, fontsize=size)
            y += size * 1.6
        writer.write_text(page)
    if toc:
        document.set_toc(toc)
    document.save(path)
    document.close()
    return path


def test_pdf_removes_headers_and_page_numbers(tmp_path):
    pages = [[("Chapter 1", 20), ("Body text one.", 11)], [("Body text two.", 11)], [("Chapter 2", 20), ("Body text three.", 11)], [("Body text four.", 11)]]
    book = read_book(foreign_pdf(tmp_path / "a.pdf", pages))
    text = "\n".join(plain_text(block.runs) for chapter in book.chapters for block in chapter.blocks if isinstance(block, Para))
    assert "HEADER" not in text
    assert [chapter.title for chapter in book.chapters] == ["Chapter 1", "Chapter 2"]
    assert "Body text three." in text


def test_pdf_short_file_has_no_page_numbers_in_text(tmp_path):
    pages = [[("Body text one.", 11)], [("Body text two.", 11)]]
    book = read_book(foreign_pdf(tmp_path / "short.pdf", pages))
    texts = [plain_text(block.runs) for chapter in book.chapters for block in chapter.blocks if isinstance(block, Para)]
    assert not any(text.strip().isdigit() for text in texts), texts
    assert "Body text one." in " ".join(texts)


def test_pdf_keeps_number_that_is_not_a_page_number(tmp_path):
    pages = [[("Body text one.", 11)], [("Body text two.", 11), ("1984", 11)]]
    book = read_book(foreign_pdf(tmp_path / "year.pdf", pages))
    texts = " ".join(plain_text(block.runs) for chapter in book.chapters for block in chapter.blocks if isinstance(block, Para))
    assert "1984" in texts


def test_pdf_uses_bookmarks_for_chapters(tmp_path):
    pages = [[("Intro line here.", 11)], [("Alpha", 11), ("Text of alpha.", 11)], [("Beta", 11), ("Text of beta.", 11)]]
    toc = [[1, "Alpha", 2], [1, "Beta", 3]]
    book = read_book(foreign_pdf(tmp_path / "b.pdf", pages, toc))
    assert [chapter.title for chapter in book.chapters] == ["", "Alpha", "Beta"]


def test_pdf_scan_becomes_images_with_warning(tmp_path):
    document = pymupdf.open()
    for _ in range(2):
        page = document.new_page(width=300, height=400)
        page.insert_image(page.rect, stream=jpeg_bytes())
    path = tmp_path / "scan.pdf"
    document.save(path)
    document.close()
    book = read_book(path)
    assert any("скан" in warning for warning in book.warnings)
    images = [block for chapter in book.chapters for block in chapter.blocks if isinstance(block, ImageBlock)]
    assert len(images) == 2


def test_pdf_with_password_gets_clear_message(tmp_path):
    document = pymupdf.open()
    document.new_page().insert_text((50, 50), "secret")
    path = tmp_path / "locked.pdf"
    document.save(path, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="1", owner_pw="2")
    document.close()
    with pytest.raises(ConversionError, match="парол"):
        read_book(path)


def test_pdf_joins_hyphenated_words(tmp_path):
    book = sample_book()
    path = tmp_path / "h.pdf"
    write_book(book, path, "pdf", {"page_size": "B6"})
    result = read_book(path)
    text = " ".join(plain_text(block.runs) for chapter in result.chapters for block in chapter.blocks if isinstance(block, Para))
    # Переносы в вёрстке не должны оставаться внутри слов.
    assert "преобразовании файла" in text
    assert "- " not in LONG and " - " not in text.replace("—", "")


def test_pdf_output_has_bookmarks_toc_and_cyrillic(tmp_path, book):
    book.meta.cover_id = book.add_image(jpeg_bytes())
    path = tmp_path / "out.pdf"
    write_book(book, path, "pdf")
    with pymupdf.open(path) as document:
        toc = document.get_toc()
        assert [entry[1] for entry in toc][:3] == ["Глава 1. Начало", "Глава 2. Продолжение", "Глава 3. Финал"]
        assert toc[-1][1] == "Примечания"
        text = "\n".join(page.get_text() for page in document)
        assert "Оглавление" in text and "әғқңөұүһі" in text
        fonts = {font[3] for page in document for font in page.get_fonts()}
        assert any("KvSerif" in name or "PT" in name for name in fonts)
        assert document.metadata["title"] == book.meta.title
        # Весь текст набран встроенными шрифтами: файл читается на компьютере без них.
        used = {
            span["font"]
            for page in document
            for block in page.get_text("dict")["blocks"]
            for line in block.get("lines", [])
            for span in line["spans"]
        }
        embedded = {font[3].split("+")[-1] for page in document for font in page.get_fonts() if font[1] != "n/a"}
        assert used and used <= embedded, used - embedded


def test_pdf_does_not_fail_on_huge_image_and_long_word(tmp_path, book):
    big = book.add_image(png_bytes(size=(4000, 6000)))
    book.chapters[0].blocks.insert(0, ImageBlock(big))
    book.chapters[0].blocks.append(Para([Run("оченьдлинноеслово" * 60)]))
    write_book(book, tmp_path / "big.pdf", "pdf", {"page_size": "B6"})


def test_pdf_chinese_and_symbols_do_not_crash(tmp_path, book):
    book.chapters[0].blocks.append(Para([Run("中文文本 αβγ ₸ 😀 שלום")]))
    write_book(book, tmp_path / "cjk.pdf", "pdf")
    assert (tmp_path / "cjk.pdf").stat().st_size > 1000


# --- Прочее ---


def test_txt_cp1251_and_chapters(tmp_path):
    path = tmp_path / "book.txt"
    path.write_bytes("Глава 1\n\nТекст первой главы.\n\nГлава 2\n\nТекст второй главы.\n".encode("cp1251"))
    book = read_book(path)
    assert [chapter.title for chapter in book.chapters] == ["Глава 1", "Глава 2"]
    assert plain_text(book.chapters[0].blocks[0].runs) == "Текст первой главы."


@pytest.mark.parametrize("encoding", ["cp1251", "koi8_r", "cp866"])
def test_txt_russian_encodings(tmp_path, encoding):
    original = "Привет, это обычный русский текст для проверки кодировки файла."
    path = tmp_path / "book.txt"
    path.write_bytes(original.encode(encoding))
    assert plain_text(read_book(path).chapters[0].blocks[0].runs) == original


def test_txt_utf16(tmp_path):
    path = tmp_path / "book.txt"
    path.write_bytes("Привет, мир".encode("utf-16"))
    assert plain_text(read_book(path).chapters[0].blocks[0].runs) == "Привет, мир"


def test_txt_hard_wrapped_lines_are_joined(tmp_path):
    line = "Это строка обрезанная по ширине как в старых книгах такого вида\n"
    path = tmp_path / "wrap.txt"
    path.write_text((line * 3 + "\n") * 10, encoding="utf-8")
    book = read_book(path)
    assert len(book.chapters[0].blocks) == 10


def test_prose_is_not_a_chapter(tmp_path):
    path = tmp_path / "prose.txt"
    path.write_text("Часть людей ушла домой.\n\nГлава семьи вернулась поздно вечером.\n", encoding="utf-8")
    book = read_book(path)
    assert len(book.chapters) == 1 and not book.chapters[0].title


def test_html_foreign_with_scripts_and_external_images(tmp_path):
    path = tmp_path / "page.html"
    path.write_text(
        '<html><head><title>Страница</title><script>alert(1)</script></head><body>'
        '<h1>Первый</h1><p onclick="x()">Текст <b>жирный</b></p><img src="http://example.com/a.png">'
        '<h1>Второй</h1><ul><li>один</li><li>два</li></ul><table><tr><td>я</td></tr></table></body></html>',
        encoding="utf-8",
    )
    book = read_book(path)
    assert [chapter.title for chapter in book.chapters] == ["Первый", "Второй"]
    assert any("интернет" in warning for warning in book.warnings)
    text = plain_text(book.chapters[0].blocks[0].runs)
    assert text == "Текст жирный" and "alert" not in text


def test_html_cannot_read_files_outside_its_folder(tmp_path):
    outside = tmp_path / "secret.png"
    outside.write_bytes(png_bytes())
    folder = tmp_path / "book"
    folder.mkdir()
    path = folder / "page.html"
    path.write_text('<p>Текст</p><img src="../secret.png">', encoding="utf-8")
    book = read_book(path)
    assert not book.images


def test_markdown_foreign(tmp_path):
    path = tmp_path / "notes.md"
    path.write_text(
        "# Первая\n\nТекст **жирный** и *курсив*[^a].\n\n- раз\n- два\n\n## Вложенная\n\n| x | y |\n|---|---|\n| 1 | 2 |\n\n```\nкод  тут\n```\n\n[^a]: Сноска.\n",
        encoding="utf-8",
    )
    book = read_book(path)
    assert [(chapter.title, chapter.level) for chapter in book.chapters] == [("Вложенная", 1)] or [
        chapter.title for chapter in book.chapters
    ] == ["Первая", "Вложенная"]
    assert len(book.notes) == 1


def test_detect_format_by_content_not_extension(tmp_path, raw_fb2):
    wrong = tmp_path / "book.txt"
    wrong.write_bytes(raw_fb2.read_bytes())
    assert detect_format(wrong) == "fb2"
    renamed = tmp_path / "noext"
    renamed.write_bytes(raw_fb2.read_bytes())
    assert detect_format(renamed) == "fb2"
    pdf = tmp_path / "really.fb2"
    pdf.write_bytes(b"%PDF-1.7\n")
    assert detect_format(pdf) == "pdf"
    for name in ("broken.pdf", "broken.docx", "broken.epub"):
        fake = tmp_path / name
        fake.write_bytes(b"not a real file")
        with pytest.raises(ConversionError):
            detect_format(fake)


def test_file_names():
    assert stem_of("Книга.fb2.zip") == "Книга"
    assert stem_of("C:\\папка\\Книга.docx") == "Книга"
    assert safe_filename('a/b\\c:d*e?"<>|') == "a b c d e"
    assert safe_filename("CON") == "книга"
    assert safe_filename("...") == "книга"
    assert safe_filename("../../etc/passwd") == "etc passwd"


def test_unique_path_never_overwrites(tmp_path):
    (tmp_path / "a.pdf").write_text("x")
    (tmp_path / "a (1).pdf").write_text("x")
    assert unique_path(tmp_path, "a", ".pdf").name == "a (2).pdf"
    assert unique_path(tmp_path, "b", ".pdf").name == "b.pdf"
