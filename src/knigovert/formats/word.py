"""Чтение и запись DOCX, включая настоящие сноски Word."""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path
from typing import Iterable, Optional
from xml.sax.saxutils import escape

import docx
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.opc.constants import CONTENT_TYPE, RELATIONSHIP_TYPE
from docx.opc.packuri import PackURI
from docx.opc.part import Part
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor
from lxml import etree

from ..chapters import find_chapter_titles, split_by_titles
from ..model import (
    Block,
    Book,
    Chapter,
    ConversionError,
    Gap,
    ImageBlock,
    ListBlock,
    Note,
    Para,
    Run,
    TableBlock,
    add_run,
    book_title,
    clean_text,
    plain_text,
    prepare_for_write,
    trim_runs,
)

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
A_BLIP = "{http://schemas.openxmlformats.org/drawingml/2006/main}blip"
VML_IMAGEDATA = "{urn:schemas-microsoft-com:vml}imagedata"
BODY_FONT = "Times New Roman"
HEADING_STYLE_RE = re.compile(r"^(?:heading|заголовок)\s*(\d)$", re.IGNORECASE)
TOC_STYLE_RE = re.compile(r"^(?:toc|оглавление|содержание|заголовок оглавления)\b", re.IGNORECASE)
TOC_TITLES = {"оглавление", "содержание", "contents", "table of contents", "мазмұны"}


# --- Чтение --------------------------------------------------------------


class _Reader:
    def __init__(self, document, book: Book):
        self.document = document
        self.book = book
        self.part = document.part
        self.outline: dict[str, Optional[int]] = {}
        self.has_lists: dict[str, bool] = {}
        self.ordered_lists = self._read_numbering()

    def _read_numbering(self) -> dict[str, bool]:
        """Какие нумерованные списки на самом деле с цифрами, а не с маркерами."""
        result: dict[str, bool] = {}
        try:
            root = self.part.numbering_part.element
        except (NotImplementedError, KeyError, AttributeError):
            return result
        abstract: dict[str, bool] = {}
        for item in root.findall(f"{W}abstractNum"):
            first = item.find(f"{W}lvl/{W}numFmt")
            value = first.get(f"{W}val") if first is not None else "bullet"
            abstract[item.get(f"{W}abstractNumId")] = value not in ("bullet", "none")
        for item in root.findall(f"{W}num"):
            link = item.find(f"{W}abstractNumId")
            if link is not None:
                result[item.get(f"{W}numId")] = abstract.get(link.get(f"{W}val"), False)
        return result

    def style_outline(self, style) -> Optional[int]:
        """Уровень заголовка из стиля, с учётом наследования."""
        if style is None:
            return None
        key = style.style_id
        if key in self.outline:
            return self.outline[key]
        self.outline[key] = None  # защита от зацикленного наследования стилей
        level: Optional[int] = None
        match = HEADING_STYLE_RE.match(style.name or "")
        if match:
            level = int(match.group(1))
        else:
            ppr = style.element.find(f"{W}pPr")
            outline = ppr.find(f"{W}outlineLvl") if ppr is not None else None
            value = outline.get(f"{W}val") if outline is not None else None
            if value is not None and value.isdigit() and int(value) < 9:
                level = int(value) + 1
            else:
                base = getattr(style, "base_style", None)
                level = self.style_outline(base) if base is not None else None
        self.outline[key] = level
        return level

    def paragraph_level(self, element, style) -> Optional[int]:
        ppr = element.find(f"{W}pPr")
        if ppr is not None:
            outline = ppr.find(f"{W}outlineLvl")
            value = outline.get(f"{W}val") if outline is not None else None
            if value is not None and value.isdigit() and int(value) < 9:
                return int(value) + 1
        return self.style_outline(style)

    def _run_flag(self, rpr, name: str) -> bool:
        if rpr is None:
            return False
        element = rpr.find(f"{W}{name}")
        if element is None:
            return False
        value = element.get(f"{W}val")
        return value not in ("0", "false", "off", "none")

    def runs(self, element, images: list[ImageBlock], href: Optional[str] = None) -> list[Run]:
        result: list[Run] = []
        for child in element:
            tag = child.tag
            if tag == f"{W}r":
                self._read_run(child, result, images, href)
            elif tag == f"{W}hyperlink":
                target = href
                relation = child.get(f"{R}id")
                if relation and relation in self.part.rels:
                    candidate = self.part.rels[relation].target_ref
                    if re.match(r"(?i)https?://|mailto:", candidate or ""):
                        target = candidate
                for run in self.runs(child, images, target):
                    _append(result, run)
            elif tag in (f"{W}smartTag", f"{W}sdt", f"{W}sdtContent", f"{W}ins", f"{W}fldSimple"):
                for run in self.runs(child, images, href):
                    _append(result, run)
        return result

    def _read_run(self, element, result: list[Run], images: list[ImageBlock], href: Optional[str]) -> None:
        rpr = element.find(f"{W}rPr")
        vertical = rpr.find(f"{W}vertAlign") if rpr is not None else None
        position = vertical.get(f"{W}val") if vertical is not None else None
        style = {
            "bold": self._run_flag(rpr, "b"),
            "italic": self._run_flag(rpr, "i"),
            "strike": self._run_flag(rpr, "strike") or self._run_flag(rpr, "dstrike"),
            "sup": position == "superscript",
            "sub": position == "subscript",
            "href": href,
        }
        for child in element:
            tag = child.tag
            if tag == f"{W}t":
                add_run(result, Run(clean_text(child.text or ""), **style))
            elif tag == f"{W}tab":
                add_run(result, Run("\t", **style))
            elif tag == f"{W}br":
                kind = child.get(f"{W}type")
                if kind in (None, "textWrapping"):
                    add_run(result, Run("\n", **style))
            elif tag == f"{W}noBreakHyphen":
                add_run(result, Run("‑", **style))
            elif tag == f"{W}footnoteReference":
                note_id = f"w:f{child.get(f'{W}id')}"
                if note_id in self.book.notes:
                    result.append(Run("", note=note_id))
            elif tag == f"{W}endnoteReference":
                note_id = f"w:e{child.get(f'{W}id')}"
                if note_id in self.book.notes:
                    result.append(Run("", note=note_id))
            elif tag in (f"{W}drawing", f"{W}pict", f"{W}object"):
                images.extend(self._images(child))
            elif tag.endswith("}AlternateContent"):
                fallback = [item for item in child if item.tag.endswith("}Choice")] or list(child)
                for item in fallback[:1]:
                    images.extend(self._images(item))

    def _images(self, element) -> list[ImageBlock]:
        found = []
        for blip in list(element.iter(A_BLIP)) + list(element.iter(VML_IMAGEDATA)):
            relation = blip.get(f"{R}embed") or blip.get(f"{R}id")
            if not relation or relation not in self.part.rels:
                continue
            try:
                blob = self.part.rels[relation].target_part.blob
            except (KeyError, ValueError, AttributeError):
                continue
            image_id = self.book.add_image(blob)
            if image_id:
                found.append(ImageBlock(image_id))
            else:
                self.book.warn("Часть картинок в неподдерживаемом формате (например, EMF) пропущена.")
        return found

    def read_notes(self) -> None:
        for name, prefix in (("footnotes", "f"), ("endnotes", "e")):
            root = _note_part(self.document, name)
            if root is None:
                continue
            for note in root:
                if note.get(f"{W}type") in ("separator", "continuationSeparator", "continuationNotice"):
                    continue
                note_id = f"w:{prefix}{note.get(f'{W}id')}"
                self.book.notes[note_id] = Note(note_id)
                blocks: list[Block] = []
                for paragraph in note.findall(f"{W}p"):
                    images: list[ImageBlock] = []
                    runs = trim_runs(self.runs(paragraph, images))
                    if runs:
                        blocks.append(Para(runs))
                self.book.notes[note_id].blocks = blocks

    def table(self, element) -> Optional[TableBlock]:
        rows: list[list[list[Run]]] = []
        header = False
        for index, row in enumerate(element.findall(f"{W}tr")):
            trpr = row.find(f"{W}trPr")
            if index == 0 and trpr is not None and trpr.find(f"{W}tblHeader") is not None:
                header = True
            cells = []
            for cell in row.findall(f"{W}tc"):
                pieces: list[Run] = []
                images: list[ImageBlock] = []
                for paragraph in cell.findall(f"{W}p"):
                    if pieces:
                        add_run(pieces, Run("\n"))
                    for run in self.runs(paragraph, images):
                        _append(pieces, run)
                cells.append(trim_runs(pieces))
            if cells:
                rows.append(cells)
        return TableBlock(rows, header) if rows else None


def _append(runs: list[Run], run: Run) -> None:
    if run.note:
        runs.append(run)
    else:
        add_run(runs, run)


def _note_part(document, name: str):
    relation_type = getattr(RELATIONSHIP_TYPE, name.upper())
    for relation in document.part.rels.values():
        if relation.reltype == relation_type:
            try:
                return etree.fromstring(
                    relation.target_part.blob,
                    etree.XMLParser(recover=True, resolve_entities=False, no_network=True, huge_tree=True),
                )
            except etree.XMLSyntaxError:
                return None
    return None


def _body_elements(body) -> Iterable:
    for element in body:
        if element.tag == f"{W}sdt":
            content = element.find(f"{W}sdtContent")
            if content is not None:
                yield from _body_elements(content)
        else:
            yield element


def read(path: Path, progress=None) -> Book:
    if path.read_bytes()[:4] == b"\xd0\xcf\x11\xe0":
        raise ConversionError("Это старый формат .doc. Откройте файл в Word и сохраните как .docx.")
    try:
        document = docx.Document(str(path))
    except (zipfile.BadZipFile, KeyError, ValueError) as error:
        raise ConversionError("Файл DOCX повреждён и не открывается.") from error
    book = Book()
    reader = _Reader(document, book)
    reader.read_notes()
    props = document.core_properties
    book.meta.title = (props.title or "").strip()
    if props.author and props.author.strip().lower() not in ("python-docx", "author", "автор"):
        book.meta.authors = [item.strip() for item in re.split(r"[;,]", props.author) if item.strip()]
    book.meta.annotation = (props.comments or "").strip() if (props.comments or "") != "generated by python-docx" else ""
    book.meta.lang = (props.language or "")[:2]

    styles = {style.style_id: style for style in document.styles}
    default_style = document.styles.default(WD_STYLE_TYPE.PARAGRAPH)
    blocks: list[Block] = []
    titles: dict[int, int] = {}
    numbered: list[tuple[int, str]] = []
    pending_list: Optional[ListBlock] = None
    pending_list_id: Optional[str] = None
    elements = list(_body_elements(document.element.body))
    total = max(len(elements), 1)
    front_lines = {_compact(", ".join(book.meta.authors))} | {
        _compact(line) for line in book.meta.annotation.splitlines()
    }
    front_lines.discard("")
    in_toc = False

    for position, element in enumerate(elements):
        if progress and position % 200 == 0:
            progress("Читаю документ", position / total)
        if element.tag == f"{W}tbl":
            pending_list = None
            table = reader.table(element)
            if table:
                blocks.append(table)
            continue
        if element.tag != f"{W}p":
            continue
        # Оглавление-поле строится заново из глав, в текст книги оно не попадает.
        if in_toc or _starts_toc_field(element):
            if not in_toc and blocks and isinstance(blocks[-1], Para):
                if _compact(plain_text(blocks[-1].runs)) in TOC_TITLES:
                    blocks.pop()
            in_toc = not _ends_field(element)
            continue
        ppr = element.find(f"{W}pPr")
        style_ref = ppr.find(f"{W}pStyle") if ppr is not None else None
        style_id = style_ref.get(f"{W}val") if style_ref is not None else None
        style = styles.get(style_id, default_style)
        style_name = (style.name if style is not None else "") or ""
        if TOC_STYLE_RE.match(style_name):
            continue
        images: list[ImageBlock] = []
        runs = trim_runs(reader.runs(element, images))
        text = plain_text(runs)

        if style_name.strip().lower() in ("title", "название", "заголовок") and text:
            if not book.meta.title:
                book.meta.title = text.strip()
            continue
        if style_name.strip().lower() in ("subtitle", "подзаголовок") and text and not blocks:
            continue
        # Автор и аннотация на титульной странице уже есть в описании книги.
        if text.strip() and _compact(text) in front_lines and all(isinstance(item, ImageBlock) for item in blocks):
            continue

        blocks.extend(images)
        if not text.strip() and not any(run.note for run in runs):
            continue

        level = reader.paragraph_level(element, style)
        if level is not None:
            pending_list = None
            titles[len(blocks)] = level
            blocks.append(Para(runs))
            continue

        num_pr = ppr.find(f"{W}numPr") if ppr is not None else None
        num_id = None
        if num_pr is not None:
            num_ref = num_pr.find(f"{W}numId")
            num_id = num_ref.get(f"{W}val") if num_ref is not None else None
        lowered_name = style_name.lower()
        list_style = lowered_name.startswith(("list bullet", "list number", "маркированный список", "нумерованный список"))
        if (num_id and num_id != "0") or list_style:
            ordered = reader.ordered_lists.get(
                num_id or "", lowered_name.startswith(("list number", "нумерованный"))
            )
            if pending_list is None or pending_list_id != num_id or pending_list.ordered != ordered:
                pending_list = ListBlock([], ordered)
                pending_list_id = num_id
                blocks.append(pending_list)
            pending_list.items.append(runs)
            continue
        pending_list = None

        align = None
        jc = ppr.find(f"{W}jc") if ppr is not None else None
        if jc is not None and jc.get(f"{W}val") in ("center", "right"):
            align = jc.get(f"{W}val")
        kind = "normal"
        lowered = style_name.lower()
        if lowered in ("quote", "intense quote", "цитата", "выделенная цитата", "block text"):
            kind = "quote"
        elif lowered in ("subtitle", "подзаголовок"):
            kind = "subtitle"
        elif lowered in ("code", "html preformatted", "стандартный html"):
            kind = "code"
        elif _is_fake_heading(runs, align, text):
            numbered.append((len(blocks), text))
        blocks.append(Para(runs, kind, align))

    if not titles:
        titles = find_chapter_titles(blocks)
    if not titles and len(numbered) >= 2:
        titles = {index: 1 for index, _ in numbered}
    if titles:
        levels = sorted(set(titles.values()))
        rank = {level: position + 1 for position, level in enumerate(levels)}
        titles = {index: rank[level] for index, level in titles.items()}
        book.chapters = split_by_titles(blocks, titles, path.name)
    elif blocks:
        book.chapters = [Chapter(blocks=blocks, source=path.name)]
    if not book.chapters:
        raise ConversionError("В документе нет текста.")
    # Одна картинка перед первой главой — это обложка.
    first = book.chapters[0]
    if len(book.chapters) > 1 and not first.title and len(first.blocks) == 1 and isinstance(first.blocks[0], ImageBlock):
        book.meta.cover_id = first.blocks[0].image_id
        book.chapters.pop(0)
    _lift_single_top(book)
    book.meta.title = book.meta.title or path.stem
    return book


def _compact(text: str) -> str:
    return " ".join(text.split()).lower()


def _starts_toc_field(paragraph) -> bool:
    return any(
        (item.text or "").strip().upper().startswith("TOC") for item in paragraph.iter(f"{W}instrText")
    )


def _ends_field(paragraph) -> bool:
    return any(item.get(f"{W}fldCharType") == "end" for item in paragraph.iter(f"{W}fldChar"))


def _is_fake_heading(runs: list[Run], align: Optional[str], text: str) -> bool:
    """Заголовок, оформленный вручную: короткий, жирный и по центру."""
    stripped = text.strip()
    if not stripped or len(stripped) > 90 or stripped[-1] in ".,;":
        return False
    return align == "center" and all(run.bold for run in runs if run.text.strip())


def _lift_single_top(book: Book) -> None:
    """Единственный заголовок верхнего уровня над главами — это название книги."""
    if len(book.chapters) < 3:
        return
    first = book.chapters[0]
    if first.level != 1 or first.blocks:
        return
    if any(chapter.level == 1 for chapter in book.chapters[1:]):
        return
    if not book.meta.title or book.meta.title == first.title:
        book.meta.title = first.title
        book.chapters.pop(0)
        for chapter in book.chapters:
            chapter.level -= 1


# --- Запись --------------------------------------------------------------


def _set_font(style, name: str, size: Optional[float] = None, bold: Optional[bool] = None) -> None:
    style.font.name = name
    rpr = style.element.get_or_add_rPr()
    fonts = rpr.find(qn("w:rFonts"))
    if fonts is None:
        fonts = OxmlElement("w:rFonts")
        rpr.append(fonts)
    for attr in ("w:ascii", "w:hAnsi", "w:eastAsia", "w:cs"):
        fonts.set(qn(attr), name)
    for attr in ("w:asciiTheme", "w:hAnsiTheme", "w:eastAsiaTheme", "w:cstheme"):
        if fonts.get(qn(attr)) is not None:
            del fonts.attrib[qn(attr)]
    if size:
        style.font.size = Pt(size)
    if bold is not None:
        style.font.bold = bold


def _setup_styles(document) -> None:
    normal = document.styles["Normal"]
    _set_font(normal, BODY_FONT, 12)
    normal.paragraph_format.space_after = Pt(4)
    normal.paragraph_format.line_spacing = 1.15
    for level, size in ((1, 20), (2, 16), (3, 14), (4, 12), (5, 12), (6, 12)):
        style = document.styles[f"Heading {level}"]
        _set_font(style, BODY_FONT, size, True)
        style.font.color.rgb = RGBColor(0x1D, 0x1B, 0x18)
        style.font.italic = False
        style.paragraph_format.space_before = Pt(18 if level > 1 else 0)
        style.paragraph_format.space_after = Pt(10)
        style.paragraph_format.keep_with_next = True
    _set_font(document.styles["Title"], BODY_FONT, 28, True)
    title_rpr = document.styles["Title"].element.get_or_add_rPr()
    document.styles["Title"].font.color.rgb = RGBColor(0x1D, 0x1B, 0x18)
    for element in title_rpr.findall(qn("w:spacing")):
        title_rpr.remove(element)
    title_ppr = document.styles["Title"].element.get_or_add_pPr()
    for element in title_ppr.findall(qn("w:pBdr")):
        title_ppr.remove(element)
    for name in ("List Bullet", "List Number"):
        _set_font(document.styles[name], BODY_FONT, 12)


class _Writer:
    def __init__(self, document, book: Book):
        self.document = document
        self.book = book
        self.width = document.sections[0].page_width - document.sections[0].left_margin - document.sections[0].right_margin
        self.height = document.sections[0].page_height - document.sections[0].top_margin - document.sections[0].bottom_margin
        self.note_ids: dict[str, int] = {}
        self.bookmark_id = 100

    def add_runs(self, paragraph, runs: list[Run]) -> None:
        for run in runs:
            if run.note:
                self._note_reference(paragraph, run.note)
                continue
            if run.href:
                self._hyperlink(paragraph, run)
                continue
            self._text_run(paragraph, run)

    def _text_run(self, paragraph, run: Run):
        piece = paragraph.add_run()
        self._fill(piece, run)
        return piece

    @staticmethod
    def _fill(piece, run: Run) -> None:
        pieces = clean_text(run.text).split("\n")
        for index, text in enumerate(pieces):
            if index:
                piece.add_break(WD_BREAK.LINE)
            if text:
                piece.add_text(text)
        piece.bold = run.bold or None
        piece.italic = run.italic or None
        piece.font.strike = run.strike or None
        piece.font.superscript = run.sup or None
        piece.font.subscript = run.sub or None
        if run.code:
            piece.font.name = "Consolas"

    def _hyperlink(self, paragraph, run: Run) -> None:
        relation = paragraph.part.relate_to(run.href, RELATIONSHIP_TYPE.HYPERLINK, is_external=True)
        link = OxmlElement("w:hyperlink")
        link.set(qn("r:id"), relation)
        piece = paragraph.add_run()
        self._fill(piece, run)
        piece.font.underline = True
        piece.font.color.rgb = RGBColor(0x1A, 0x4E, 0x9B)
        paragraph._p.remove(piece._r)
        link.append(piece._r)
        paragraph._p.append(link)

    def _note_reference(self, paragraph, note_key: str) -> None:
        note = self.book.notes.get(note_key)
        if note is None:
            return
        note_id = self.note_ids.setdefault(note_key, len(self.note_ids) + 1)
        piece = paragraph.add_run()
        piece.font.superscript = True
        reference = OxmlElement("w:footnoteReference")
        reference.set(qn("w:id"), str(note_id))
        piece._r.append(reference)

    def add_blocks(self, blocks: list[Block]) -> None:
        for block in blocks:
            if isinstance(block, Para):
                self._para(block)
            elif isinstance(block, ImageBlock):
                self._image(block)
            elif isinstance(block, Gap):
                self.document.add_paragraph()
            elif isinstance(block, ListBlock):
                style = "List Number" if block.ordered else "List Bullet"
                if block.ordered:
                    # Каждый нумерованный список начинается с единицы.
                    for number, item in enumerate(block.items, 1):
                        paragraph = self.document.add_paragraph()
                        paragraph.paragraph_format.left_indent = Cm(1)
                        paragraph.paragraph_format.first_line_indent = Cm(-0.6)
                        paragraph.add_run(f"{number}. ")
                        self.add_runs(paragraph, item)
                else:
                    for item in block.items:
                        self.add_runs(self.document.add_paragraph(style=style), item)
            elif isinstance(block, TableBlock):
                self._table(block)

    def _para(self, block: Para) -> None:
        paragraph = self.document.add_paragraph()
        fmt = paragraph.paragraph_format
        runs = block.runs
        if block.kind == "subtitle":
            fmt.alignment = WD_ALIGN_PARAGRAPH.CENTER
            fmt.space_before = Pt(10)
            fmt.keep_with_next = True
            runs = [_with(run, bold=True) for run in runs]
        elif block.kind == "quote":
            fmt.left_indent = Cm(1.2)
            runs = [_with(run, italic=True) for run in runs]
        elif block.kind == "epigraph":
            fmt.left_indent = Cm(6)
            runs = [_with(run, italic=True) for run in runs]
        elif block.kind == "author":
            fmt.alignment = WD_ALIGN_PARAGRAPH.RIGHT
            runs = [_with(run, italic=True) for run in runs]
        elif block.kind == "poem":
            fmt.left_indent = Cm(1.5)
            fmt.space_after = Pt(0)
        elif block.kind == "code":
            fmt.space_after = Pt(6)
            runs = [Run(plain_text(block.runs), code=True)]
        else:
            if block.align == "center":
                fmt.alignment = WD_ALIGN_PARAGRAPH.CENTER
            elif block.align == "right":
                fmt.alignment = WD_ALIGN_PARAGRAPH.RIGHT
            else:
                fmt.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
                fmt.first_line_indent = Cm(0.8)
        self.add_runs(paragraph, runs)

    def _image(self, block: ImageBlock) -> None:
        image = self.book.images.get(block.image_id)
        if image is None:
            return
        paragraph = self.document.add_paragraph()
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        piece = paragraph.add_run()
        self.picture(piece, image, self.width, int(self.height * 0.9))

    @staticmethod
    def picture(piece, image, max_width: int, max_height: int) -> None:
        # 96 точек на дюйм: 9525 EMU на пиксель.
        width = image.width * 9525
        height = image.height * 9525
        scale = min(1.0, max_width / width, max_height / height)
        piece.add_picture(io.BytesIO(image.data), width=int(width * scale), height=int(height * scale))

    def _table(self, block: TableBlock) -> None:
        columns = max(len(row) for row in block.rows)
        table = self.document.add_table(rows=len(block.rows), cols=columns)
        table.style = "Table Grid"
        for row_index, row in enumerate(block.rows):
            for column, cell in enumerate(row):
                paragraph = table.cell(row_index, column).paragraphs[0]
                runs = cell
                if block.header and row_index == 0:
                    runs = [_with(run, bold=True) for run in cell]
                self.add_runs(paragraph, runs)
            if block.header and row_index == 0:
                trpr = table.rows[0]._tr.get_or_add_trPr()
                trpr.append(OxmlElement("w:tblHeader"))
        self.document.add_paragraph()

    def bookmark(self, paragraph, name: str) -> None:
        self.bookmark_id += 1
        start = OxmlElement("w:bookmarkStart")
        start.set(qn("w:id"), str(self.bookmark_id))
        start.set(qn("w:name"), name)
        end = OxmlElement("w:bookmarkEnd")
        end.set(qn("w:id"), str(self.bookmark_id))
        paragraph._p.insert(1 if paragraph._p.pPr is not None else 0, start)
        paragraph._p.append(end)

    def toc(self, chapters: list[Chapter]) -> None:
        """Поле оглавления Word: сразу видно и обновляется клавишей F9."""
        heading = self.document.add_paragraph()
        heading.alignment = WD_ALIGN_PARAGRAPH.CENTER
        heading.paragraph_format.page_break_before = True
        heading.paragraph_format.space_after = Pt(14)
        title = heading.add_run("Оглавление")
        title.bold = True
        title.font.size = Pt(18)
        entries = [(index, chapter) for index, chapter in enumerate(chapters) if chapter.title]
        for position, (index, chapter) in enumerate(entries):
            paragraph = self.document.add_paragraph()
            paragraph.paragraph_format.left_indent = Cm(0.7 * (chapter.level - 1))
            paragraph.paragraph_format.space_after = Pt(3)
            if position == 0:
                _field_char(paragraph, "begin")
                instruction = paragraph.add_run()
                text = OxmlElement("w:instrText")
                text.set(qn("xml:space"), "preserve")
                text.text = ' TOC \\o "1-6" \\h \\z \\u '
                instruction._r.append(text)
                _field_char(paragraph, "separate")
            link = OxmlElement("w:hyperlink")
            link.set(qn("w:anchor"), f"ch{index}")
            link.set(qn("w:history"), "1")
            piece = paragraph.add_run(clean_text(chapter.title))
            paragraph._p.remove(piece._r)
            link.append(piece._r)
            paragraph._p.append(link)
            if position == len(entries) - 1:
                _field_char(paragraph, "end")

    def write_footnotes(self) -> None:
        if not self.note_ids:
            return
        parts = [
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:footnotes xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<w:footnote w:type="separator" w:id="-1"><w:p><w:pPr><w:spacing w:after="0" w:line="240" w:lineRule="auto"/></w:pPr>'
            "<w:r><w:separator/></w:r></w:p></w:footnote>"
            '<w:footnote w:type="continuationSeparator" w:id="0"><w:p><w:pPr><w:spacing w:after="0" w:line="240" w:lineRule="auto"/></w:pPr>'
            "<w:r><w:continuationSeparator/></w:r></w:p></w:footnote>"
        ]
        for key, note_id in self.note_ids.items():
            note = self.book.notes[key]
            parts.append(f'<w:footnote w:id="{note_id}">')
            paragraphs = [
                block for block in note.blocks if isinstance(block, Para)
            ] or [Para([Run("")])]
            for index, paragraph in enumerate(paragraphs):
                parts.append('<w:p><w:pPr><w:spacing w:after="0"/></w:pPr>')
                if index == 0:
                    parts.append(
                        '<w:r><w:rPr><w:vertAlign w:val="superscript"/><w:sz w:val="20"/></w:rPr><w:footnoteRef/></w:r>'
                        '<w:r><w:rPr><w:sz w:val="20"/></w:rPr><w:t xml:space="preserve"> </w:t></w:r>'
                    )
                for run in paragraph.runs:
                    if run.note:
                        continue
                    props = '<w:sz w:val="20"/>'
                    if run.bold:
                        props = "<w:b/>" + props
                    if run.italic:
                        props = "<w:i/>" + props
                    if run.sup:
                        props += '<w:vertAlign w:val="superscript"/>'
                    text = escape(clean_text(run.text).replace("\n", " "))
                    parts.append(f'<w:r><w:rPr>{props}</w:rPr><w:t xml:space="preserve">{text}</w:t></w:r>')
                parts.append("</w:p>")
            parts.append("</w:footnote>")
        parts.append("</w:footnotes>")
        part = Part(
            PackURI("/word/footnotes.xml"),
            CONTENT_TYPE.WML_FOOTNOTES,
            "".join(parts).encode("utf-8"),
            self.document.part.package,
        )
        self.document.part.relate_to(part, RELATIONSHIP_TYPE.FOOTNOTES)


def _with(run: Run, **flags) -> Run:
    from dataclasses import replace

    return replace(run, **flags)


def _field_char(paragraph, kind: str) -> None:
    piece = paragraph.add_run()
    char = OxmlElement("w:fldChar")
    char.set(qn("w:fldCharType"), kind)
    piece._r.append(char)


def write(source: Book, path: Path, options: Optional[dict] = None, progress=None) -> None:
    options = options or {}
    book = prepare_for_write(source)
    document = docx.Document()
    _setup_styles(document)
    section = document.sections[0]
    section.page_width, section.page_height = Cm(21), Cm(29.7)
    section.left_margin = section.right_margin = Cm(2.5)
    section.top_margin = section.bottom_margin = Cm(2.2)

    props = document.core_properties
    props.title = book_title(book)
    props.author = ", ".join(book.meta.authors)
    props.comments = book.meta.annotation
    props.language = (book.meta.lang or "ru")[:2]

    writer = _Writer(document, book)
    has_front = False
    if book.meta.cover_id:
        paragraph = document.add_paragraph()
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        writer.picture(
            paragraph.add_run(), book.images[book.meta.cover_id], writer.width, int(writer.height * 0.95)
        )
        has_front = True
    if options.get("title_page", True):
        title = document.add_paragraph(book_title(book), style="Title")
        title.alignment = WD_ALIGN_PARAGRAPH.CENTER
        if book.meta.cover_id:
            title.paragraph_format.page_break_before = True
        if book.meta.authors:
            author = document.add_paragraph()
            author.alignment = WD_ALIGN_PARAGRAPH.CENTER
            piece = author.add_run(", ".join(book.meta.authors))
            piece.italic = True
            piece.font.size = Pt(14)
        if book.meta.annotation:
            for line in book.meta.annotation.splitlines():
                if line.strip():
                    paragraph = document.add_paragraph(line.strip())
                    paragraph.paragraph_format.space_before = Pt(6)
        has_front = True
    if options.get("toc", True) and sum(1 for chapter in book.chapters if chapter.title) > 1:
        writer.toc(book.chapters)
        has_front = True

    total = max(len(book.chapters), 1)
    started = False
    for index, chapter in enumerate(book.chapters):
        if progress:
            progress("Собираю документ", index / total)
        if chapter.title:
            heading = document.add_heading(clean_text(chapter.title), level=min(chapter.level, 6))
            writer.bookmark(heading, f"ch{index}")
            if chapter.level == 1 and (started or has_front):
                heading.paragraph_format.page_break_before = True
        elif chapter.blocks and (started or has_front):
            # Глава без названия тоже начинается с новой страницы.
            document.add_page_break()
        writer.add_blocks(chapter.blocks)
        started = True
    writer.write_footnotes()
    document.save(str(path))
