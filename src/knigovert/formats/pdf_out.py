"""Запись PDF: книжная вёрстка, оглавление, закладки, сноски, переносы."""

from __future__ import annotations

import io
import os
import re
from pathlib import Path
from typing import Iterable, Optional
from xml.sax.saxutils import escape, quoteattr

import pymupdf
import pyphen
from reportlab import rl_config
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4, A5, B6, LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.pdfmetrics import registerFontFamily
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate,
    CondPageBreak,
    Frame,
    Image,
    NextPageTemplate,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.platypus.tableofcontents import TableOfContents

from .. import APP_NAME
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
from ..paths import FONTS_DIR

PAGE_SIZES = {"A4": A4, "A5": A5, "B6": B6, "Letter": LETTER}
MARGINS = {"A4": 2.3 * cm, "A5": 1.7 * cm, "B6": 1.4 * cm, "Letter": 2.3 * cm}
INK = colors.HexColor("#1d1b18")
SOFT_INK = colors.HexColor("#5c554c")
LINK = colors.HexColor("#1a4e9b")

_FONT_FILES = {
    "KvSerif": "PT_Serif-Web-Regular.ttf",
    "KvSerif-Bold": "PT_Serif-Web-Bold.ttf",
    "KvSerif-Italic": "PT_Serif-Web-Italic.ttf",
    "KvSerif-BoldItalic": "PT_Serif-Web-BoldItalic.ttf",
    "KvMono": "PTM55FT.ttf",
    "KvFallback": "DejaVuSans.ttf",
    "KvFallback-Bold": "DejaVuSans-Bold.ttf",
}
# Шрифты Windows для иероглифов: у них нет смысла занимать место в программе.
_CJK_CANDIDATES = [
    ("msyh.ttc", 0),
    ("simsun.ttc", 0),
    ("malgun.ttf", 0),
    ("YuGothR.ttc", 0),
    ("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc", 0),
    ("/System/Library/Fonts/PingFang.ttc", 0),
]

_registered = False
_cmap: dict[str, frozenset[int]] = {}
_cjk_font: Optional[str] = None
_cjk_checked = False


def _register_fonts() -> None:
    global _registered
    if _registered:
        return
    for name, filename in _FONT_FILES.items():
        font = TTFont(name, str(FONTS_DIR / filename))
        pdfmetrics.registerFont(font)
        _cmap[name] = frozenset(font.face.charToGlyph)
    registerFontFamily(
        "KvSerif", normal="KvSerif", bold="KvSerif-Bold", italic="KvSerif-Italic", boldItalic="KvSerif-BoldItalic"
    )
    registerFontFamily(
        "KvFallback", normal="KvFallback", bold="KvFallback-Bold", italic="KvFallback", boldItalic="KvFallback-Bold"
    )
    registerFontFamily("KvMono", normal="KvMono", bold="KvMono", italic="KvMono", boldItalic="KvMono")
    _registered = True


def _system_cjk_font() -> Optional[str]:
    """Подключает системный шрифт с иероглифами, только если он нужен."""
    global _cjk_font, _cjk_checked
    if _cjk_checked:
        return _cjk_font
    _cjk_checked = True
    windows_fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    for filename, index in _CJK_CANDIDATES:
        candidate = Path(filename) if filename.startswith("/") else windows_fonts / filename
        if not candidate.is_file():
            continue
        try:
            font = TTFont("KvCJK", str(candidate), subfontIndex=index)
            pdfmetrics.registerFont(font)
            _cmap["KvCJK"] = frozenset(font.face.charToGlyph)
            registerFontFamily("KvCJK", normal="KvCJK", bold="KvCJK", italic="KvCJK", boldItalic="KvCJK")
            _cjk_font = "KvCJK"
            return _cjk_font
        except Exception:
            continue
    return None


class _Markup:
    """Превращает куски текста в разметку абзаца, подбирая шрифт под символы."""

    def __init__(self, book: Book):
        self.book = book
        self.main = _cmap["KvSerif"]
        self.fallback = _cmap["KvFallback"]
        self.mono = _cmap["KvMono"]
        self.missing = False

    def _font_for(self, char: str, mono: bool) -> Optional[str]:
        code = ord(char)
        if code in (self.mono if mono else self.main):
            return None
        if code in self.fallback:
            return "KvFallback"
        if code >= 0x2E80:
            cjk = _system_cjk_font()
            if cjk and code in _cmap[cjk]:
                return cjk
        self.missing = True
        return None

    def text(self, value: str, mono: bool = False) -> str:
        value = clean_text(value)
        table = self.mono if mono else self.main
        if all(ord(char) in table or char in "\n\t" for char in value):
            return _escape(value)
        parts: list[str] = []
        buffer: list[str] = []
        current: Optional[str] = None
        for char in value:
            font = None if char in "\n\t" else self._font_for(char, mono)
            if font != current and buffer:
                parts.append(self._wrap("".join(buffer), current))
                buffer = []
            current = font
            buffer.append(char)
        if buffer:
            parts.append(self._wrap("".join(buffer), current))
        return "".join(parts)

    @staticmethod
    def _wrap(text: str, font: Optional[str]) -> str:
        escaped = _escape(text)
        return f'<font name="{font}">{escaped}</font>' if font else escaped

    def runs(self, runs: Iterable[Run], note_links: bool = True) -> str:
        parts: list[str] = []
        for run in runs:
            if run.note:
                note = self.book.notes.get(run.note)
                if note is None:
                    continue
                label = escape(note.label)
                if note_links:
                    parts.append(f'<a name="ref{label}"/><a href="#note{label}" color="#1a4e9b"><super>{label}</super></a>')
                else:
                    parts.append(f"<super>{label}</super>")
                continue
            markup = self.text(run.text, run.code)
            if run.code:
                markup = f'<font name="KvMono">{markup}</font>'
            if run.sub:
                markup = f"<sub>{markup}</sub>"
            if run.sup:
                markup = f"<super>{markup}</super>"
            if run.strike:
                markup = f"<strike>{markup}</strike>"
            if run.italic:
                markup = f"<i>{markup}</i>"
            if run.bold:
                markup = f"<b>{markup}</b>"
            if run.href and re.match(r"(?i)^(https?://|mailto:)", run.href):
                markup = f'<a href={quoteattr(run.href)} color="#1a4e9b"><u>{markup}</u></a>'
            parts.append(markup)
        return "".join(parts)


def _escape(text: str) -> str:
    return escape(text).replace("\n", "<br/>").replace("\t", "&nbsp;&nbsp;&nbsp;&nbsp;")


class _Heading(Paragraph):
    """Заголовок главы: регистрирует запись в оглавлении и закладку."""

    def __init__(self, text, style, entry_title: str, level: int, key: str):
        super().__init__(text, style)
        self.entry_markup = text
        self.entry_title = entry_title
        self.entry_level = level
        self.entry_key = key


class _BookDocument(BaseDocTemplate):
    def __init__(self, filename, footer: bool, report=None, **kwargs):
        super().__init__(filename, **kwargs)
        self.outline_level = -1
        self.report = report
        frame = Frame(
            self.leftMargin, self.bottomMargin, self.width, self.height, id="main",
            leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0,
        )
        self.addPageTemplates(
            [
                PageTemplate(id="front", frames=[frame]),
                PageTemplate(id="body", frames=[frame], onPage=self._page_number if footer else None),
            ]
        )

    def _page_number(self, canvas, doc):
        canvas.saveState()
        canvas.setFont("KvSerif", 9)
        canvas.setFillColor(SOFT_INK)
        canvas.drawCentredString(self.pagesize[0] / 2, self.bottomMargin * 0.5, str(doc.page))
        canvas.restoreState()

    def beforeDocument(self):
        # Вёрстка идёт в несколько проходов, и на каждом закладки строятся заново.
        self.outline_level = -1

    def afterPage(self):
        if self.report and self.page % 10 == 0:
            self.report(f"Верстаю книгу, страница {self.page}", None)

    def afterFlowable(self, flowable):
        if isinstance(flowable, _Heading):
            # Глава без названия не даёт закладки, поэтому уровень нельзя перескочить.
            level = min(flowable.entry_level - 1, self.outline_level + 1)
            self.outline_level = level
            self.canv.bookmarkPage(flowable.entry_key)
            self.canv.addOutlineEntry(flowable.entry_title, flowable.entry_key, level, False)
            self.notify("TOCEntry", (level, flowable.entry_markup, self.page, flowable.entry_key))


def _hyphenation(lang: str) -> Optional[str]:
    code = (lang or "ru").lower()[:2]
    for candidate in (f"{code}_{code.upper()}", code, {"en": "en_US", "ru": "ru_RU"}.get(code, "")):
        if candidate and candidate in pyphen.LANGUAGES:
            return candidate
    return None


def _styles(size: float, lang: str) -> dict[str, ParagraphStyle]:
    hyphen = _hyphenation(lang)
    leading = size * 1.38
    body = ParagraphStyle(
        "body", fontName="KvSerif", fontSize=size, leading=leading, alignment=TA_JUSTIFY, textColor=INK,
        firstLineIndent=size * 1.4, spaceAfter=size * 0.15, allowWidows=0, allowOrphans=0,
        hyphenationLang=hyphen or "", splitLongWords=1,
    )
    plain = dict(firstLineIndent=0, alignment=TA_LEFT, hyphenationLang="")
    styles = {
        "normal": body,
        "center": ParagraphStyle("center", parent=body, **{**plain, "alignment": TA_CENTER}),
        "right": ParagraphStyle("right", parent=body, **{**plain, "alignment": TA_RIGHT}),
        "subtitle": ParagraphStyle(
            "subtitle", parent=body, fontName="KvSerif-Bold", spaceBefore=size * 0.9, spaceAfter=size * 0.5,
            keepWithNext=1, **{**plain, "alignment": TA_CENTER},
        ),
        "quote": ParagraphStyle(
            "quote", parent=body, fontName="KvSerif-Italic", leftIndent=size * 2, firstLineIndent=0,
            spaceBefore=size * 0.2, spaceAfter=size * 0.3,
        ),
        "epigraph": ParagraphStyle(
            "epigraph", parent=body, fontName="KvSerif-Italic", fontSize=size * 0.93, leading=leading * 0.93,
            leftIndent=size * 9, **plain,
        ),
        "author": ParagraphStyle(
            "author", parent=body, fontName="KvSerif-Italic", fontSize=size * 0.93, spaceAfter=size * 0.7,
            **{**plain, "alignment": TA_RIGHT},
        ),
        "poem": ParagraphStyle("poem", parent=body, leftIndent=size * 2.5, spaceAfter=0, **plain),
        "code": ParagraphStyle(
            "code", parent=body, fontName="KvMono", fontSize=size * 0.85, leading=size * 1.15, spaceBefore=size * 0.4,
            spaceAfter=size * 0.6, backColor=colors.HexColor("#f2eee6"), borderPadding=(4, 4, 4, 4), **plain,
        ),
        "list": ParagraphStyle(
            "list", parent=body, leftIndent=size * 1.6, bulletIndent=size * 0.4, firstLineIndent=0,
            spaceAfter=size * 0.2, alignment=TA_LEFT, hyphenationLang="", bulletFontName="KvSerif",
            bulletFontSize=size,
        ),
        "cell": ParagraphStyle("cell", parent=body, fontSize=size * 0.9, leading=size * 1.2, spaceAfter=0, **plain),
        "cell-head": ParagraphStyle(
            "cell-head", parent=body, fontName="KvSerif-Bold", fontSize=size * 0.9, leading=size * 1.2, spaceAfter=0, **plain
        ),
        "note": ParagraphStyle(
            "note", parent=body, fontSize=size * 0.9, leading=leading * 0.9, firstLineIndent=0, spaceAfter=size * 0.4,
        ),
        "book-title": ParagraphStyle(
            "book-title", fontName="KvSerif-Bold", fontSize=size * 2.6, leading=size * 3.1, alignment=TA_CENTER,
            textColor=INK, spaceAfter=size * 1.6,
        ),
        "book-author": ParagraphStyle(
            "book-author", fontName="KvSerif-Italic", fontSize=size * 1.35, leading=size * 1.7, alignment=TA_CENTER,
            textColor=SOFT_INK, spaceAfter=size * 2,
        ),
        "annotation": ParagraphStyle(
            "annotation", parent=body, fontSize=size * 0.95, leading=leading * 0.95, firstLineIndent=0, spaceAfter=size * 0.5,
        ),
        "toc-title": ParagraphStyle(
            "toc-title", fontName="KvSerif-Bold", fontSize=size * 1.7, leading=size * 2.1, alignment=TA_CENTER,
            textColor=INK, spaceAfter=size * 1.4,
        ),
    }
    for level, (scale, before) in enumerate(((1.9, 0), (1.45, 1.6), (1.2, 1.3), (1.05, 1.1), (1.0, 1.0), (1.0, 1.0)), 1):
        styles[f"h{level}"] = ParagraphStyle(
            f"h{level}", fontName="KvSerif-Bold", fontSize=size * scale, leading=size * scale * 1.22,
            alignment=TA_CENTER if level == 1 else TA_LEFT, textColor=INK, spaceBefore=size * before,
            spaceAfter=size * (1.5 if level == 1 else 0.7), keepWithNext=1,
        )
    return styles


class _StoryBuilder:
    def __init__(self, book: Book, styles: dict[str, ParagraphStyle], width: float, height: float):
        self.book = book
        self.styles = styles
        self.width = width
        self.height = height
        self.markup = _Markup(book)
        self.size = styles["normal"].fontSize

    def image(self, image_id: str, max_height_share: float = 0.92) -> Optional[Image]:
        data = self.book.images.get(image_id)
        if data is None:
            return None
        reader = ImageReader(io.BytesIO(data.data))
        width, height = reader.getSize()
        # Мелкие картинки не растягиваем: 96 точек на дюйм = 0,75 пункта на пиксель.
        draw_width, draw_height = width * 0.75, height * 0.75
        scale = min(1.0, self.width / draw_width, self.height * max_height_share / draw_height)
        flowable = Image(io.BytesIO(data.data), draw_width * scale, draw_height * scale, mask="auto")
        flowable.hAlign = "CENTER"
        return flowable

    def blocks(self, blocks: Iterable[Block], note_style: bool = False) -> list:
        story: list = []
        for block in blocks:
            if isinstance(block, Para):
                story.append(self._para(block, note_style))
            elif isinstance(block, ImageBlock):
                image = self.image(block.image_id)
                if image:
                    story.extend([Spacer(1, self.size * 0.5), image, Spacer(1, self.size * 0.5)])
            elif isinstance(block, Gap):
                story.append(Spacer(1, self.size * 0.9))
            elif isinstance(block, ListBlock):
                story.extend(self._list(block))
            elif isinstance(block, TableBlock):
                story.extend(self._table(block))
        return story

    def _para(self, block: Para, note_style: bool):
        if block.kind == "code":
            # Неразрывные пробелы сохраняют отступы в коде.
            text = self.markup.text(plain_text(block.runs).replace(" ", " "), mono=True)
            return Paragraph(text, self.styles["code"])
        key = block.kind
        if block.kind == "normal":
            key = "note" if note_style else (block.align or "normal")
        return Paragraph(self.markup.runs(block.runs), self.styles[key])

    def _list(self, block: ListBlock) -> list:
        story = []
        for number, item in enumerate(block.items, 1):
            bullet = f"{number}." if block.ordered else "•"
            story.append(Paragraph(self.markup.runs(item), self.styles["list"], bulletText=bullet))
        return story

    def _table(self, block: TableBlock) -> list:
        columns = max(len(row) for row in block.rows)
        longest = max((len(plain_text(cell)) for row in block.rows for cell in row), default=0)
        # Ячейка не может переноситься между страницами: слишком длинные таблицы выводим списком.
        chars_per_line = max(self.width / columns / (self.size * 0.45), 1)
        if longest / chars_per_line * self.size * 1.2 > self.height * 0.7:
            story = []
            for row in block.rows:
                text = " — ".join(self.markup.runs(cell) for cell in row if cell)
                story.append(Paragraph(text, self.styles["normal"]))
            return story
        data = []
        for row_index, row in enumerate(block.rows):
            style = self.styles["cell-head" if block.header and row_index == 0 else "cell"]
            cells = [Paragraph(self.markup.runs(cell), style) for cell in row]
            cells += [""] * (columns - len(cells))
            data.append(cells)
        table = Table(data, colWidths=[self.width / columns] * columns, repeatRows=1 if block.header else 0)
        table.setStyle(
            TableStyle(
                [
                    ("FONTNAME", (0, 0), (-1, -1), "KvSerif"),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#8a8378")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 5),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ]
                + ([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f2eee6"))] if block.header else [])
            )
        )
        return [Spacer(1, self.size * 0.4), table, Spacer(1, self.size * 0.7)]

    def chapter(self, chapter: Chapter, index: int, begins_page: bool) -> list:
        story: list = []
        if begins_page:
            story.append(PageBreak())
        if chapter.title:
            style = self.styles[f"h{min(chapter.level, 6)}"]
            story.append(CondPageBreak(self.height * 0.14))
            title = clean_text(" ".join(chapter.title.split()))
            story.append(_Heading(self.markup.text(title), style, title, chapter.level, f"ch{index}"))
        story.extend(self.blocks(chapter.blocks))
        return story

    def notes(self) -> list:
        if not self.book.notes:
            return []
        title = "Примечания"
        story: list = [PageBreak(), _Heading(title, self.styles["h1"], title, 1, "notes")]
        for note in self.book.notes.values():
            label = escape(note.label)
            anchor = f'<a name="note{label}"/><a href="#ref{label}" color="#1a4e9b"><b>{label}.</b></a> '
            blocks = note.blocks
            first = ""
            if blocks and isinstance(blocks[0], Para):
                first = self.markup.runs(blocks[0].runs)
                blocks = blocks[1:]
            story.append(Paragraph(anchor + first, self.styles["note"]))
            story.extend(self.blocks(blocks, note_style=True))
        return story


def _front_matter(book: Book, builder: _StoryBuilder, options: dict, styles) -> list:
    story: list = []
    if book.meta.cover_id and book.meta.cover_id in book.images:
        # Обложка занимает страницу целиком, насколько позволяет пропорция.
        data = book.images[book.meta.cover_id]
        scale = min(builder.width / data.width, builder.height / data.height) * 0.999
        cover = Image(io.BytesIO(data.data), data.width * scale, data.height * scale, mask="auto")
        cover.hAlign = "CENTER"
        story.extend([cover, PageBreak()])
    if options.get("title_page", True):
        story.append(Spacer(1, builder.height * 0.26))
        story.append(Paragraph(builder.markup.text(book_title(book)), styles["book-title"]))
        if book.meta.authors:
            story.append(Paragraph(builder.markup.text(", ".join(book.meta.authors)), styles["book-author"]))
        if book.meta.annotation:
            for line in book.meta.annotation.splitlines():
                if line.strip():
                    story.append(Paragraph(builder.markup.text(line.strip()), styles["annotation"]))
    return story


def write(source: Book, path: Path, options: Optional[dict] = None, progress=None) -> None:
    options = options or {}
    _register_fonts()
    book = prepare_for_write(source)
    page_name = options.get("page_size", "A5")
    page_size = PAGE_SIZES.get(page_name, A5)
    margin = MARGINS.get(page_name, MARGINS["A5"])
    font_size = float(options.get("font_size", 11 if page_name != "A4" else 12))
    font_size = max(7.0, min(font_size, 24.0))
    styles = _styles(font_size, book.meta.lang)

    has_toc = options.get("toc", True) and sum(1 for chapter in book.chapters if chapter.title) > 1
    document = _BookDocument(
        str(path),
        options.get("page_numbers", True),
        report=progress,
        pagesize=page_size,
        leftMargin=margin,
        rightMargin=margin,
        topMargin=margin,
        bottomMargin=margin * 1.1,
        title=book_title(book),
        author=", ".join(book.meta.authors),
        creator=APP_NAME,
        subject=book.meta.annotation[:1000],
        lang=(book.meta.lang or "ru")[:2],
        # Иначе в файл попадает ссылка на невстроенный шрифт Helvetica.
        initialFontName="KvSerif",
        initialFontSize=font_size,
    )
    builder = _StoryBuilder(book, styles, document.width, document.height)
    front = _front_matter(book, builder, options, styles)

    # Обложка и титульный лист идут без номеров страниц.
    story: list = [*front, NextPageTemplate("body")]
    if has_toc:
        if front:
            story.append(PageBreak())
        story.append(Paragraph("Оглавление", styles["toc-title"]))
        toc = TableOfContents()
        toc.levelStyles = [
            ParagraphStyle(
                f"toc{level}", fontName="KvSerif-Bold" if level == 0 else "KvSerif", fontSize=font_size,
                leading=font_size * 1.35, leftIndent=font_size * 1.2 * level, firstLineIndent=0, spaceAfter=font_size * 0.2,
                textColor=INK,
            )
            for level in range(6)
        ]
        toc.dotsMinLevel = 0
        story.append(toc)

    total = max(len(book.chapters), 1)
    started = bool(front) or has_toc
    for index, chapter in enumerate(book.chapters):
        if progress and index % 20 == 0:
            progress("Верстаю главы", index / total * 0.5)
        begins_page = started and chapter.level == 1 and bool(chapter.title or chapter.blocks)
        story.extend(builder.chapter(chapter, index, begins_page))
        started = True
    story.extend(builder.notes())

    if progress:
        progress("Собираю PDF", 0.6)
    rl_config.warnOnMissingFontGlyphs = 0
    try:
        if has_toc:
            document.multiBuild(story, maxPasses=6)
        else:
            document.build(story)
    except Exception as error:
        if "too large on page" in str(error) or "LayoutError" in type(error).__name__:
            raise ConversionError(
                "Один из элементов книги не помещается на страницу. Выберите больший размер страницы."
            ) from error
        raise
    if builder.markup.missing:
        source.warn("Некоторые редкие символы не нашлись в шрифтах и могут отображаться квадратиками.")


def merge_pages(book: Book, path: Path, progress=None) -> None:
    """Склеивает исходные страницы PDF без перевёрстки, добавляя закладки."""
    from ..model import normalize_levels

    output = pymupdf.open()
    toc: list[list] = []
    sources: dict[str, pymupdf.Document] = {}
    chapters = normalize_levels(book.chapters)
    total = max(len(chapters), 1)
    last: tuple[Optional[str], int] = (None, -1)
    try:
        for index, chapter in enumerate(chapters):
            if progress:
                progress("Склеиваю страницы", index / total)
            first_page = None
            for page_range in chapter.pdf_pages:
                source = sources.get(page_range.path)
                if source is None:
                    source = pymupdf.open(page_range.path)
                    sources[page_range.path] = source
                start = page_range.start
                # Страница может быть общей с концом предыдущей главы.
                if last[0] == page_range.path and start <= last[1]:
                    if first_page is None:
                        first_page = output.page_count
                    start = last[1] + 1
                if start <= page_range.end:
                    if first_page is None:
                        first_page = output.page_count + 1
                    output.insert_pdf(source, from_page=start, to_page=page_range.end, annots=False)
                    last = (page_range.path, page_range.end)
            if chapter.title and first_page is not None:
                toc.append([chapter.level, chapter.title, max(first_page, 1)])
        if toc:
            depth = 0
            for entry in toc:
                entry[0] = min(entry[0], depth + 1)
                depth = entry[0]
            output.set_toc(toc)
        output.set_metadata(
            {"title": book_title(book), "author": ", ".join(book.meta.authors), "creator": APP_NAME, "producer": APP_NAME}
        )
        output.save(str(path), garbage=3, deflate=True)
    finally:
        output.close()
        for source in sources.values():
            source.close()
