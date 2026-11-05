"""Чтение PDF: восстановление абзацев, глав, картинок и таблиц из страниц."""

from __future__ import annotations

import collections
import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pymupdf

from ..chapters import is_bare_number, is_chapter_title
from ..model import (
    Block,
    Book,
    Chapter,
    ConversionError,
    ImageBlock,
    Para,
    PdfRange,
    Run,
    TableBlock,
    add_run,
    clean_text,
    plain_text,
)

_HYPHENS = ("-", "­", "‐", "‑")
_SENTENCE_END = (".", "!", "?", "…", ":", ";", "»", '"', "”", ")", "—")
_MIN_IMAGE_SIDE = 24
_SCAN_DPI = 150


@dataclass
class _Line:
    runs: list[Run]
    x0: float
    x1: float
    y0: float
    y1: float
    size: float
    page: int
    index: int = 0

    @property
    def text(self) -> str:
        return plain_text(self.runs)


@dataclass
class _Item:
    """Картинка или таблица в потоке страницы."""

    block: Block
    y0: float
    page: int


@dataclass
class _Paragraph:
    runs: list[Run] = field(default_factory=list)
    size: float = 0
    page: int = 0
    last_page: int = 0
    lines: int = 0
    center: bool = False
    x0: float = 0

    @property
    def text(self) -> str:
        return plain_text(self.runs)


def _span_run(span: dict) -> Optional[Run]:
    text = clean_text(span.get("text", ""))
    if not text:
        return None
    flags = span.get("flags", 0)
    font = span.get("font", "").lower()
    bold = bool(flags & 16) or "bold" in font or "black" in font or "heavy" in font
    italic = bool(flags & 2) or "italic" in font or "oblique" in font
    return Run(text.replace(" ", " "), bold=bold, italic=italic, sup=bool(flags & 1) and len(text) <= 6)


def _page_lines(page_dict: dict, page_number: int) -> tuple[list[_Line], list[tuple[dict, float]]]:
    """Строки в порядке документа и картинки с их положением."""
    lines: list[_Line] = []
    images: list[tuple[dict, float]] = []
    for block in page_dict.get("blocks", []):
        if block.get("type") == 1:
            images.append((block, block["bbox"][1]))
            continue
        for raw in block.get("lines", []):
            spans = raw.get("spans", [])
            runs: list[Run] = []
            sizes: collections.Counter = collections.Counter()
            for span in spans:
                run = _span_run(span)
                if run is None:
                    continue
                add_run(runs, run)
                sizes[round(span.get("size", 0), 1)] += len(run.text)
            if not runs or not "".join(run.text for run in runs).strip():
                continue
            size = sizes.most_common(1)[0][0]
            x0, y0, x1, y1 = raw["bbox"]
            # Одна визуальная строка бывает разбита на несколько строк PDF.
            previous = lines[-1] if lines else None
            if (
                previous
                and abs(previous.y0 - y0) < 0.5 * size
                and x0 >= previous.x1 - 0.5
                and x0 - previous.x1 < 4 * size
            ):
                if x0 - previous.x1 > 0.15 * size and not previous.text.endswith(" "):
                    add_run(previous.runs, Run(" "))
                for run in runs:
                    add_run(previous.runs, run)
                previous.x1 = max(previous.x1, x1)
                previous.y1 = max(previous.y1, y1)
                previous.size = max(previous.size, size) if len(previous.text) < 3 else previous.size
                continue
            lines.append(_Line(runs, x0, x1, y0, y1, size, page_number))
    return lines, images


def _find_furniture(pages: list[list[_Line]], heights: list[float]) -> set[tuple[int, int]]:
    """Находит колонтитулы и номера страниц: они повторяются у края страницы."""
    exact: collections.Counter = collections.Counter()
    pattern: collections.Counter = collections.Counter()
    candidates: list[tuple[tuple[int, int], str, str]] = []
    page_numbers: set[tuple[int, int]] = set()
    for page_index, lines in enumerate(pages):
        height = heights[page_index]
        for line_index, line in enumerate(lines):
            top = line.y1 < height * 0.11
            bottom = line.y0 > height * 0.89
            if not (top or bottom):
                continue
            edge = "t" if top else "b"
            text = line.text.lower().strip(" .-—–")
            # Номер, совпадающий с номером страницы, — колонтитул даже в коротком файле.
            if text.isdigit() and abs(int(text) - (page_index + 1)) <= 2 and len(pages) > 1:
                page_numbers.add((page_index, line_index))
            # Размер шрифта в ключе: заголовок главы крупнее своего колонтитула.
            exact_key = f"{edge}:{round(line.size)}:{text}"
            # Заголовки «Глава 1», «Глава 2» отличаются только номером, но это не колонтитул.
            if is_chapter_title(line.text):
                pattern_key = ""
            elif not text or is_bare_number(text.upper()):
                pattern_key = f"{edge}:#"
            else:
                pattern_key = f"{edge}:{round(line.size)}:{re.sub(r'[0-9]+', '#', text)}"
            exact[exact_key] += 1
            if pattern_key:
                pattern[pattern_key] += 1
            candidates.append(((page_index, line_index), exact_key, pattern_key))
    threshold = max(3, int(len(pages) * 0.15))
    found = page_numbers | {
        position
        for position, exact_key, pattern_key in candidates
        if exact[exact_key] >= 3 or (pattern_key and pattern[pattern_key] >= threshold)
    }
    # Если «колонтитулы» — это весь текст страниц, значит, это сам текст книги.
    filled = [index for index, lines in enumerate(pages) if lines]
    emptied = sum(
        all((index, position) in found for position in range(len(pages[index]))) for index in filled
    )
    return set() if emptied > len(filled) * 0.5 else found


_CONTENTS_TITLES = {"оглавление", "содержание", "contents", "tableofcontents", "мазмұны"}
_DOTTED_ENTRY_RE = re.compile(r"(?:[.…·_]\s*){3,}\d{1,4}\s*$")
_NUMBERED_ENTRY_RE = re.compile(r"\S\s+\d{1,4}\s*$")


def _drop_contents(pages: list[list[_Line]]) -> None:
    """Страницы оглавления не нужны: новое оглавление строится заново."""
    inside = False
    for page_index, lines in enumerate(pages):
        has_title = any(_normalize(line.text) in _CONTENTS_TITLES for line in lines[:4])
        dotted = [line for line in lines if _DOTTED_ENTRY_RE.search(line.text)]
        numbered = sum(bool(_NUMBERED_ENTRY_RE.search(line.text)) for line in lines)
        is_contents = (
            len(dotted) >= 3
            or (has_title and (numbered >= 2 or dotted))
            or (inside and numbered >= max(len(lines) * 0.6, 1))
        )
        if not is_contents:
            inside = False
            continue
        inside = True

        def in_dotted_row(line: _Line) -> bool:
            # Название пункта и точки с номером бывают отдельными строками PDF в одном ряду.
            middle = (line.y0 + line.y1) / 2
            return any(row.y0 <= middle <= row.y1 for row in dotted)

        kept = [
            line
            for line in lines
            if _normalize(line.text) not in _CONTENTS_TITLES
            and not in_dotted_row(line)
            and not _NUMBERED_ENTRY_RE.search(line.text)
        ]
        # Если на странице осталось много текста, значит, это был не список глав.
        if len(kept) <= max(len(lines) * 0.3, 3):
            pages[page_index] = kept


def _usable_image(block: dict) -> bool:
    return block.get("width", 0) >= _MIN_IMAGE_SIDE and block.get("height", 0) >= _MIN_IMAGE_SIDE


def _extract_tables(page, lines: list[_Line]) -> tuple[list[_Item], list[_Line]]:
    """Таблицы с линиями границ становятся настоящими таблицами."""
    try:
        if len(page.get_cdrawings()) < 4:
            return [], lines
        found = page.find_tables()
    except Exception:
        return [], lines
    items: list[_Item] = []
    keep = lines
    for table in found.tables:
        if table.row_count < 2 or table.col_count < 2:
            continue
        rows = []
        for row in table.extract():
            rows.append([[Run(clean_text(cell or "").strip())] if (cell or "").strip() else [] for cell in row])
        x0, y0, x1, y1 = table.bbox
        items.append(_Item(TableBlock(rows, False), y0, page.number))
        keep = [
            line
            for line in keep
            if not (x0 - 1 <= (line.x0 + line.x1) / 2 <= x1 + 1 and y0 - 1 <= (line.y0 + line.y1) / 2 <= y1 + 1)
        ]
    return items, keep


def _is_garbled(text: str) -> bool:
    if len(text) < 20:
        return False
    bad = sum(char == "�" or "" <= char <= "" for char in text)
    return bad / len(text) > 0.2


def _join(paragraph: _Paragraph, line: _Line) -> None:
    runs = paragraph.runs
    if runs and runs[-1].note is None:
        tail = runs[-1].text.rstrip()
        head = line.text.lstrip()
        if tail.endswith(_HYPHENS) and head[:1].islower():
            # Перенос слова: убираем дефис и склеиваем.
            runs[-1].text = tail.rstrip("".join(_HYPHENS))
        elif not runs[-1].text.endswith((" ", "\n")):
            add_run(runs, Run(" ", bold=runs[-1].bold, italic=runs[-1].italic))
    for run in line.runs:
        add_run(runs, Run(run.text, run.bold, run.italic, run.strike, run.sup, run.sub, run.code, run.href))
    paragraph.last_page = line.page
    paragraph.lines += 1


class _Layout:
    """Обобщённые размеры текста на странице."""

    def __init__(self, lines: list[_Line], body_size: float, page_width: float):
        body = [line for line in lines if abs(line.size - body_size) < 1] or lines
        lefts = collections.Counter(round(line.x0) for line in body)
        self.left = lefts.most_common(1)[0][0] if lefts else 0
        rights = sorted(line.x1 for line in body)
        self.right = rights[int(len(rights) * 0.9)] if rights else page_width
        self.width = max(self.right - self.left, 1)
        self.page_width = page_width


def _assemble(pages: list[list[_Line]], widths: list[float], body_size: float, leading: float) -> list[object]:
    """Склеивает строки в абзацы, учитывая отступы, интервалы и переходы страниц."""
    result: list[object] = []
    current: Optional[_Paragraph] = None
    previous: Optional[_Line] = None
    prev_layout: Optional[_Layout] = None

    def close() -> None:
        nonlocal current
        if current is not None:
            result.append(current)
            current = None

    for page_index, lines in enumerate(pages):
        layout = _Layout([line for line in lines if isinstance(line, _Line)], body_size, widths[page_index])
        for item in lines:
            if isinstance(item, _Item):
                close()
                previous = None
                result.append(item)
                continue
            line = item
            starts_new = current is None or previous is None
            if not starts_new:
                same_page = previous.page == line.page
                size_changed = abs(line.size - previous.size) > 0.6
                indent = line.x0 - layout.left
                prev_right = prev_layout.right if prev_layout else layout.right
                prev_width = prev_layout.width if prev_layout else layout.width
                short_previous = (prev_right - previous.x1) > 0.12 * prev_width
                ends_sentence = previous.text.rstrip().endswith(_SENTENCE_END)
                if size_changed:
                    starts_new = True
                elif same_page and (line.y0 - previous.y0) > max(leading * 1.35, line.size * 1.6):
                    starts_new = True
                elif indent > 0.8 * line.size:
                    starts_new = True
                elif short_previous and (ends_sentence or (prev_right - previous.x1) > 0.35 * prev_width):
                    starts_new = True
                elif same_page and line.x0 > previous.x1:
                    starts_new = True  # другая колонка
            if starts_new:
                close()
                center_offset = abs(((line.x0 + line.x1) / 2) - ((layout.left + layout.right) / 2))
                current = _Paragraph(
                    runs=[Run(r.text, r.bold, r.italic, r.strike, r.sup, r.sub, r.code, r.href) for r in line.runs],
                    size=line.size,
                    page=line.page,
                    last_page=line.page,
                    lines=1,
                    center=center_offset < 6 and line.x0 - layout.left > 12,
                    x0=line.x0,
                )
            else:
                _join(current, line)
            previous = line
            prev_layout = layout
    close()
    return result


def _trim(paragraph: _Paragraph) -> None:
    runs = paragraph.runs
    while runs and not runs[0].text.strip():
        runs.pop(0)
    while runs and not runs[-1].text.strip():
        runs.pop()
    if runs:
        runs[0].text = runs[0].text.lstrip()
        runs[-1].text = runs[-1].text.rstrip()


def _is_all(paragraph: _Paragraph, attribute: str) -> bool:
    visible = [run for run in paragraph.runs if run.text.strip()]
    return bool(visible) and all(getattr(run, attribute) for run in visible)


def _classify(items: list[object], body_size: float) -> dict[int, int]:
    """Определяет заголовки по размеру шрифта и по тексту. Возвращает {индекс: уровень}."""
    candidates: dict[int, float] = {}
    named: dict[int, float] = {}
    for index, item in enumerate(items):
        if not isinstance(item, _Paragraph):
            continue
        text = item.text.strip()
        if not text or len(text) > 160 or item.lines > 4:
            continue
        if is_chapter_title(text):
            named[index] = item.size
            continue
        if item.size >= body_size * 1.12 and not text.endswith((",", ";")) and any(c.isalpha() for c in text):
            candidates[index] = item.size
    paragraphs = sum(isinstance(item, _Paragraph) for item in items)
    if len(candidates) > max(paragraphs * 0.35, 6) and named:
        candidates = {}
    elif len(candidates) > max(paragraphs * 0.5, 6):
        candidates = {}
    all_sizes = {**candidates, **named}
    if not all_sizes:
        return {}
    sizes = sorted({round(size) for size in all_sizes.values()}, reverse=True)[:3]

    def level(size: float) -> int:
        rounded = round(size)
        for position, value in enumerate(sizes, 1):
            if rounded >= value:
                return position
        return len(sizes)

    return {index: level(size) for index, size in all_sizes.items()}


def _to_block(item: _Paragraph, body_size: float, ignore_bold: bool, ignore_italic: bool) -> Block:
    runs = item.runs
    if ignore_bold or ignore_italic:
        for run in runs:
            if ignore_bold:
                run.bold = False
            if ignore_italic:
                run.italic = False
    text = item.text.strip()
    kind = "normal"
    if len(text) < 90 and not text.endswith((".", ",", ";", ":")) and _is_all(item, "bold"):
        kind = "subtitle"
    align = "center" if item.center and item.lines == 1 and kind == "normal" and len(text) < 80 else None
    return Para(runs, kind, align)


def _apply_outline(items: list[object], toc: list, titles: dict[int, int]) -> dict[int, int]:
    """Закладки PDF точнее любых догадок: режем главы по ним."""
    result: dict[int, int] = {}
    by_page: dict[int, list[int]] = collections.defaultdict(list)
    for index, item in enumerate(items):
        if isinstance(item, _Paragraph):
            by_page[item.page].append(index)
    used: set[int] = set()
    for entry in toc:
        level, title, page_number = entry[0], entry[1], entry[2] - 1
        normalized = _normalize(title)
        if page_number < 0:
            continue
        chosen = None
        for index in by_page.get(page_number, []):
            if index in used:
                continue
            text = _normalize(items[index].text)
            if text and (text.startswith(normalized[:40]) or normalized.startswith(text[:40])) and len(text) < len(normalized) + 80:
                chosen = index
                break
        if chosen is None:
            # Заголовка нет в тексте (например, он картинкой): добавляем его сами.
            first = next((index for index in by_page.get(page_number, []) if index not in used), None)
            if first is None:
                continue
            marker = _Paragraph(runs=[Run(title.strip())], size=0, page=page_number, last_page=page_number, lines=1)
            chosen = first
            result[-first - 1] = level
            items[first] = _Inserted(marker, items[first])
            used.add(first)
            continue
        used.add(chosen)
        result[chosen] = level
    return result


class _Inserted:
    """Абзац, перед которым вставлен заголовок из закладки."""

    def __init__(self, marker: _Paragraph, original: _Paragraph):
        self.marker = marker
        self.original = original


def _normalize(text: str) -> str:
    return re.sub(r"[\W_]+", "", text.lower())


def _render_scan(page, book: Book) -> Optional[ImageBlock]:
    pixmap = page.get_pixmap(dpi=_SCAN_DPI, alpha=False)
    image_id = book.add_image(pixmap.tobytes("jpeg", jpg_quality=80))
    return ImageBlock(image_id) if image_id else None


def read(path: Path, progress=None) -> Book:
    try:
        document = pymupdf.open(str(path))
    except Exception as error:
        raise ConversionError("Файл PDF повреждён и не открывается.") from error
    with document:
        if document.needs_pass:
            raise ConversionError("PDF защищён паролем. Снимите защиту и попробуйте снова.")
        if document.page_count == 0:
            raise ConversionError("В PDF нет страниц.")
        return _read_document(document, path, progress)


def _read_document(document, path: Path, progress) -> Book:
    book = Book()
    meta = document.metadata or {}
    title = (meta.get("title") or "").strip()
    if title and not re.search(r"(?i)untitled|\.(docx?|pdf|indd|tex)$|^microsoft word", title):
        book.meta.title = title
    author = (meta.get("author") or "").strip()
    if author:
        book.meta.authors = [item.strip() for item in re.split(r"[;,]", author) if item.strip()]
    book.meta.annotation = (meta.get("subject") or "").strip()

    flags =pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_LIGATURES
    pages: list[list[_Line]] = []
    raw_images: list[list[tuple[dict, float]]] = []
    tables: list[list[_Item]] = []
    heights: list[float] = []
    widths: list[float] = []
    scans: dict[int, ImageBlock] = {}
    total = document.page_count

    for number in range(total):
        if progress and number % 5 == 0:
            progress("Читаю страницы", number / total * 0.8)
        page = document[number]
        page_dict = page.get_text("dict", flags=flags)
        lines, images = _page_lines(page_dict, number)
        text = "".join(line.text for line in lines)
        heights.append(page.rect.height)
        widths.append(page.rect.width)
        image_area = sum(
            (block["bbox"][2] - block["bbox"][0]) * (block["bbox"][3] - block["bbox"][1]) for block, _ in images
        )
        page_area = max(page.rect.width * page.rect.height, 1)
        scanned = (len(text.strip()) < 25 and image_area / page_area > 0.4) or _is_garbled(text)
        if scanned:
            block = _render_scan(page, book)
            if block:
                scans[number] = block
            pages.append([])
            raw_images.append([])
            tables.append([])
            continue
        table_items, lines = _extract_tables(page, lines)
        pages.append(lines)
        raw_images.append(images)
        tables.append(table_items)

    if scans:
        if len(scans) >= total * 0.5:
            book.warn(
                "Этот PDF похож на скан: текст в нём не выделяется. Страницы сохранены картинками, "
                "распознавание текста приложение не выполняет."
            )
        else:
            book.warn(f"Страниц без текстового слоя: {len(scans)}. Они сохранены картинками.")

    furniture = _find_furniture(pages, heights)
    for page_index, lines in enumerate(pages):
        pages[page_index] = [line for line_index, line in enumerate(lines) if (page_index, line_index) not in furniture]
    _drop_contents(pages)

    all_lines = [line for lines in pages for line in lines]
    sizes: collections.Counter = collections.Counter()
    for line in all_lines:
        sizes[line.size] += len(line.text)
    body_size = sizes.most_common(1)[0][0] if sizes else 11
    steps = [
        after.y0 - before.y0
        for lines in pages
        for before, after in zip(lines, lines[1:])
        if abs(before.size - body_size) < 0.6 and abs(after.size - body_size) < 0.6 and 0 < after.y0 - before.y0 < body_size * 3
    ]
    leading = statistics.median(steps) if steps else body_size * 1.3

    # Картинки и таблицы вставляем в поток по их вертикальному положению.
    flow: list[list[object]] = []
    for page_index, lines in enumerate(pages):
        items: list[_Item] = list(tables[page_index])
        for block, y0 in raw_images[page_index]:
            if not _usable_image(block):
                continue
            image_id = book.add_image(block.get("image", b""))
            if image_id:
                items.append(_Item(ImageBlock(image_id), y0, page_index))
        if page_index in scans:
            items.append(_Item(scans[page_index], 0, page_index))
        items.sort(key=lambda item: item.y0)
        merged: list[object] = []
        queue = list(items)
        for line in lines:
            while queue and queue[0].y0 <= line.y0:
                merged.append(queue.pop(0))
            merged.append(line)
        merged.extend(queue)
        flow.append(merged)

    if progress:
        progress("Собираю абзацы", 0.85)
    items = _assemble(flow, widths, body_size, leading)
    for item in items:
        if isinstance(item, _Paragraph):
            _trim(item)
    items = [item for item in items if not isinstance(item, _Paragraph) or item.text.strip()]
    if not items:
        raise ConversionError("В PDF не удалось найти ни текста, ни изображений.")

    weights = [(sum(len(run.text) for run in item.runs if run.bold), sum(len(run.text) for run in item.runs if run.italic), len(item.text))
               for item in items if isinstance(item, _Paragraph)]
    total_chars = max(sum(w[2] for w in weights), 1)
    ignore_bold = sum(w[0] for w in weights) / total_chars > 0.6
    ignore_italic = sum(w[1] for w in weights) / total_chars > 0.6

    toc = [entry for entry in document.get_toc(simple=True) if entry[1].strip()]
    outline_used = len(toc) >= 2
    if outline_used:
        titles = _apply_outline(items, toc, {})
    else:
        titles = _classify(items, body_size)

    book.chapters = _build_chapters(items, titles, body_size, ignore_bold, ignore_italic, path)
    _lift_pdf_title(book, path)
    return book


def _build_chapters(items, titles, body_size, ignore_bold, ignore_italic, path: Path) -> list[Chapter]:
    chapters: list[Chapter] = []
    current: Optional[Chapter] = None
    last_page = 0

    def begin(title: str, level: int, page: int) -> Chapter:
        chapter = Chapter(title=title, level=level, source=path.name)
        chapter.pdf_pages = [PdfRange(str(path), page, page)]
        chapters.append(chapter)
        return chapter

    def extend(chapter: Chapter, page: int) -> None:
        chapter.pdf_pages[-1].end = max(chapter.pdf_pages[-1].end, page)

    for index, item in enumerate(items):
        if isinstance(item, _Inserted):
            current = begin(plain_text(item.marker.runs), titles[-index - 1], item.original.page)
            item = item.original
        if isinstance(item, _Paragraph):
            last_page = item.page
            if index in titles:
                title = " ".join(item.text.split())
                # Заголовок из двух строк: «Глава 1» и сразу название главы.
                if (
                    current is not None
                    and not current.blocks
                    and current.level == titles[index]
                    and is_chapter_title(current.title)
                    and not is_chapter_title(title)
                    and len(current.title) < 40
                ):
                    current.title = f"{current.title}. {title}"
                    continue
                current = begin(title, titles[index], item.page)
                continue
            if current is None:
                current = begin("", 1, item.page)
            current.blocks.append(_to_block(item, body_size, ignore_bold, ignore_italic))
            extend(current, item.last_page)
        else:
            if current is None:
                current = begin("", 1, item.page)
            current.blocks.append(item.block)
            extend(current, item.page)
            last_page = item.page
    if chapters:
        extend(chapters[-1], last_page)
    return chapters


def _lift_pdf_title(book: Book, path: Path) -> None:
    """Убирает из текста титульный лист: название и автор хранятся в описании книги."""
    chapters = book.chapters
    authors = {_normalize(name) for name in book.meta.authors}
    authors.add(_normalize(", ".join(book.meta.authors)))
    authors.discard("")
    title_key = _normalize(book.meta.title)
    annotation_key = _normalize(book.meta.annotation)

    def strip_front(chapter: Chapter) -> None:
        """Убирает из начала главы строки титульного листа."""
        while chapter.blocks and isinstance(chapter.blocks[0], Para):
            text = _normalize(plain_text(chapter.blocks[0].runs))
            in_annotation = len(text) > 8 and annotation_key and (text in annotation_key or annotation_key in text)
            if text and (text == title_key or text in authors or in_annotation):
                chapter.blocks.pop(0)
            else:
                break

    # Титульный лист без закладки: абзацы до первой главы.
    if chapters and not chapters[0].title:
        front = chapters[0]
        # Картинка перед названием или единственная на первой странице — это обложка.
        if front.blocks and isinstance(front.blocks[0], ImageBlock):
            following = front.blocks[1] if len(front.blocks) > 1 else None
            next_text = _normalize(plain_text(following.runs)) if isinstance(following, Para) else ""
            if following is None or (next_text and next_text == title_key):
                book.meta.cover_id = book.meta.cover_id or front.blocks[0].image_id
                front.blocks.pop(0)
        strip_front(front)
        if not front.blocks and len(chapters) > 1:
            chapters.pop(0)

    # Название оформлено заголовком: первая глава без текста и самая крупная.
    if len(chapters) > 1:
        first = chapters[0]
        same_level = sum(1 for chapter in chapters if chapter.level == first.level)
        if (
            not first.blocks
            and first.title
            and first.level < chapters[1].level
            and same_level == 1
            and (not title_key or _normalize(first.title) == title_key or len(chapters) > 2)
        ):
            book.meta.title = book.meta.title or first.title
            title_key = _normalize(book.meta.title)
            chapters.pop(0)
            # Имя автора крупным шрифтом похоже на заголовок, но главой не является.
            if chapters and _normalize(chapters[0].title) in authors:
                strip_front(chapters[0])
                if chapters[0].blocks:
                    chapters[0].title = ""
                elif len(chapters) > 1:
                    chapters.pop(0)
            if chapters:
                top = min(chapter.level for chapter in chapters)
                for chapter in chapters:
                    chapter.level = chapter.level - top + 1
    book.meta.title = book.meta.title or path.stem
