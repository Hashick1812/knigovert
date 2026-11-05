"""Чтение и запись EPUB 3 (с совместимым оглавлением для EPUB 2)."""

from __future__ import annotations

import datetime as dt
import posixpath
import re
import urllib.parse
import uuid
import zipfile
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup
from lxml import etree

from ..model import Book, Chapter, ConversionError, ImageBlock, book_title, prepare_for_write
from .markup import (
    BOOK_CSS,
    HtmlConverter,
    _Heading,
    _lift_title,
    _normalize_path,
    blocks_to_chapters,
    blocks_to_html,
    drop_notes_heading,
    esc,
    heading_ranks,
)

_PARSER = dict(recover=True, resolve_entities=False, no_network=True, huge_tree=True)
_XHTML_TYPES = ("application/xhtml+xml", "text/html", "application/x-dtbook+xml")


def _xml(data: bytes):
    return etree.fromstring(data, etree.XMLParser(**_PARSER))


def _local(element) -> str:
    return element.tag.rsplit("}", 1)[-1].lower() if isinstance(element.tag, str) else ""


def _zip_join(base: str, href: str) -> str:
    href = urllib.parse.unquote(href.split("#")[0])
    return _normalize_path(posixpath.join(posixpath.dirname(base), href))


class _Package:
    def __init__(self, archive: zipfile.ZipFile):
        self.archive = archive
        self.names = {name.lower(): name for name in archive.namelist()}
        container = _xml(self.read("META-INF/container.xml"))
        rootfile = next((el for el in container.iter() if _local(el) == "rootfile"), None)
        if rootfile is None:
            raise ConversionError("EPUB повреждён: не найден файл описания книги.")
        self.opf_path = rootfile.get("full-path", "")
        self.opf = _xml(self.read(self.opf_path))
        self.manifest: dict[str, dict] = {}
        for item in self.opf.iter():
            if _local(item) == "item" and item.get("href"):
                self.manifest[item.get("id", "")] = {
                    "path": _zip_join(self.opf_path, item.get("href")),
                    "type": (item.get("media-type") or "").lower(),
                    "props": item.get("properties") or "",
                }

    def read(self, name: str) -> bytes:
        real = self.names.get(name.lower())
        if real is None:
            raise ConversionError(f"EPUB повреждён: нет файла {name}.")
        return self.archive.read(real)

    def exists(self, name: str) -> bool:
        return name.lower() in self.names

    def spine(self) -> list[str]:
        paths = []
        for element in self.opf.iter():
            if _local(element) == "itemref":
                item = self.manifest.get(element.get("idref", ""))
                if item and item["type"] in _XHTML_TYPES:
                    paths.append(item["path"])
        return paths

    def toc_titles(self) -> dict[str, str]:
        """Названия глав из оглавления: путь файла → заголовок."""
        titles: dict[str, str] = {}
        for item in self.manifest.values():
            if "nav" in item["props"].split():
                soup = BeautifulSoup(self.read(item["path"]), "lxml")
                for link in soup.find_all("a", href=True):
                    target = _zip_join(item["path"], str(link["href"]))
                    title = " ".join(link.get_text().split())
                    if title:
                        titles.setdefault(target, title)
                return titles
        for item in self.manifest.values():
            if item["type"] == "application/x-dtbncx+xml":
                root = _xml(self.read(item["path"]))
                for point in root.iter():
                    if _local(point) != "navpoint":
                        continue
                    label = next((el for el in point.iter() if _local(el) == "text"), None)
                    content = next((el for el in point if _local(el) == "content"), None)
                    if label is not None and content is not None and content.get("src"):
                        title = " ".join("".join(label.itertext()).split())
                        if title:
                            titles.setdefault(_zip_join(item["path"], content.get("src")), title)
        return titles


def _read_meta(package: _Package, book: Book) -> None:
    meta = book.meta
    cover_id = None
    for element in package.opf.iter():
        name = _local(element)
        text = " ".join("".join(element.itertext()).split()) if isinstance(element.tag, str) else ""
        if name == "title" and not meta.title:
            meta.title = text
        elif name == "creator" and text:
            meta.authors.append(text)
        elif name == "language" and not meta.lang:
            meta.lang = text[:2]
        elif name == "description" and not meta.annotation:
            meta.annotation = BeautifulSoup(text, "lxml").get_text(" ", strip=True) if text else ""
        elif name == "publisher":
            meta.publisher = text
        elif name == "date" and not meta.date:
            meta.date = text[:10]
        elif name == "subject" and text:
            meta.genres.append(text)
        elif name == "meta" and element.get("name") == "cover":
            cover_id = element.get("content")
    cover = package.manifest.get(cover_id or "")
    if cover is None:
        cover = next((item for item in package.manifest.values() if "cover-image" in item["props"].split()), None)
    if cover and package.exists(cover["path"]):
        meta.cover_id = book.add_image(package.read(cover["path"]))


def read(path: Path, progress=None) -> Book:
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as error:
        raise ConversionError("Файл EPUB повреждён и не открывается.") from error
    with archive:
        package = _Package(archive)
        book = Book()
        _read_meta(package, book)
        toc = package.toc_titles()
        documents = []
        for name in package.spine():
            if package.exists(name):
                documents.append((name, BeautifulSoup(package.read(name), "lxml")))
        if not documents:
            raise ConversionError("В EPUB не найден текст книги.")

        current = {"name": ""}

        def resolve(src: str) -> Optional[bytes]:
            target = _zip_join(current["name"], src)
            return package.read(target) if package.exists(target) else None

        converter = HtmlConverter(book, resolve)
        converter.extract_notes(documents)
        total = len(documents)
        parsed = []
        for index, (name, soup) in enumerate(documents):
            if progress:
                progress("Читаю главы", index / total)
            current["name"] = name
            body = soup.find("body") or soup
            blocks = _drop_cover_page(converter.blocks(body), book)
            if blocks:
                parsed.append((name, blocks))
        # Уровни заголовков считаем по всей книге, а не по каждому файлу отдельно.
        rank = heading_ranks([block for _, blocks in parsed for block in blocks])
        for name, blocks in parsed:
            chapters = blocks_to_chapters(blocks, path.name, toc.get(name, ""), rank)
            if chapters and not chapters[0].title and toc.get(name):
                chapters[0].title = toc[name]
            # У файла без заголовка нет своего уровня: он идёт вровень с предыдущей главой.
            if chapters and not chapters[0].title and book.chapters:
                chapters[0].level = book.chapters[-1].level
            book.chapters.extend(chapters)
    drop_notes_heading(book)
    book.chapters = _drop_book_title(book)
    if not book.chapters:
        raise ConversionError("В EPUB не найден текст книги.")
    book.meta.title = book.meta.title or path.stem
    return book


def _drop_cover_page(blocks: list, book: Book) -> list:
    """Страница, где есть только обложка, не считается главой."""
    if len(blocks) == 1 and isinstance(blocks[0], ImageBlock) and blocks[0].image_id == book.meta.cover_id:
        return []
    return blocks


def _drop_book_title(book: Book) -> list[Chapter]:
    """Убирает титульный лист и оглавление, которые мы же и сгенерировали."""
    result = []
    for chapter in book.chapters:
        if chapter.title in ("Оглавление", "Содержание") and all(isinstance(b, _Heading) for b in chapter.blocks):
            continue
        result.append(chapter)
    holder = Book(book.meta, result)
    _lift_title(holder)
    return holder.chapters


# --- Запись --------------------------------------------------------------


def _page(title: str, lang: str, body: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
        f'<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" '
        f'lang="{lang}" xml:lang="{lang}"><head><meta charset="utf-8"/><title>{esc(title)}</title>'
        f'<link rel="stylesheet" type="text/css" href="style.css"/></head><body>\n{body}\n</body></html>\n'
    )


def write(source: Book, path: Path, options: Optional[dict] = None, progress=None) -> None:
    book = prepare_for_write(source)
    title = book_title(book)
    lang = esc((book.meta.lang or "ru")[:2])
    uid = f"urn:uuid:{uuid.uuid4()}"
    modified = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    image_src = lambda image_id: f"images/{image_id}.{book.images[image_id].ext}"
    note_href = lambda label: f"notes.xhtml#note{label}"

    files: list[tuple[str, str, str]] = []  # имя, media-type, свойства
    documents: list[tuple[str, str]] = []

    if book.meta.cover_id:
        cover = book.images[book.meta.cover_id]
        documents.append(
            (
                "cover.xhtml",
                _page(title, lang, f'<p class="cover"><img src="images/{cover.id}.{cover.ext}" alt=""/></p>'),
            )
        )
    head = f'<h1 class="book-title">{esc(title)}</h1>'
    if book.meta.authors:
        head += f'<p class="book-author">{esc(", ".join(book.meta.authors))}</p>'
    documents.append(("title.xhtml", _page(title, lang, head)))

    entries: list[tuple[int, str, str]] = []
    for index, chapter in enumerate(book.chapters, 1):
        name = f"chapter{index:04d}.xhtml"
        level = min(chapter.level + 1, 6)
        lines = []
        if chapter.title:
            lines.append(f"<h{level}>{esc(chapter.title)}</h{level}>")
            # Глава без названия не попадает в оглавление, поэтому уровни выравниваем заново.
            depth = min(chapter.level, (entries[-1][0] + 1) if entries else 1)
            entries.append((depth, chapter.title, name))
        lines.extend(blocks_to_html(chapter.blocks, book, image_src, note_href))
        documents.append((name, _page(chapter.title or title, lang, "\n".join(lines))))
    if not entries:
        entries.append((1, title, "title.xhtml"))
    if book.notes:
        items = []
        for note in book.notes.values():
            inner = "".join(blocks_to_html(note.blocks, book, image_src, lambda label: f"#note{label}"))
            items.append(
                f'<li id="note{note.label}"><aside epub:type="footnote">{inner}</aside></li>'
            )
        documents.append(("notes.xhtml", _page("Примечания", lang, f'<h2>Примечания</h2><ol class="notes">{"".join(items)}</ol>')))
        entries.append((1, "Примечания", "notes.xhtml"))

    manifest = [
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
        '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
        '<item id="css" href="style.css" media-type="text/css"/>',
    ]
    spine = []
    for index, (name, _) in enumerate(documents):
        manifest.append(f'<item id="doc{index}" href="{name}" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="doc{index}"/>')
    for image in book.images.values():
        props = ' properties="cover-image"' if image.id == book.meta.cover_id else ""
        manifest.append(
            f'<item id="img{image.id}" href="images/{image.id}.{image.ext}" media-type="{image.mime}"{props}/>'
        )

    creators = "".join(f"<dc:creator>{esc(author)}</dc:creator>" for author in book.meta.authors)
    extras = ""
    if book.meta.annotation:
        extras += f"<dc:description>{esc(book.meta.annotation)}</dc:description>"
    if book.meta.publisher:
        extras += f"<dc:publisher>{esc(book.meta.publisher)}</dc:publisher>"
    if book.meta.date:
        extras += f"<dc:date>{esc(book.meta.date)}</dc:date>"
    for genre in book.meta.genres:
        extras += f"<dc:subject>{esc(genre)}</dc:subject>"
    if book.meta.cover_id:
        extras += f'<meta name="cover" content="img{book.meta.cover_id}"/>'
    opf = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f'<dc:identifier id="bookid">{uid}</dc:identifier><dc:title>{esc(title)}</dc:title>'
        f'<dc:language>{lang}</dc:language>{creators}{extras}'
        f'<meta property="dcterms:modified">{modified}</meta></metadata>'
        f'<manifest>{"".join(manifest)}</manifest><spine toc="ncx">{"".join(spine)}</spine></package>'
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", zipfile.ZIP_STORED)
        archive.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container version="1.0" '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
            '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
            "</rootfiles></container>",
            zipfile.ZIP_DEFLATED,
        )
        archive.writestr("OEBPS/content.opf", opf, zipfile.ZIP_DEFLATED)
        archive.writestr("OEBPS/style.css", BOOK_CSS, zipfile.ZIP_DEFLATED)
        archive.writestr("OEBPS/nav.xhtml", _nav(entries, lang, title), zipfile.ZIP_DEFLATED)
        archive.writestr("OEBPS/toc.ncx", _ncx(entries, uid, title), zipfile.ZIP_DEFLATED)
        for name, content in documents:
            archive.writestr(f"OEBPS/{name}", content, zipfile.ZIP_DEFLATED)
        for image in book.images.values():
            archive.writestr(f"OEBPS/images/{image.id}.{image.ext}", image.data, zipfile.ZIP_STORED)


def _nav(entries: list[tuple[int, str, str]], lang: str, title: str) -> str:
    parts = []
    depth = 0
    open_item = False
    for level, text, href in entries:
        if level > depth:
            parts.append("<ol>" * (level - depth))
        else:
            if open_item:
                parts.append("</li>")
            parts.append("</ol></li>" * (depth - level))
        depth = level
        parts.append(f'<li><a href="{href}">{esc(text)}</a>')
        open_item = True
    if open_item:
        parts.append("</li>")
    parts.append("</ol>" + "</li></ol>" * (depth - 1))
    body = f'<nav epub:type="toc" id="toc"><h2>Оглавление</h2>{"".join(parts)}</nav>'
    return _page(title, lang, body)


def _ncx(entries: list[tuple[int, str, str]], uid: str, title: str) -> str:
    parts = []
    stack: list[int] = []
    for order, (level, text, href) in enumerate(entries, 1):
        while stack and stack[-1] >= level:
            parts.append("</navPoint>")
            stack.pop()
        parts.append(
            f'<navPoint id="np{order}" playOrder="{order}"><navLabel><text>{esc(text)}</text></navLabel>'
            f'<content src="{href}"/>'
        )
        stack.append(level)
    parts.append("</navPoint>" * len(stack))
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1"><head>'
        f'<meta name="dtb:uid" content="{uid}"/></head><docTitle><text>{esc(title)}</text></docTitle>'
        f'<navMap>{"".join(parts)}</navMap></ncx>'
    )
