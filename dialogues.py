from collections import deque
from dataclasses import dataclass, field
from sys import getsizeof

from attachments import Attachment
from evidence import Search

DIALOG_LIMIT = 8
FILE_LIMIT = 8
IMAGE_CONTEXT_LIMIT = 12
IMAGE_BYTES_LIMIT = 16 * 1024**2
DOCUMENT_CONTEXT_LIMIT = 48_000
ATTACHMENT_MEMORY_LIMIT = 64 * 1024**2


@dataclass(frozen=True)
class Reply:
    text: str
    truncated: bool = False
    sources: str = ""
    web_calls: list[dict] = field(default_factory=list)
    model: str = ""
    web_status: str = ""
    inputs: str = ""
    evidence: str = ""


@dataclass(frozen=True)
class Request:
    question: str
    turns: tuple[tuple[str, Reply, bool], ...]
    files: dict[int, Attachment]
    model: str
    explicit_web: bool = False
    incident: bool = False
    search: Search | None = None


@dataclass(frozen=True)
class Retry:
    token: str
    request: Request


@dataclass
class Dialogue:
    name: str
    turns: deque[tuple[str, Reply, bool]]
    model: str = ""
    files: dict[int, Attachment] = field(default_factory=dict)
    next_file_id: int = 1
    last_status: str = ""
    search: Search | None = None
    export: str = ""
    retry: Retry | None = None

    def add_file(self, attachment: Attachment) -> int:
        if len(self.files) >= FILE_LIMIT:
            raise ValueError(f"В диалоге уже {FILE_LIMIT} файлов. Удалите один через /files remove ID.")
        images = [image for file in (*self.files.values(), attachment) for image in file.images]
        if len(images) > IMAGE_CONTEXT_LIMIT or sum(len(image.url) for image in images) > IMAGE_BYTES_LIMIT:
            raise ValueError("Исчерпан объём изображений в диалоге. Удалите лишние файлы через /files remove ID.")
        number = self.next_file_id
        self.files[number] = attachment
        self.retry = None
        self.next_file_id += 1
        return number

    def reset(self) -> None:
        self.files.clear()
        self.turns.clear()
        self.last_status = ""
        self.search = None
        self.export = ""
        self.retry = None


class Dialogues:
    def __init__(self, history: int) -> None:
        self.history = history
        self.items = {1: Dialogue("Основной", deque(maxlen=history))}
        self.active_id = 1
        self.next_id = 2

    @property
    def current(self) -> Dialogue:
        return self.items[self.active_id]

    def add_file(self, attachment: Attachment) -> int:
        attachments = [file for dialogue in self.items.values() for file in dialogue.files.values()]
        total = sum(
            getsizeof(file.text) + sum(getsizeof(image.url) for image in file.images)
            for file in (*attachments, attachment)
        )
        if total > ATTACHMENT_MEMORY_LIMIT:
            raise ValueError("Достигнут общий лимит памяти вложений. Освободите место через /files remove ID.")
        return self.current.add_file(attachment)

    def create(self, name: str) -> int:
        if len(self.items) >= DIALOG_LIMIT:
            raise ValueError(f"Можно держать до {DIALOG_LIMIT} диалогов. Удалите старый через /chat delete ID.")
        name = " ".join(name.split())
        if not 1 <= len(name) <= 60:
            raise ValueError("Введите название диалога длиной от 1 до 60 символов.")
        number = self.next_id
        self.items[number] = Dialogue(name, deque(maxlen=self.history))
        self.active_id = number
        self.next_id += 1
        return number
