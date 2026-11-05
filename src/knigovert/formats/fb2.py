"""Чтение и запись FictionBook 2 (FB2 и FB2 в ZIP)."""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import io
import re
import uuid
import zipfile
from pathlib import Path
from typing import Optional

from lxml import etree

from .. import APP_NAME, __version__
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
    collapse_spaces,
    plain_text,
    prepare_for_write,
    trim_runs,
)

FB_NS = "http://www.gribuser.ru/xml/fictionbook/2.0"
XLINK_NS = "http://www.w3.org/1999/xlink"
NOTE_BODIES = {"notes", "footnotes", "comments", "note", "примечания", "сноски"}


# --- Чтение --------------------------------------------------------------


def _local(element) -> str:
    tag = element.tag
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].lower()


def _href(element) -> str:
    for name, value in element.attrib.items():
        if name.rsplit("}", 1)[-1] == "href" or name.endswith(":href"):
            return value
    return ""


def _children(element, *names: str):
    return [child for child in element if _local(child) in names]


def _first(element, name: str):
    if element is None:
        return None
    for child in element:
        if _local(child) == name:
            return child
    return None


def _text_of(element) -> str:
    if element is None:
        return ""
    return collapse_spaces("".join(element.itertext())).strip()


def _load_bytes(path: Path) -> bytes:
    data = path.read_bytes()
    if data[:2] != b"PK":
        return data
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = [name for name in archive.namelist() if not name.endswith("/")]
            candidates = [name for name in names if name.lower().endswith(".fb2")] or names
            if not candidates:
                raise ConversionError("В архиве нет файла FB2.")
            return archive.read(candidates[0])
    except zipfile.BadZipFile as error:
        raise ConversionError("Архив повреждён и не открывается.") from error


def _parse(data: bytes):
    parser = etree.XMLParser(
        recover=True, resolve_entities=False, no_network=True, huge_tree=True, remove_comments=True
    )
    root = None
    try:
        root = etree.fromstring(data, parser)
    except (etree.XMLSyntaxError, ValueError):
        pass
    if root is None or _local(root) != "fictionbook" or not len(root):
        # Кодировка объявлена неверно или не объявлена вовсе.
        for encoding in ("utf-8-sig", "cp1251", "utf-16", "koi8-r"):
            try:
                text = data.decode(encoding)
            except (UnicodeDecodeError, LookupError):
                continue
            text = re.sub(r"^\s*<\?xml[^>]*\?>", "", text)
            try:
                root = etree.fromstring(text.encode("utf-8"), parser)
            except (etree.XMLSyntaxError, ValueError):
                continue
            if root is not None and _local(root) == "fictionbook":
                break
    if root is None or _local(root) != "fictionbook":
        raise ConversionError("Файл не похож на FB2: нет корневого элемента FictionBook.")
    return root


class _Reader:
    def __init__(self, book: Book, source: str):
        self.book = book
        self.source = source
        self.image_ids: dict[str, str] = {}
        self.note_targets: set[str] = set()

    def read_binaries(self, root) -> None:
        for binary in _children(root, "binary"):
            key = binary.get("id", "")
            raw = re.sub(r"\s+", "", binary.text or "")
            if not key or not raw:
                continue
            try:
                data = base64.b64decode(raw + "=" * (-len(raw) % 4))
            except (binascii.Error, ValueError):
                self.book.warn(f"Картинка {key} повреждена и пропущена.")
                continue
            image_id = self.book.add_image(data)
            if image_id:
                self.image_ids[key] = image_id
            else:
                self.book.warn(f"Картинка {key} в неизвестном формате и пропущена.")

    def read_description(self, root) -> None:
        description = _first(root, "description")
        info = _first(description, "title-info")
        meta = self.book.meta
        if info is None:
            return
        meta.title = _text_of(_first(info, "book-title"))
        meta.lang = _text_of(_first(info, "lang"))
        meta.date = _text_of(_first(info, "date"))
        meta.genres = [_text_of(genre) for genre in _children(info, "genre") if _text_of(genre)]
        for author in _children(info, "author"):
            parts = [_text_of(_first(author, key)) for key in ("first-name", "middle-name", "last-name")]
            name = " ".join(part for part in parts if part) or _text_of(_first(author, "nickname"))
            if name:
                meta.authors.append(name)
        annotation = _first(info, "annotation")
        if annotation is not None:
            lines = [_text_of(child) for child in annotation]
            meta.annotation = "\n".join(line for line in lines if line)
        sequence = _first(info, "sequence")
        if sequence is not None:
            meta.series = collapse_spaces(sequence.get("name", "")).strip()
            meta.series_number = (sequence.get("number") or "").strip()
        cover = _first(info, "coverpage")
        if cover is not None:
            image = _first(cover, "image")
            if image is not None:
                meta.cover_id = self.image_ids.get(_href(image).lstrip("#"))
        publish = _first(description, "publish-info")
        if publish is not None:
            meta.publisher = _text_of(_first(publish, "publisher"))
            meta.date = meta.date or _text_of(_first(publish, "year"))

    def inline(self, element, runs: Optional[list[Run]] = None, style: Optional[dict] = None) -> list[Run]:
        runs = [] if runs is None else runs
        style = style or {}
        if element.text:
            add_run(runs, Run(collapse_spaces(element.text), **style))
        for child in element:
            name = _local(child)
            if name in ("strong", "emphasis", "strikethrough", "sub", "sup", "code"):
                key = {
                    "strong": "bold",
                    "emphasis": "italic",
                    "strikethrough": "strike",
                    "sub": "sub",
                    "sup": "sup",
                    "code": "code",
                }[name]
                self.inline(child, runs, {**style, key: True})
            elif name == "a":
                target = _href(child)
                if target.startswith("#") and target[1:] in self.note_targets:
                    runs.append(Run(_text_of(child), note=f"n:{target[1:]}"))
                elif re.match(r"(?i)https?://|mailto:", target):
                    self.inline(child, runs, {**style, "href": target})
                else:
                    self.inline(child, runs, style)
            elif name == "image":
                pass  # Картинка внутри абзаца выносится отдельным блоком в read_blocks.
            elif name:
                self.inline(child, runs, style)
            if child.tail:
                add_run(runs, Run(collapse_spaces(child.tail), **style))
        return runs

    def para(self, element, kind: str = "normal") -> Optional[Para]:
        runs = trim_runs(self.inline(element))
        return Para(runs, kind) if runs else None

    def image_block(self, element) -> Optional[ImageBlock]:
        image_id = self.image_ids.get(_href(element).lstrip("#"))
        if image_id:
            return ImageBlock(image_id, element.get("alt", ""))
        return None

    def read_blocks(self, element, kind: str = "normal") -> list[Block]:
        """Содержимое секции, цитаты или эпиграфа, кроме вложенных секций."""
        blocks: list[Block] = []
        for child in element:
            name = _local(child)
            if name == "p":
                paragraph = self.para(child, kind)
                if paragraph:
                    blocks.append(paragraph)
                for image in child.iter("{*}image"):
                    block = self.image_block(image)
                    if block:
                        blocks.append(block)
            elif name == "subtitle":
                paragraph = self.para(child, "subtitle")
                if paragraph:
                    blocks.append(paragraph)
            elif name == "text-author":
                paragraph = self.para(child, "author")
                if paragraph:
                    blocks.append(paragraph)
            elif name == "empty-line":
                blocks.append(Gap())
            elif name == "image":
                block = self.image_block(child)
                if block:
                    blocks.append(block)
            elif name == "epigraph":
                blocks.extend(self.read_blocks(child, "epigraph"))
            elif name == "cite":
                blocks.extend(self.read_blocks(child, "quote"))
            elif name == "annotation":
                blocks.extend(self.read_blocks(child, "quote"))
            elif name == "poem":
                blocks.extend(self.read_poem(child))
            elif name == "table":
                table = self.read_table(child)
                if table:
                    blocks.append(table)
        return blocks

    def read_poem(self, element) -> list[Block]:
        blocks: list[Block] = []
        for child in element:
            name = _local(child)
            if name == "title":
                for line in _children(child, "p"):
                    paragraph = self.para(line, "subtitle")
                    if paragraph:
                        blocks.append(paragraph)
            elif name == "epigraph":
                blocks.extend(self.read_blocks(child, "epigraph"))
            elif name == "stanza":
                if blocks and not isinstance(blocks[-1], Gap):
                    blocks.append(Gap())
                for line in _children(child, "v"):
                    paragraph = self.para(line, "poem")
                    if paragraph:
                        blocks.append(paragraph)
            elif name == "text-author":
                paragraph = self.para(child, "author")
                if paragraph:
                    blocks.append(paragraph)
        return blocks

    def read_table(self, element) -> Optional[TableBlock]:
        rows: list[list[list[Run]]] = []
        header = False
        for row_index, row in enumerate(_children(element, "tr")):
            cells = []
            for cell in _children(row, "td", "th"):
                if row_index == 0 and _local(cell) == "th":
                    header = True
                cells.append(trim_runs(self.inline(cell)))
            if cells:
                rows.append(cells)
        return TableBlock(rows, header) if rows else None

    def section_title(self, element) -> str:
        title = _first(element, "title")
        if title is None:
            return ""
        lines = [_text_of(line) for line in _children(title, "p")] or [_text_of(title)]
        lines = [line for line in lines if line]
        result = ""
        for line in lines:
            if not result:
                result = line
            elif result[-1] in ".!?:…":
                result = f"{result} {line}"
            else:
                result = f"{result}. {line}"
        return result

    def read_section(self, element, level: int, chapters: list[Chapter]) -> None:
        chapter = Chapter(title=self.section_title(element), level=level, source=self.source)
        chapters.append(chapter)
        chapter.blocks = self.read_blocks(
            [child for child in element if _local(child) not in ("title", "section")]
        )
        sections = _children(element, "section")
        # Текст главы с подглавами по схеме лежит в первой подсекции без названия.
        if sections and not chapter.blocks and _first(sections[0], "title") is None and not _children(sections[0], "section"):
            chapter.blocks = self.read_blocks(list(sections[0]))
            sections = sections[1:]
        for child in sections:
            self.read_section(child, level + 1, chapters)

    def collect_note_targets(self, bodies) -> None:
        for body in bodies:
            for section in body.iter("{*}section", "section"):
                key = section.get("id")
                if key:
                    self.note_targets.add(key)

    def read_notes(self, bodies) -> None:
        for body in bodies:
            for section in body.iter("{*}section", "section"):
                key = section.get("id")
                if not key:
                    continue
                content = [child for child in section if _local(child) not in ("title", "section")]
                self.book.notes[f"n:{key}"] = Note(f"n:{key}", self.read_blocks(content))


def read(path: Path, progress=None) -> Book:
    root = _parse(_load_bytes(path))
    book = Book()
    reader = _Reader(book, path.name)
    reader.read_binaries(root)
    reader.read_description(root)
    bodies = _children(root, "body")
    if not bodies:
        raise ConversionError("В файле FB2 нет основного текста (элемент body).")
    note_bodies = [b for b in bodies if (b.get("name") or "").strip().lower() in NOTE_BODIES]
    main_bodies = [b for b in bodies if b not in note_bodies]
    reader.collect_note_targets(note_bodies)
    reader.read_notes(note_bodies)
    for body in main_bodies:
        front = reader.read_blocks(
            [child for child in body if _local(child) not in ("title", "section")]
        )
        if front:
            book.chapters.append(Chapter(blocks=front, source=path.name))
        for section in _children(body, "section"):
            reader.read_section(section, 1, book.chapters)
    if not book.chapters:
        raise ConversionError("В файле FB2 не найден текст книги.")
    if not book.meta.title:
        book.meta.title = re.sub(r"\.fb2(\.zip)?$", "", path.name, flags=re.IGNORECASE)
    return book


# --- Запись --------------------------------------------------------------


def _element(parent, tag: str, text: Optional[str] = None, **attrs):
    element = etree.SubElement(parent, f"{{{FB_NS}}}{tag}", attrs)
    if text is not None:
        element.text = clean_text(text)
    return element


def _append_text(parent, text: str) -> None:
    text = clean_text(text).replace("\n", " ").replace("\t", " ")
    if len(parent):
        parent[-1].tail = (parent[-1].tail or "") + text
    else:
        parent.text = (parent.text or "") + text


def _write_runs(parent, runs: list[Run], book: Book) -> None:
    for run in runs:
        if run.note:
            note = book.notes.get(run.note)
            if note:
                link = _element(parent, "a", f"[{note.label}]", type="note")
                link.set(f"{{{XLINK_NS}}}href", f"#note{note.label}")
            continue
        target = parent
        for flag, tag in (
            (run.bold, "strong"),
            (run.italic, "emphasis"),
            (run.strike, "strikethrough"),
            (run.sup, "sup"),
            (run.sub, "sub"),
            (run.code, "code"),
        ):
            if flag:
                target = _element(target, tag)
        if run.href:
            target = _element(target, "a")
            target.set(f"{{{XLINK_NS}}}href", run.href)
        _append_text(target, run.text)


def _write_image(parent, image_id: str, book: Book) -> None:
    image = book.images.get(image_id)
    if image:
        element = _element(parent, "image")
        element.set(f"{{{XLINK_NS}}}href", f"#{image.id}.{image.ext}")


def _write_blocks(section, blocks: list[Block], book: Book) -> None:
    index = 0
    # Эпиграф по схеме идёт сразу после заголовка, до основного текста.
    while index < len(blocks) and isinstance(blocks[index], Para) and blocks[index].kind in ("epigraph", "author"):
        start = index
        while index < len(blocks) and isinstance(blocks[index], Para) and blocks[index].kind in ("epigraph", "author"):
            index += 1
        epigraph = _element(section, "epigraph")
        _write_group(epigraph, blocks[start:index], book)
    wrote_content = False
    while index < len(blocks):
        block = blocks[index]
        if isinstance(block, Para) and block.kind in ("quote", "epigraph", "author"):
            start = index
            while index < len(blocks) and isinstance(blocks[index], Para) and blocks[index].kind in ("quote", "epigraph", "author"):
                index += 1
            _write_group(_element(section, "cite"), blocks[start:index], book)
        elif isinstance(block, Para) and block.kind == "poem":
            start = index
            while index < len(blocks) and (
                (isinstance(blocks[index], Para) and blocks[index].kind == "poem")
                or (isinstance(blocks[index], Gap) and _next_is_poem(blocks, index))
            ):
                index += 1
            _write_poem(section, blocks[start:index], book)
        else:
            _write_block(section, block, book)
            index += 1
        wrote_content = True
    if not wrote_content:
        _element(section, "empty-line")


def _next_is_poem(blocks: list[Block], index: int) -> bool:
    following = blocks[index + 1] if index + 1 < len(blocks) else None
    return isinstance(following, Para) and following.kind == "poem"


def _write_group(parent, blocks: list[Block], book: Book) -> None:
    for block in blocks:
        tag = "text-author" if block.kind == "author" else "p"
        _write_runs(_element(parent, tag), block.runs, book)
    # По схеме text-author идёт в конце, а внутри должен быть хотя бы один p.
    if not any(_local(child) == "p" for child in parent):
        for child in parent:
            child.tag = f"{{{FB_NS}}}p"


def _write_poem(section, blocks: list[Block], book: Book) -> None:
    poem = _element(section, "poem")
    stanza = _element(poem, "stanza")
    for block in blocks:
        if isinstance(block, Gap):
            if len(stanza):
                stanza = _element(poem, "stanza")
        else:
            _write_runs(_element(stanza, "v"), block.runs, book)


def _write_block(section, block: Block, book: Book) -> None:
    if isinstance(block, Para):
        if block.kind == "subtitle":
            _write_runs(_element(section, "subtitle"), block.runs, book)
        elif block.kind == "code":
            code = _element(_element(section, "p"), "code")
            _append_text(code, plain_text(block.runs))
        else:
            _write_runs(_element(section, "p"), block.runs, book)
    elif isinstance(block, ImageBlock):
        _write_image(section, block.image_id, book)
    elif isinstance(block, Gap):
        _element(section, "empty-line")
    elif isinstance(block, ListBlock):
        for number, item in enumerate(block.items, 1):
            prefix = f"{number}. " if block.ordered else "• "
            _write_runs(_element(section, "p"), [Run(prefix)] + item, book)
    elif isinstance(block, TableBlock):
        table = _element(section, "table")
        for row_index, row in enumerate(block.rows):
            row_element = _element(table, "tr")
            for cell in row:
                tag = "th" if block.header and row_index == 0 else "td"
                _write_runs(_element(row_element, tag), cell, book)


def _write_title(parent, text: str) -> None:
    title = _element(parent, "title")
    _element(title, "p", text)


def _split_author(name: str, info) -> None:
    author = _element(info, "author")
    parts = name.split()
    if len(parts) == 1:
        _element(author, "nickname", parts[0])
    elif len(parts) == 2:
        _element(author, "first-name", parts[0])
        _element(author, "last-name", parts[1])
    else:
        _element(author, "first-name", parts[0])
        _element(author, "middle-name", " ".join(parts[1:-1]))
        _element(author, "last-name", parts[-1])


def to_bytes(source: Book) -> bytes:
    book = prepare_for_write(source)
    root = etree.Element(f"{{{FB_NS}}}FictionBook", nsmap={None: FB_NS, "l": XLINK_NS})
    description = _element(root, "description")
    info = _element(description, "title-info")
    for genre in book.meta.genres or ["prose"]:
        _element(info, "genre", genre)
    for name in book.meta.authors or []:
        _split_author(name, info)
    if not book.meta.authors:
        _element(_element(info, "author"), "nickname", "Неизвестный автор")
    _element(info, "book-title", book_title(book))
    if book.meta.annotation:
        annotation = _element(info, "annotation")
        for line in book.meta.annotation.splitlines():
            if line.strip():
                _element(annotation, "p", line.strip())
    if book.meta.date:
        _element(info, "date", book.meta.date)
    if book.meta.cover_id:
        _write_image(_element(info, "coverpage"), book.meta.cover_id, book)
    _element(info, "lang", (book.meta.lang or "ru")[:2])
    if book.meta.series:
        attrs = {"name": book.meta.series}
        if book.meta.series_number.isdigit():
            attrs["number"] = book.meta.series_number
        _element(info, "sequence", **attrs)
    document = _element(description, "document-info")
    _element(_element(document, "author"), "nickname", APP_NAME)
    _element(document, "program-used", f"{APP_NAME} {__version__}")
    today = dt.date.today().isoformat()
    _element(document, "date", today, value=today)
    _element(document, "id", uuid.uuid4().hex)
    _element(document, "version", "1.0")
    if book.meta.publisher:
        _element(_element(description, "publish-info"), "publisher", book.meta.publisher)

    body = _element(root, "body")
    _write_title(body, book_title(book))
    _write_sections(body, book)

    if book.notes:
        notes_body = _element(root, "body", name="notes")
        _write_title(notes_body, "Примечания")
        for note in book.notes.values():
            section = _element(notes_body, "section", id=f"note{note.label}")
            _write_title(section, note.label)
            _write_blocks(section, note.blocks or [Gap()], book)

    for image in book.images.values():
        binary = _element(root, "binary", id=f"{image.id}.{image.ext}")
        binary.set("content-type", image.mime)
        binary.text = base64.b64encode(image.data).decode("ascii")

    return etree.tostring(root, xml_declaration=True, encoding="utf-8", pretty_print=True)


def _write_sections(body, book: Book) -> None:
    stack: list[tuple[int, object]] = [(0, body)]
    chapters = book.chapters
    for index, chapter in enumerate(chapters):
        while stack[-1][0] >= chapter.level:
            stack.pop()
        section = _element(stack[-1][1], "section")
        if chapter.title:
            _write_title(section, chapter.title)
        following = chapters[index + 1] if index + 1 < len(chapters) else None
        has_children = following is not None and following.level > chapter.level
        if has_children:
            # По схеме секция содержит либо текст, либо подсекции.
            if chapter.blocks:
                _write_blocks(_element(section, "section"), chapter.blocks, book)
        else:
            _write_blocks(section, chapter.blocks, book)
        stack.append((chapter.level, section))


def write(book: Book, path: Path, options: Optional[dict] = None, progress=None) -> None:
    path.write_bytes(to_bytes(book))


def write_zip(book: Book, path: Path, options: Optional[dict] = None, progress=None) -> None:
    inner = re.sub(r"\.zip$", "", path.name, flags=re.IGNORECASE)
    if not inner.lower().endswith(".fb2"):
        inner += ".fb2"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(inner, to_bytes(book))
