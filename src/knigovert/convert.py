"""Выбор формата и единая точка конвертации."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .formats import epub, fb2, markup, pdf_in, pdf_out, plain, word
from .model import Book, ConversionError, Progress


@dataclass(frozen=True)
class OutputFormat:
    key: str
    label: str
    extension: str
    write: Callable


OUTPUTS: dict[str, OutputFormat] = {
    item.key: item
    for item in (
        OutputFormat("pdf", "PDF", ".pdf", pdf_out.write),
        OutputFormat("docx", "DOCX (Word)", ".docx", word.write),
        OutputFormat("fb2", "FB2", ".fb2", fb2.write),
        OutputFormat("fb2.zip", "FB2 в архиве", ".fb2.zip", fb2.write_zip),
        OutputFormat("epub", "EPUB", ".epub", epub.write),
        OutputFormat("txt", "TXT", ".txt", plain.write),
        OutputFormat("html", "HTML", ".html", markup.write_html),
        OutputFormat("md", "Markdown", ".md", markup.write_markdown),
    )
}

_READERS: dict[str, Callable] = {
    "pdf": pdf_in.read,
    "docx": word.read,
    "fb2": fb2.read,
    "epub": epub.read,
    "txt": plain.read,
    "html": markup.read_html,
    "md": markup.read_markdown,
}

INPUT_EXTENSIONS = (
    ".pdf", ".docx", ".fb2", ".fb2.zip", ".zip", ".epub", ".txt", ".text", ".html", ".htm", ".xhtml", ".md", ".markdown",
)
_UNSUPPORTED = {
    ".doc": "Старый формат .doc не поддерживается. Откройте файл в Word и сохраните как .docx.",
    ".rtf": "Формат RTF не поддерживается. Откройте файл в Word и сохраните как .docx.",
    ".odt": "Формат ODT не поддерживается. Сохраните файл как .docx.",
    ".mobi": "Формат MOBI не поддерживается.",
    ".azw3": "Формат AZW3 не поддерживается.",
    ".djvu": "Формат DjVu не поддерживается.",
}


def detect_format(path: Path, display_name: Optional[str] = None) -> str:
    """Определяет формат по содержимому и расширению; содержимое важнее."""
    name = (display_name or path.name).lower()
    suffix = Path(name).suffix
    with path.open("rb") as handle:
        head = handle.read(4096)
    if head[:4] == b"%PDF" or head[:1024].find(b"%PDF-") >= 0 and suffix == ".pdf":
        return "pdf"
    if head[:4] == b"\xd0\xcf\x11\xe0":
        raise ConversionError(_UNSUPPORTED[".doc"])
    if head[:5] == b"{\\rtf":
        raise ConversionError(_UNSUPPORTED[".rtf"])
    if head[:2] == b"PK":
        return _detect_zip(path, suffix)
    if suffix in _UNSUPPORTED:
        raise ConversionError(_UNSUPPORTED[suffix])
    if suffix == ".fb2" or re.search(rb"<\s*FictionBook\b", head):
        return "fb2"
    if suffix in (".html", ".htm", ".xhtml"):
        return "html"
    if suffix in (".md", ".markdown"):
        return "md"
    if suffix in (".txt", ".text", ""):
        if re.match(rb"\s*<(!doctype html|html)\b", head, re.IGNORECASE):
            return "html"
        return "txt"
    if suffix == ".pdf":
        raise ConversionError("Файл с расширением .pdf не является настоящим PDF.")
    if suffix in (".docx", ".epub"):
        raise ConversionError(f"Файл с расширением {suffix} повреждён.")
    raise ConversionError(f"Формат {suffix or 'этого файла'} не поддерживается.")


def _detect_zip(path: Path, suffix: str) -> str:
    import zipfile

    try:
        with zipfile.ZipFile(path) as archive:
            names = [name.lower() for name in archive.namelist()]
            if "mimetype" in archive.namelist() and archive.read("mimetype").strip() == b"application/epub+zip":
                return "epub"
    except zipfile.BadZipFile as error:
        raise ConversionError("Архив повреждён и не открывается.") from error
    if "word/document.xml" in names:
        return "docx"
    if "meta-inf/container.xml" in names:
        return "epub"
    if any(name.endswith(".fb2") for name in names):
        return "fb2"
    if suffix == ".odt" or "content.xml" in names:
        raise ConversionError(_UNSUPPORTED[".odt"])
    raise ConversionError("В архиве нет книги: ожидается файл FB2.")


def read_book(path: Path, display_name: Optional[str] = None, progress: Progress = None) -> Book:
    fmt = detect_format(path, display_name)
    book = _READERS[fmt](path, progress)
    if display_name:
        stem = stem_of(display_name)
        if book.meta.title == path.stem or book.meta.title == re.sub(r"\.fb2(\.zip)?$", "", path.name, flags=re.I):
            book.meta.title = stem
        for chapter in book.chapters:
            chapter.source = display_name
    return book


def write_book(book: Book, path: Path, target: str, options: Optional[dict] = None, progress: Progress = None) -> None:
    fmt = OUTPUTS.get(target)
    if fmt is None:
        raise ConversionError(f"Неизвестный формат: {target}")
    fmt.write(book, path, options or {}, progress)


def stem_of(name: str) -> str:
    """Имя файла без расширения, в том числе двойного .fb2.zip."""
    name = Path(name.replace("\\", "/")).name
    return re.sub(
        r"\.(fb2\.zip|fb2|docx|pdf|epub|txt|text|html|htm|xhtml|md|markdown|zip)$", "", name, flags=re.IGNORECASE
    ) or "книга"


_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_filename(name: str, fallback: str = "книга") -> str:
    """Имя, которое можно создать в любой системе, без путей и запрещённых символов."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned or cleaned.upper() in _RESERVED:
        cleaned = fallback
    return cleaned[:150].rstrip(" .")


def unique_path(folder: Path, stem: str, extension: str) -> Path:
    """Никогда не перезаписывает существующий файл: добавляет номер."""
    candidate = folder / f"{stem}{extension}"
    number = 1
    while candidate.exists():
        candidate = folder / f"{stem} ({number}){extension}"
        number += 1
    return candidate
