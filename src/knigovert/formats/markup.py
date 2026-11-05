"""HTML и Markdown: общий разбор HTML используется также для EPUB."""

from __future__ import annotations

import base64
import binascii
import html as html_lib
import re
import urllib.parse
from pathlib import Path
from typing import Callable, Optional

import warnings

from bs4 import BeautifulSoup, NavigableString, Tag, XMLParsedAsHTMLWarning
from markdown_it import MarkdownIt
from mdit_py_plugins.footnote import footnote_plugin

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

# XHTML из EPUB специально разбираем терпимым HTML-парсером: он прощает ошибки разметки.
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

_COMMON_CYRILLIC = frozenset("оеаинтсрвлкмдпуяыьгзбчйхжшюцщэфъё")
BLOCK_TAGS = {
    "p", "div", "section", "article", "aside", "header", "footer", "main", "figure", "figcaption",
    "blockquote", "pre", "ul", "ol", "dl", "dt", "dd", "table", "hr", "h1", "h2", "h3", "h4", "h5",
    "h6", "center", "address", "details", "summary", "body", "html", "li", "tbody", "thead", "tr",
}
SKIP_TAGS = {"script", "style", "head", "nav", "template", "noscript", "svg", "object", "embed", "iframe", "form", "button"}
HEADING_RE = re.compile(r"^h([1-6])$")
_WS_RE = re.compile(r"[ \t\r\n\f]+")
_NOTE_LABEL_RE = re.compile(r"^[\[\(\{]?\s*(\d{1,4}|\*{1,4}|†|‡)\s*[\]\)\}]?\.?$")
NOTE_TITLES = {"примечания", "сноски", "комментарии", "notes", "footnotes", "endnotes", "ескертпелер"}

ImageResolver = Callable[[str], Optional[bytes]]


def _is_block(node) -> bool:
    return isinstance(node, Tag) and (node.name in BLOCK_TAGS or bool(HEADING_RE.match(node.name)))


def _epub_type(tag: Tag) -> str:
    return " ".join(
        str(value) for key, value in tag.attrs.items() if key.lower() in ("epub:type", "type", "role")
    ).lower()


def _style_flags(tag: Tag) -> dict:
    style = str(tag.get("style", "")).lower().replace(" ", "")
    flags = {}
    if re.search(r"font-weight:(bold|[6-9]00)", style):
        flags["bold"] = True
    if "font-style:italic" in style or "font-style:oblique" in style:
        flags["italic"] = True
    if "line-through" in style:
        flags["strike"] = True
    if "vertical-align:super" in style:
        flags["sup"] = True
    if "vertical-align:sub" in style:
        flags["sub"] = True
    return flags


class HtmlConverter:
    """Превращает дерево HTML в блоки модели."""

    def __init__(self, book: Book, resolve_image: Optional[ImageResolver] = None):
        self.book = book
        self.resolve_image = resolve_image
        # Ссылка на сноску: исходный адрес → id сноски в модели.
        self.note_links: dict[str, str] = {}
        self.base = ""

    # --- Сноски ----------------------------------------------------------

    def is_note_link(self, link: Tag) -> bool:
        href = str(link.get("href", ""))
        if "#" not in href or re.match(r"(?i)[a-z]+:", href):
            return False
        kind = _epub_type(link)
        if "noteref" in kind or "doc-noteref" in kind:
            return True
        if "footnote-ref" in " ".join(link.parent.get("class", []) if link.parent else []):
            return True
        return bool(_NOTE_LABEL_RE.match(link.get_text().strip()))

    @staticmethod
    def note_container(target: Tag) -> Tag:
        """Элемент сноски: якорь часто лежит внутри абзаца или пункта списка."""
        node = target
        while node.parent is not None and node.name not in ("li", "aside", "p", "div", "section", "dd", "tr"):
            node = node.parent
        return node

    def extract_notes(self, documents: list[tuple[str, BeautifulSoup]]) -> None:
        """Находит сноски, забирает их текст и убирает из основного потока."""
        anchors: dict[str, Tag] = {}
        for name, soup in documents:
            for tag in soup.find_all(id=True):
                anchors[f"{name}#{tag['id']}"] = tag
            for tag in soup.find_all("a", attrs={"name": True}):
                anchors.setdefault(f"{name}#{tag['name']}", tag)
        taken: list[tuple[str, Tag]] = []
        for name, soup in documents:
            for link in soup.find_all("a", href=True):
                if not self.is_note_link(link):
                    continue
                key = self._resolve_href(name, str(link["href"]))
                target = anchors.get(key)
                if target is None:
                    continue
                container = self.note_container(target)
                if container.name in ("body", "html"):
                    continue
                # Ссылка и сноска в одном абзаце — значит, это обратная ссылка.
                if any(parent is container for parent in link.parents):
                    continue
                note_id = f"h:{key}"
                link["data-note"] = note_id
                if note_id not in self.book.notes:
                    self.book.notes[note_id] = Note(note_id)
                    taken.append((note_id, container))
        for note_id, container in taken:
            for back in container.find_all("a", href=True):
                text = back.get_text().strip()
                kind = _epub_type(back)
                if "backlink" in kind or text in ("↩", "↩︎", "↑", "^") or "footnote-backref" in " ".join(back.get("class", [])):
                    back.decompose()
                elif _NOTE_LABEL_RE.match(text) and not back.has_attr("data-note"):
                    back.decompose()
            blocks = self.blocks(container)
            self.book.notes[note_id].blocks = _strip_note_label(blocks)
        for _, container in taken:
            if container.parent is not None:
                container.decompose()
        # После изъятия сносок могут остаться пустые заголовки и списки.
        for name, soup in documents:
            for tag in soup.find_all(["ol", "ul", "section", "div", "aside"]):
                if tag.decomposed:
                    continue
                if not tag.get_text(strip=True) and not tag.find("img"):
                    tag.decompose()

    @staticmethod
    def _resolve_href(document: str, href: str) -> str:
        path, _, fragment = href.partition("#")
        path = urllib.parse.unquote(path)
        if not path:
            return f"{document}#{fragment}"
        base = document.rsplit("/", 1)[0] + "/" if "/" in document else ""
        joined = _normalize_path(base + path)
        return f"{joined}#{fragment}"

    # --- Текст -----------------------------------------------------------

    def inline(self, node, runs: list[Run], style: dict, preserve: bool = False) -> None:
        if isinstance(node, NavigableString):
            if type(node).__name__ in ("Comment", "Doctype", "CData", "ProcessingInstruction", "Declaration"):
                return
            text = clean_text(str(node))
            if not preserve:
                text = _WS_RE.sub(" ", text)
            if text:
                add_run(runs, Run(text, **style))
            return
        if not isinstance(node, Tag) or node.name in SKIP_TAGS:
            return
        name = node.name
        if name == "br":
            add_run(runs, Run("\n", **style))
            return
        if name == "img":
            return
        if name == "a" and node.has_attr("data-note"):
            runs.append(Run(node.get_text().strip(), note=str(node["data-note"])))
            return
        new_style = {**style, **_style_flags(node)}
        if name in ("b", "strong"):
            new_style["bold"] = True
        elif name in ("i", "em", "cite", "var", "dfn"):
            new_style["italic"] = True
        elif name in ("s", "strike", "del"):
            new_style["strike"] = True
        elif name == "sup":
            new_style["sup"] = True
        elif name == "sub":
            new_style["sub"] = True
        elif name in ("code", "kbd", "samp", "tt"):
            new_style["code"] = True
        elif name == "a":
            href = str(node.get("href", ""))
            if re.match(r"(?i)https?://|mailto:", href):
                new_style["href"] = href
        for child in node.children:
            self.inline(child, runs, new_style, preserve)

    def _para(self, nodes, kind: str, align: Optional[str]) -> Optional[Para]:
        runs: list[Run] = []
        for node in nodes:
            self.inline(node, runs, {})
        runs = trim_runs(_collapse_edges(runs))
        return Para(runs, kind, align) if runs else None

    def image(self, tag: Tag) -> Optional[ImageBlock]:
        src = str(tag.get("src") or tag.get("data-src") or "")
        if not src:
            return None
        data: Optional[bytes] = None
        if src.startswith("data:"):
            data = _decode_data_uri(src)
        elif re.match(r"(?i)https?:|//", src):
            self.book.warn("Картинки по ссылкам из интернета пропущены: приложение работает без сети.")
            return None
        elif self.resolve_image:
            data = self.resolve_image(urllib.parse.unquote(src.split("#")[0].split("?")[0]))
        if not data:
            return None
        image_id = self.book.add_image(data)
        if image_id is None:
            self.book.warn("Часть картинок в неподдерживаемом формате (например, SVG) пропущена.")
            return None
        return ImageBlock(image_id, str(tag.get("alt", "")))

    # --- Блоки -----------------------------------------------------------

    def blocks(self, node: Tag, kind: str = "normal") -> list[Block]:
        result: list[Block] = []
        inline_nodes: list = []

        def flush() -> None:
            if inline_nodes:
                paragraph = self._para(inline_nodes, kind, _align(node))
                if paragraph:
                    result.append(paragraph)
                inline_nodes.clear()

        for child in node.children:
            if isinstance(child, Tag) and child.name in SKIP_TAGS:
                continue
            if isinstance(child, Tag) and child.name == "img":
                flush()
                image = self.image(child)
                if image:
                    result.append(image)
                continue
            if _is_block(child):
                flush()
                result.extend(self._block(child, kind))
            elif isinstance(child, Tag) and child.find("img"):
                flush()
                for image_tag in child.find_all("img"):
                    image = self.image(image_tag)
                    if image:
                        result.append(image)
                inline_nodes.append(child)
                flush()
            else:
                inline_nodes.append(child)
        flush()
        return result

    def _block(self, tag: Tag, kind: str) -> list[Block]:
        name = tag.name
        heading = HEADING_RE.match(name)
        if heading:
            text = _WS_RE.sub(" ", tag.get_text()).strip()
            images = [self.image(item) for item in tag.find_all("img")]
            blocks: list[Block] = [image for image in images if image]
            if text:
                blocks.append(_Heading([Run(clean_text(text))], "normal", None, level=int(heading.group(1))))
            return blocks
        if name == "hr":
            return [Gap()]
        if name == "pre":
            runs: list[Run] = []
            for child in tag.children:
                self.inline(child, runs, {}, preserve=True)
            text = plain_text(runs).strip("\n")
            return [Para([Run(text)], "code")] if text else []
        if name == "blockquote":
            return self.blocks(tag, "quote")
        if name in ("ul", "ol"):
            return self._list(tag)
        if name == "table":
            return self._table(tag)
        if name in ("dt",):
            paragraph = self._para(tag.children, "subtitle", None)
            return [paragraph] if paragraph else []
        classes = " ".join(tag.get("class", [])).lower()
        if name in ("p", "div"):
            if name == "p" and "gap" in classes.split():
                return [Gap()]
            if "epigraph" in classes:
                kind = "epigraph"
            elif "poem" in classes or "stanza" in classes or "verse" in classes:
                kind = "poem"
            elif "subtitle" in classes:
                kind = "subtitle"
            elif "author" in classes:
                kind = "author"
            elif "quote" in classes or "cite" in classes:
                kind = "quote"
        if name == "p" and not any(_is_block(child) for child in tag.children):
            result: list[Block] = []
            for image_tag in tag.find_all("img"):
                image = self.image(image_tag)
                if image:
                    result.append(image)
            paragraph = self._para(tag.children, kind, _align(tag))
            if paragraph:
                result.append(paragraph)
            return result
        return self.blocks(tag, kind)

    def _list(self, tag: Tag) -> list[Block]:
        items: list[list[Run]] = []
        for item in tag.find_all("li", recursive=False):
            runs: list[Run] = []
            for child in item.children:
                if isinstance(child, Tag) and child.name in ("ul", "ol"):
                    continue
                self.inline(child, runs, {})
            runs = trim_runs(_collapse_edges(runs))
            if runs:
                items.append(runs)
            for nested in item.find_all(["ul", "ol"], recursive=False):
                for sub in self._list(nested):
                    if isinstance(sub, ListBlock):
                        for sub_item in sub.items:
                            items.append([Run("– ")] + sub_item)
        return [ListBlock(items, tag.name == "ol")] if items else []

    def _table(self, tag: Tag) -> list[Block]:
        rows: list[list[list[Run]]] = []
        header = False
        for row in tag.find_all("tr"):
            if row.find_parent("table") is not tag:
                continue
            cells = []
            for cell in row.find_all(["td", "th"], recursive=False):
                if not rows and cell.name == "th":
                    header = True
                runs: list[Run] = []
                self.inline(cell, runs, {})
                cells.append(trim_runs(_collapse_edges(runs)))
            if cells:
                rows.append(cells)
        return [TableBlock(rows, header)] if rows else []


class _Heading(Para):
    """Служебный блок: заголовок внутри потока, заменяется на границу главы."""

    def __init__(self, runs, kind, align, level):
        super().__init__(runs, kind, align)
        self.level = level


def _strip_note_label(blocks: list[Block]) -> list[Block]:
    """Убирает номер в начале текста сноски: «1. Текст» → «Текст»."""
    if blocks and isinstance(blocks[0], Para) and blocks[0].runs:
        first = blocks[0].runs[0]
        if first.note is None:
            first.text = re.sub(r"^\s*[\[\(]?\d{1,4}[\]\)\.]?\s+", "", first.text)
    return blocks


def _collapse_edges(runs: list[Run]) -> list[Run]:
    """Убирает двойные пробелы на стыках кусков разного оформления."""
    result: list[Run] = []
    for run in runs:
        if run.note is None and not run.code:
            run.text = re.sub(r" {2,}", " ", run.text)
        if run.note is None and result and result[-1].note is None:
            if result[-1].text.endswith((" ", "\n")) and run.text.startswith(" "):
                run.text = run.text.lstrip(" ")
                if not run.text:
                    continue
        result.append(run)
    return result


def _align(tag: Tag) -> Optional[str]:
    if tag.name == "center":
        return "center"
    style = str(tag.get("style", "")).lower().replace(" ", "")
    value = str(tag.get("align", "")).lower()
    classes = [str(item).lower() for item in tag.get("class", [])]
    if "text-align:center" in style or value == "center" or "center" in classes:
        return "center"
    if "text-align:right" in style or value == "right" or "right" in classes:
        return "right"
    return None


def _decode_data_uri(uri: str) -> Optional[bytes]:
    header, _, payload = uri.partition(",")
    try:
        if ";base64" in header.lower():
            return base64.b64decode(payload + "=" * (-len(payload) % 4))
        return urllib.parse.unquote_to_bytes(payload)
    except (binascii.Error, ValueError):
        return None


def _normalize_path(path: str) -> str:
    parts: list[str] = []
    for part in path.replace("\\", "/").split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        parts.append(part)
    return "/".join(parts)


def decode_text(data: bytes) -> str:
    """Определяет кодировку текстового файла (UTF-8, UTF-16, CP1251, KOI8-R)."""
    if data[:3] == b"\xef\xbb\xbf":
        return data[3:].decode("utf-8", "replace")
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", "replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    # Выбираем кириллическую кодировку, при которой текст больше всего похож на русский:
    # в чужой кодировке буквы становятся заглавными, а часть символов — псевдографикой.
    sample = data[:200000]
    best, best_score = "cp1252", 0
    for encoding in ("cp1251", "koi8_r", "cp866"):
        text = sample.decode(encoding, "replace")
        common = sum(char in _COMMON_CYRILLIC for char in text)
        capitals = sum("А" <= char <= "Я" for char in text)
        symbols = sum(ord(char) > 0x7f and not char.isalpha() and char not in "«»—–…№" for char in text)
        score = common - capitals - 3 * symbols
        if score > best_score:
            best, best_score = encoding, score
    return data.decode(best, "replace")


def heading_ranks(blocks: list[Block]) -> dict[int, int]:
    """Сквозные уровни заголовков: h2 и h4 без h3 — это уровни 1 и 2."""
    levels = sorted({block.level for block in blocks if isinstance(block, _Heading)})
    return {level: index + 1 for index, level in enumerate(levels)}


def blocks_to_chapters(
    blocks: list[Block],
    source: str,
    fallback_title: str = "",
    rank: Optional[dict[int, int]] = None,
) -> list[Chapter]:
    """Режет поток блоков по заголовкам h1–h6."""
    rank = rank if rank is not None else heading_ranks(blocks)
    titles = {index: rank[block.level] for index, block in enumerate(blocks) if isinstance(block, _Heading)}
    if not titles:
        # В HTML нет заголовков: пробуем найти главы по тексту.
        titles = find_chapter_titles(blocks)
    if not titles:
        return [Chapter(title=fallback_title, blocks=_plain_blocks(blocks), source=source)] if blocks else []
    chapters = split_by_titles(blocks, titles, source)
    for chapter in chapters:
        chapter.blocks = _plain_blocks(chapter.blocks)
    return chapters


def _plain_blocks(blocks: list[Block]) -> list[Block]:
    return [Para(block.runs, "subtitle") if isinstance(block, _Heading) else block for block in blocks]


def _html_title(soup: BeautifulSoup) -> str:
    tag = soup.find("title")
    return _WS_RE.sub(" ", tag.get_text()).strip() if tag else ""


def _read_html_string(data: bytes, path: Path, book: Book) -> None:
    soup = BeautifulSoup(decode_text(data) if not data.lstrip().startswith(b"<?xml") else data, "lxml")
    base_dir = path.parent.resolve()

    def resolve(src: str) -> Optional[bytes]:
        candidate = (base_dir / src).resolve()
        try:
            candidate.relative_to(base_dir)
        except ValueError:
            return None
        return candidate.read_bytes() if candidate.is_file() else None

    converter = HtmlConverter(book, resolve)
    converter.extract_notes([(path.name, soup)])
    for tag in soup.find_all("meta"):
        name = str(tag.get("name", "")).lower()
        content = str(tag.get("content", "")).strip()
        if name == "author" and content:
            book.meta.authors = [content]
        elif name == "description" and content:
            book.meta.annotation = content
    html_tag = soup.find("html")
    if html_tag and html_tag.get("lang"):
        book.meta.lang = str(html_tag["lang"])[:2]
    book.meta.title = _html_title(soup)
    body = soup.find("body") or soup
    blocks = converter.blocks(body)
    book.chapters = blocks_to_chapters(blocks, path.name)
    drop_notes_heading(book)
    _lift_title(book)


def drop_notes_heading(book: Book) -> None:
    """После изъятия сносок от их раздела остаётся пустой заголовок."""
    if book.notes:
        book.chapters = [
            chapter
            for chapter in book.chapters
            if chapter.blocks or chapter.title.strip().lower() not in NOTE_TITLES
        ]


def _is_front_block(block: Block) -> bool:
    """Обложка и строка с автором под названием книги."""
    if isinstance(block, ImageBlock):
        return True
    if not isinstance(block, Para):
        return False
    text = plain_text(block.runs)
    if len(text) > 120:
        return False
    return block.kind == "author" or all(run.italic for run in block.runs if run.text.strip())


def _lift_title(book: Book) -> None:
    """Название книги, автора и обложку в начале файла переносим в описание."""
    chapters = book.chapters
    # Картинка перед названием — это обложка.
    if (
        len(chapters) > 1
        and not chapters[0].title
        and len(chapters[0].blocks) == 1
        and isinstance(chapters[0].blocks[0], ImageBlock)
    ):
        if not book.meta.cover_id:
            book.meta.cover_id = chapters[0].blocks[0].image_id
        chapters.pop(0)
    # Единственный заголовок на весь файл — это название книги, а не глава.
    if len(chapters) == 1 and chapters[0].title and chapters[0].title == (book.meta.title or chapters[0].title):
        only = chapters[0]
        book.meta.title = only.title
        only.title = ""
        if only.blocks and not book.meta.authors and _is_front_block(only.blocks[0]) and not isinstance(only.blocks[0], ImageBlock):
            names = re.split(r"[,;]", plain_text(only.blocks[0].runs))
            book.meta.authors = [name.strip() for name in names if name.strip()]
            only.blocks.pop(0)
        return
    if len(chapters) < 2 or not chapters[0].title:
        return
    first = chapters[0]
    # Заголовок совпадает с названием книги или стоит уровнем выше всех глав.
    same_as_title = first.title == book.meta.title
    if not same_as_title and first.level >= chapters[1].level:
        return
    if not all(_is_front_block(block) for block in first.blocks):
        return
    if book.meta.title and not same_as_title:
        return
    book.meta.title = first.title
    for block in first.blocks:
        if isinstance(block, ImageBlock):
            book.meta.cover_id = book.meta.cover_id or block.image_id
        elif not book.meta.authors:
            names = re.split(r"[,;]", plain_text(block.runs))
            book.meta.authors = [name.strip() for name in names if name.strip()]
    chapters.pop(0)
    top = min(chapter.level for chapter in chapters)
    for chapter in chapters:
        chapter.level = chapter.level - top + 1


def read_html(path: Path, progress=None) -> Book:
    book = Book()
    _read_html_string(path.read_bytes(), path, book)
    if not book.chapters:
        raise ConversionError("В HTML-файле не найден текст.")
    book.meta.title = book.meta.title or path.stem
    return book


def read_markdown(path: Path, progress=None) -> Book:
    source = decode_text(path.read_bytes())
    # HTML внутри Markdown безопасен: он не показывается как есть, а разбирается в модель книги.
    renderer = MarkdownIt("commonmark").enable(["table", "strikethrough"]).use(footnote_plugin)
    html = renderer.render(source)
    book = Book()
    document = f"<html><body>{html}</body></html>".encode("utf-8")
    _read_html_string(document, path, book)
    if not book.chapters:
        raise ConversionError("В файле Markdown не найден текст.")
    book.meta.title = book.meta.title or path.stem
    return book


# --- Запись HTML ---------------------------------------------------------


def esc(text: str) -> str:
    return html_lib.escape(clean_text(text), quote=True)


def safe_href(href: str) -> Optional[str]:
    return href if re.match(r"(?i)^(https?://|mailto:)", href or "") else None


def runs_to_html(runs: list[Run], book: Book, note_href: Callable[[str], str]) -> str:
    parts = []
    for run in runs:
        if run.note:
            note = book.notes.get(run.note)
            if note:
                href = esc(note_href(note.label))
                parts.append(
                    f'<a class="noteref" id="ref{note.label}" href="{href}" '
                    f'epub:type="noteref"><sup>{esc(note.label)}</sup></a>'
                )
            continue
        text = esc(run.text).replace("\n", "<br/>")
        if run.code:
            text = f"<code>{text}</code>"
        if run.sub:
            text = f"<sub>{text}</sub>"
        if run.sup:
            text = f"<sup>{text}</sup>"
        if run.strike:
            text = f"<s>{text}</s>"
        if run.italic:
            text = f"<em>{text}</em>"
        if run.bold:
            text = f"<strong>{text}</strong>"
        href = safe_href(run.href or "")
        if href:
            text = f'<a href="{esc(href)}">{text}</a>'
        parts.append(text)
    return "".join(parts)


def blocks_to_html(
    blocks: list[Block],
    book: Book,
    image_src: Callable[[str], str],
    note_href: Callable[[str], str],
    numbered_from: Optional[int] = None,
) -> list[str]:
    """Блоки → XHTML-совместимые строки. Весь текст экранируется."""
    lines: list[str] = []
    for position, block in enumerate(blocks):
        data = "" if numbered_from is None else f' data-block="{numbered_from + position}"'
        if isinstance(block, Para):
            body = runs_to_html(block.runs, book, note_href)
            if block.kind == "code":
                lines.append(f"<pre{data}>{esc(plain_text(block.runs))}</pre>")
            elif block.kind == "subtitle":
                lines.append(f'<p class="subtitle"{data}>{body}</p>')
            elif block.kind == "normal":
                align = f' class="{block.align}"' if block.align else ""
                lines.append(f"<p{align}{data}>{body}</p>")
            else:
                lines.append(f'<p class="{block.kind}"{data}>{body}</p>')
        elif isinstance(block, ImageBlock):
            image = book.images.get(block.image_id)
            if image:
                lines.append(
                    f'<p class="image"{data}><img src="{esc(image_src(image.id))}" alt="{esc(block.alt)}"/></p>'
                )
        elif isinstance(block, Gap):
            lines.append(f'<p class="gap"{data}>&#160;</p>')
        elif isinstance(block, ListBlock):
            tag = "ol" if block.ordered else "ul"
            items = "".join(f"<li>{runs_to_html(item, book, note_href)}</li>" for item in block.items)
            lines.append(f"<{tag}{data}>{items}</{tag}>")
        elif isinstance(block, TableBlock):
            rows = []
            for index, row in enumerate(block.rows):
                tag = "th" if block.header and index == 0 else "td"
                rows.append(
                    "<tr>" + "".join(f"<{tag}>{runs_to_html(cell, book, note_href)}</{tag}>" for cell in row) + "</tr>"
                )
            lines.append(f"<table{data}>{''.join(rows)}</table>")
    return lines


BOOK_CSS = """
body { font-family: Georgia, "PT Serif", "Times New Roman", serif; line-height: 1.5; margin: 0 auto; max-width: 42em; padding: 1.5em; color: #1d1b18; }
h1, h2, h3, h4, h5, h6 { line-height: 1.25; margin: 2em 0 .8em; }
h1 { font-size: 1.8em; } h2 { font-size: 1.4em; } h3 { font-size: 1.2em; }
p { margin: 0 0 .4em; text-indent: 1.4em; }
p.center { text-align: center; text-indent: 0; } p.right { text-align: right; text-indent: 0; }
p.subtitle { font-weight: bold; text-align: center; text-indent: 0; margin: 1.2em 0 .6em; }
p.quote { margin-left: 2em; text-indent: 0; font-style: italic; }
p.epigraph { margin-left: 35%; text-indent: 0; font-style: italic; font-size: .95em; }
p.author { text-align: right; text-indent: 0; font-style: italic; }
p.poem { margin-left: 2.5em; text-indent: 0; margin-bottom: 0; }
p.gap { text-indent: 0; }
p.image { text-align: center; text-indent: 0; margin: 1em 0; }
img { max-width: 100%; height: auto; }
pre { white-space: pre-wrap; font-size: .9em; background: rgba(127,127,127,.1); padding: .7em; }
code { font-size: .92em; }
table { border-collapse: collapse; margin: 1em 0; width: 100%; }
td, th { border: 1px solid #999; padding: .3em .5em; vertical-align: top; }
a.noteref { text-decoration: none; }
.book-title { text-align: center; margin-top: 2em; }
.book-author { text-align: center; text-indent: 0; font-style: italic; }
.toc { margin: 2em 0; } .toc ul { list-style: none; padding-left: 1.2em; } .toc > ul { padding-left: 0; }
.notes { margin-top: 3em; border-top: 1px solid #999; padding-top: 1em; font-size: .92em; }
.notes li > p { text-indent: 0; }
.cover { text-align: center; text-indent: 0; }
""".strip()


def _data_uri(book: Book, image_id: str) -> str:
    image = book.images[image_id]
    return f"data:{image.mime};base64,{base64.b64encode(image.data).decode('ascii')}"


def _note_blocks_html(note: Note, book: Book, image_src, note_href) -> str:
    return "".join(blocks_to_html(note.blocks, book, image_src, note_href))


def write_html(source: Book, path: Path, options: Optional[dict] = None, progress=None) -> None:
    options = options or {}
    book = prepare_for_write(source)
    image_src = lambda image_id: _data_uri(book, image_id)
    note_href = lambda label: f"#note{label}"
    title = book_title(book)
    body: list[str] = []
    if book.meta.cover_id:
        body.append(f'<p class="cover"><img src="{_data_uri(book, book.meta.cover_id)}" alt=""/></p>')
    body.append(f'<h1 class="book-title">{esc(title)}</h1>')
    if book.meta.authors:
        body.append(f'<p class="book-author">{esc(", ".join(book.meta.authors))}</p>')
    if options.get("toc", True) and len(book.chapters) > 1:
        body.append(_toc_html(book))
    for index, chapter in enumerate(book.chapters):
        level = min(chapter.level + 1, 6)
        if chapter.title:
            body.append(f'<h{level} id="ch{index}">{esc(chapter.title)}</h{level}>')
        body.extend(blocks_to_html(chapter.blocks, book, image_src, note_href))
    if book.notes:
        body.append('<section class="notes"><h2>Примечания</h2><ol>')
        for note in book.notes.values():
            inner = _note_blocks_html(note, book, image_src, note_href)
            body.append(f'<li id="note{note.label}">{inner}<p><a href="#ref{note.label}">↩</a></p></li>')
        body.append("</ol></section>")
    lang = esc((book.meta.lang or "ru")[:2])
    author_meta = f'<meta name="author" content="{esc(", ".join(book.meta.authors))}"/>' if book.meta.authors else ""
    document = (
        f'<!doctype html>\n<html lang="{lang}" xmlns:epub="http://www.idpf.org/2007/ops"><head>'
        f'<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>'
        f"<title>{esc(title)}</title>{author_meta}<style>{BOOK_CSS}</style></head>\n<body>\n"
        + "\n".join(body)
        + "\n</body></html>\n"
    )
    path.write_text(document, encoding="utf-8")


def _toc_html(book: Book) -> str:
    parts = ['<nav class="toc"><h2>Оглавление</h2>']
    depth = 0
    for index, chapter in enumerate(book.chapters):
        if not chapter.title:
            continue
        while depth < chapter.level:
            parts.append("<ul>")
            depth += 1
        while depth > chapter.level:
            parts.append("</ul>")
            depth -= 1
        parts.append(f'<li><a href="#ch{index}">{esc(chapter.title)}</a></li>')
    parts.append("</ul>" * depth)
    parts.append("</nav>")
    return "".join(parts)


# --- Запись Markdown -----------------------------------------------------

_MD_ESCAPE_RE = re.compile(r"([\\`*_\[\]<>~|])")


def _md_escape(text: str) -> str:
    text = _MD_ESCAPE_RE.sub(r"\\\1", clean_text(text))
    return re.sub(r"^(\s*)(#{1,6}\s|\d+[.)]\s|[-+]\s|>)", lambda m: m.group(1) + "\\" + m.group(2), text)


def _md_runs(runs: list[Run], book: Book) -> str:
    parts = []
    for run in runs:
        if run.note:
            note = book.notes.get(run.note)
            if note:
                parts.append(f"[^{note.label}]")
            continue
        text = run.text
        if run.code:
            parts.append(f"`{text}`")
            continue
        lead = text[: len(text) - len(text.lstrip())]
        tail = text[len(text.rstrip()) :] if text.strip() else ""
        core = _md_escape(text.strip()) if text.strip() else ""
        if not core:
            parts.append(text.replace("\n", "  \n"))
            continue
        core = core.replace("\n", "  \n")
        if run.strike:
            core = f"~~{core}~~"
        if run.italic:
            core = f"*{core}*"
        if run.bold:
            core = f"**{core}**"
        if run.sup:
            core = f"<sup>{core}</sup>"
        if run.sub:
            core = f"<sub>{core}</sub>"
        if safe_href(run.href or ""):
            core = f"[{core}]({run.href})"
        parts.append(lead + core + tail)
    return "".join(parts)


def _md_blocks(blocks: list[Block], book: Book, image_path: Callable[[str], str]) -> list[str]:
    lines: list[str] = []
    poem: list[str] = []

    def flush_poem() -> None:
        # Строки строфы идут одним абзацем с принудительными разрывами.
        if poem:
            lines.append("  \n".join(poem))
            poem.clear()

    for block in blocks:
        if isinstance(block, Para) and block.kind == "poem":
            poem.append(_md_runs(block.runs, book))
            continue
        was_poem = bool(poem)
        flush_poem()
        if isinstance(block, Para):
            text = _md_runs(block.runs, book)
            if block.kind == "code":
                lines.append(f"```\n{plain_text(block.runs)}\n```")
            elif block.kind == "subtitle":
                lines.append(f"**{text}**" if not text.startswith("**") else text)
            elif block.kind in ("quote", "epigraph", "author"):
                lines.append("> " + text.replace("\n", "\n> "))
            else:
                lines.append(text)
        elif isinstance(block, Gap):
            if not was_poem:
                lines.append("* * *")
        elif isinstance(block, ImageBlock):
            image = book.images.get(block.image_id)
            if image:
                lines.append(f"![{_md_escape(block.alt)}]({image_path(image.id)})")
        elif isinstance(block, ListBlock):
            items = []
            for number, item in enumerate(block.items, 1):
                marker = f"{number}." if block.ordered else "-"
                items.append(f"{marker} {_md_runs(item, book)}")
            lines.append("\n".join(items))
        elif isinstance(block, TableBlock):
            rows = [
                "| " + " | ".join(_md_runs(cell, book).replace("\n", " ") for cell in row) + " |"
                for row in block.rows
            ]
            width = max(len(row) for row in block.rows)
            rows.insert(1, "| " + " | ".join(["---"] * width) + " |")
            lines.append("\n".join(rows))
    flush_poem()
    return lines


def write_markdown(source: Book, path: Path, options: Optional[dict] = None, progress=None) -> None:
    book = prepare_for_write(source)
    image_dir = path.with_name(f"{path.stem}_images")
    written: set[str] = set()

    def image_path(image_id: str) -> str:
        image = book.images[image_id]
        name = f"{image.id}.{image.ext}"
        if image_id not in written:
            image_dir.mkdir(exist_ok=True)
            (image_dir / name).write_bytes(image.data)
            written.add(image_id)
        return f"{urllib.parse.quote(image_dir.name)}/{name}"

    title = book_title(book)
    parts = [f"# {_md_escape(title)}"]
    if book.meta.authors:
        parts.append(f"*{_md_escape(', '.join(book.meta.authors))}*")
    if book.meta.cover_id:
        parts.append(f"![]({image_path(book.meta.cover_id)})")
    for chapter in book.chapters:
        if chapter.title:
            parts.append(f"{'#' * min(chapter.level + 1, 6)} {_md_escape(chapter.title)}")
        parts.extend(_md_blocks(chapter.blocks, book, image_path))
    for note in book.notes.values():
        inner = _md_blocks(note.blocks, book, image_path)
        text = "\n\n    ".join(inner) if inner else " "
        parts.append(f"[^{note.label}]: {text}")
    path.write_text("\n\n".join(parts) + "\n", encoding="utf-8")
