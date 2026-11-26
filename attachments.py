from base64 import b64encode
from collections.abc import Iterable
from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePath
from threading import Lock
from zipfile import BadZipFile, ZipFile

from docx import Document as WordDocument
from docx.table import Table
from pypdf import PdfReader
from pypdf.errors import PyPdfError
from pypdfium2 import PdfDocument, PdfiumError

DOC_LIMIT = 10 * 1024**2
IMAGE_LIMIT = 8 * 1024**2
TEXT_LIMIT = 24_000
PDF_PAGE_LIMIT = 50
OCR_PAGE_LIMIT = 6
PDFIUM_LOCK = Lock()
TEXT_EXTENSIONS = {
    ".txt", ".log", ".md", ".conf", ".cfg", ".ini", ".json", ".yaml", ".yml",
    ".xml", ".csv", ".tsv", ".service", ".sh",
}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
DOCUMENT_EXTENSIONS = TEXT_EXTENSIONS | {".pdf", ".docx"}


class AttachmentError(Exception):
    """An attachment cannot be used as input."""


class LimitedBuffer(BytesIO):
    def __init__(self, limit: int) -> None:
        super().__init__()
        self.limit = limit

    def write(self, data: bytes) -> int:
        if self.tell() + len(data) > self.limit:
            raise AttachmentError(size_message(self.limit))
        return super().write(data)


@dataclass(frozen=True)
class DocumentText:
    text: str
    partial: bool


@dataclass(frozen=True)
class PageImage:
    url: str
    page: int | None = None


@dataclass(frozen=True)
class Attachment:
    name: str
    text: str = ""
    images: tuple[PageImage, ...] = ()
    partial: bool = False
    pages: tuple[tuple[int, str], ...] = ()
    masks: tuple[tuple[str, int], ...] = ()


def size_message(limit: int) -> str:
    return f"Файл превышает лимит {limit // 1024**2} МиБ. Отправьте файл меньшего размера."


def image_url(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    elif data.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif data.startswith((b"GIF87a", b"GIF89a")):
        mime = "image/gif"
    elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        raise AttachmentError("Отправьте изображение в формате JPEG, PNG, WebP или GIF.")
    return f"data:{mime};base64,{b64encode(data).decode('ascii')}"


def limit_text(parts: Iterable[str], *, partial: bool = False) -> DocumentText:
    kept = []
    remaining = TEXT_LIMIT
    for part in parts:
        if not part.strip():
            continue
        if kept:
            part = "\n" + part
        if len(part) > remaining:
            kept.append(part[:remaining])
            partial = True
            break
        kept.append(part)
        remaining -= len(part)
    return DocumentText("".join(kept).strip(), partial)


def pdf_content(data: bytes, *, ocr: bool = False) -> tuple[DocumentText, tuple[PageImage, ...], tuple[tuple[int, str], ...]]:
    reader = PdfReader(BytesIO(data))
    if reader.is_encrypted and not reader.decrypt(""):
        raise AttachmentError("PDF защищён паролем. Отправьте копию без пароля.")
    page_count = len(reader.pages)
    scans, pages = [], []
    remaining = TEXT_LIMIT
    partial = page_count > PDF_PAGE_LIMIT
    for index, page in enumerate(reader.pages[:PDF_PAGE_LIMIT]):
        text = (page.extract_text() or "").strip()
        if text:
            partial |= len(text) > remaining
            size = min(len(text), remaining)
            if size:
                pages.append((index + 1, text[:size]))
            remaining -= size
        elif page.get_contents() is not None:
            scans.append(index)
    result = DocumentText("\n".join(text for _, text in pages), partial)
    if not ocr or not scans:
        return DocumentText(result.text, result.partial or bool(scans)), (), tuple(pages)

    images = render_pages(data, scans[:OCR_PAGE_LIMIT])
    return DocumentText(result.text, result.partial or len(scans) > len(images)), images, tuple(pages)


def render_pages(data: bytes, indexes: list[int]) -> tuple[PageImage, ...]:
    images = []
    with PDFIUM_LOCK, PdfDocument(data) as pdf:
        for index in indexes:
            page = pdf[index]
            try:
                scale = min(2.5, 1900 / max(page.get_size()))
                bitmap = page.render(scale=scale)
                try:
                    with bitmap.to_pil().convert("RGB") as image, BytesIO() as encoded:
                        image.save(encoded, format="JPEG", quality=86)
                        images.append(PageImage(image_url(encoded.getvalue()), index + 1))
                finally:
                    bitmap.close()
            finally:
                page.close()
    return tuple(images)


def word_parts(document) -> Iterable[str]:
    for block in document.iter_inner_content():
        if isinstance(block, Table):
            for row in block.rows:
                yield "\t".join(cell.text for cell in row.cells)
        else:
            yield block.text


def word_text(data: bytes) -> DocumentText:
    stream = BytesIO(data)
    with ZipFile(stream) as archive:
        entries = archive.infolist()
        if len(entries) > 2_000 or sum(entry.file_size for entry in entries) > 48 * 1024**2:
            raise AttachmentError("DOCX слишком велик после распаковки.")
    stream.seek(0)
    return limit_text(word_parts(WordDocument(stream)))


def plain_text(data: bytes) -> DocumentText:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = data.decode("utf-16")
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("cp1251")
    sample = text[:4096]
    if any(ord(char) < 32 and char not in "\t\n\r\f" for char in sample):
        raise AttachmentError("Файл выглядит как двоичный. Отправьте текст, PDF или DOCX.")
    return DocumentText(text, False)


def extract_text(filename: str, data: bytes, *, ocr: bool = False) -> Attachment:
    extension = PurePath(filename).suffix.lower()
    images = ()
    pages = ()
    try:
        if extension == ".pdf":
            document, images, pages = pdf_content(data, ocr=ocr)
        elif extension == ".docx":
            document = word_text(data)
        else:
            document = plain_text(data)
    except AttachmentError:
        raise
    except (BadZipFile, PyPdfError, PdfiumError, ValueError, KeyError, OSError, RuntimeError, SyntaxError) as error:
        raise AttachmentError("Не удалось прочитать документ. Проверьте его формат и целостность.") from error
    if not document.text.strip() and not images:
        if extension == ".pdf":
            raise AttachmentError("В PDF нет извлекаемого текста. Для сканов нужна доступная модель распознавания; пустые страницы пропускаются.")
        raise AttachmentError("В документе не удалось найти текст.")
    return Attachment(filename, document.text, images, document.partial, pages)
