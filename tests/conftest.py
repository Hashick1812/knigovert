import base64
import io
import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from knigovert.model import Book, Chapter, Gap, ImageBlock, ListBlock, Meta, Note, Para, Run, TableBlock  # noqa: E402

TITLE = "Лето в Алматы"
AUTHOR = "Айгуль Касымова"
LONG = (
    "Это длинный абзац на русском языке, в котором встречаются разные слова, чтобы проверить перенос строк "
    "и сохранность текста при любом преобразовании файла из одного формата в другой. "
) * 4


def png_bytes(color=(180, 90, 40), size=(120, 80)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, "PNG")
    return buffer.getvalue()


def jpeg_bytes(color=(40, 90, 180), size=(300, 450)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, "JPEG")
    return buffer.getvalue()


def sample_book() -> Book:
    book = Book(meta=Meta(title=TITLE, authors=[AUTHOR], lang="ru", annotation="Короткая аннотация к книге."))
    picture = book.add_image(png_bytes())
    book.notes["n1"] = Note("n1", [Para([Run("Текст первой сноски.")])])
    book.notes["n2"] = Note("n2", [Para([Run("Текст второй сноски.")])])
    book.chapters = [
        Chapter(
            title="Глава 1. Начало",
            level=1,
            blocks=[
                Para([Run("Первый абзац с "), Run("жирным", bold=True), Run(" и "), Run("курсивом", italic=True), Run(" текстом")]),
                Para([Run(LONG)]),
                Para([Run("Абзац со сноской"), Run("", note="n1"), Run(" и продолжением.")]),
                ImageBlock(picture),
                Para([Run("Казахские буквы: әғқңөұүһі ӘҒҚҢӨҰҮҺІ.")]),
            ],
        ),
        Chapter(
            title="Глава 2. Продолжение",
            level=1,
            blocks=[
                Para([Run("Стихотворная строка первая")], "poem"),
                Para([Run("Стихотворная строка вторая")], "poem"),
                Gap(),
                Para([Run("Обычный текст после стиха."), Run("", note="n2")]),
                ListBlock([[Run("Первый пункт")], [Run("Второй пункт")]], ordered=False),
                TableBlock(
                    [[[Run("Город")], [Run("Население")]], [[Run("Алматы")], [Run("два миллиона")]]],
                    header=True,
                ),
            ],
        ),
        Chapter(
            title="Глава 3. Финал",
            level=1,
            blocks=[Para([Run("Символы для экранирования: <тег> & «кавычки» — конец.")]), Para([Run(LONG)])],
        ),
    ]
    return book


@pytest.fixture
def book() -> Book:
    return sample_book()


RAW_FB2 = """<?xml version="1.0" encoding="windows-1251"?>
<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0" xmlns:l="http://www.w3.org/1999/xlink">
<description><title-info>
  <genre>sf</genre>
  <author><first-name>Иван</first-name><middle-name>Петрович</middle-name><last-name>Сидоров</last-name></author>
  <book-title>Тестовая книга</book-title>
  <annotation><p>Описание книги.</p></annotation>
  <coverpage><image l:href="#cover.jpg"/></coverpage>
  <lang>ru</lang>
  <sequence name="Серия" number="2"/>
</title-info></description>
<body>
  <title><p>Тестовая книга</p></title>
  <epigraph><p>Эпиграф ко всей книге</p><text-author>Автор эпиграфа</text-author></epigraph>
  <section>
    <title><p>Часть первая</p></title>
    <section>
      <title><p>Глава 1</p><p>Название</p></title>
      <epigraph><p>Эпиграф к главе</p></epigraph>
      <p>Текст с <strong>жирным</strong>, <emphasis>курсивом</emphasis> и сноской<a l:href="#n1" type="note">[1]</a>.</p>
      <subtitle>Подзаголовок</subtitle>
      <empty-line/>
      <image l:href="#pic.png"/>
      <cite><p>Цитата</p><text-author>Автор цитаты</text-author></cite>
      <poem><title><p>Стих</p></title><stanza><v>Строка раз</v><v>Строка два</v></stanza><stanza><v>Строка три</v></stanza></poem>
      <table><tr><th>А</th><th>Б</th></tr><tr><td>1</td><td>2</td></tr></table>
      <p><a l:href="http://example.com/">Внешняя ссылка</a></p>
    </section>
    <section>
      <title><p>Глава 2</p></title>
      <p>Второй текст</p>
    </section>
  </section>
</body>
<body name="notes">
  <title><p>Примечания</p></title>
  <section id="n1"><title><p>1</p></title><p>Текст сноски</p></section>
</body>
<binary id="cover.jpg" content-type="image/jpeg">{cover}</binary>
<binary id="pic.png" content-type="image/png">{pic}</binary>
</FictionBook>
"""


@pytest.fixture
def raw_fb2(tmp_path) -> Path:
    path = tmp_path / "исходник.fb2"
    text = RAW_FB2.format(
        cover=base64.b64encode(jpeg_bytes()).decode(),
        pic=base64.b64encode(png_bytes()).decode(),
    )
    path.write_bytes(text.encode("cp1251"))
    return path
