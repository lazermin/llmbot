import re
from collections.abc import Iterator
from html import unescape

from aiogram.types import MessageEntity

ENTITY_TYPES = {
    "b": "bold",
    "i": "italic",
    "code": "code",
    "pre": "pre",
    "tg-spoiler": "spoiler",
}
TAG = re.compile(r"<(/?)(b|i|code|pre|tg-spoiler)>")
ESCAPE = re.compile(r"&(?:amp|lt|gt|quot|apos|#[0-9]{1,7}|#x[0-9a-fA-F]{1,6});")
CODE_TAGS = {"code", "pre"}


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def telegram_chunks(text: str) -> Iterator[str]:
    """Split text within Telegram's limit, preferring a nearby line break."""
    limit = 4000
    start = size = 0
    last_break = -1
    for index, char in enumerate(text):
        width = 2 if ord(char) > 0xFFFF else 1
        if size + width > limit:
            end = last_break + 1 if last_break >= start + limit // 3 else index
            yield text[start:end]
            start = end
            size = utf16_length(text[start:index])
            last_break = -1
        size += width
        if char == "\n":
            last_break = index
    if start < len(text):
        yield text[start:]


def formatted_chunks(markup: str) -> Iterator[tuple[str, list[MessageEntity]]]:
    """Turn the allowed HTML into Telegram entities over UTF-16 text offsets."""
    pieces: list[str] = []
    spans: list[tuple[int, int, str]] = []
    active: list[tuple[str, int]] = []
    position = 0

    def append(fragment: str) -> None:
        nonlocal position
        plain = ESCAPE.sub(lambda match: unescape(match.group()), fragment)
        pieces.append(plain)
        position += utf16_length(plain)

    previous = 0
    for match in TAG.finditer(markup):
        append(markup[previous:match.start()])
        previous = match.end()
        closing, tag = match.groups()
        if closing and active and active[-1][0] == tag:
            _, start = active.pop()
            if start < position:
                spans.append((start, position, ENTITY_TYPES[tag]))
        elif not closing and not active:
            active.append((tag, position))
        elif not closing and tag not in CODE_TAGS and active[-1][0] not in CODE_TAGS:
            active.append((tag, position))
        else:
            append(match.group())
    append(markup[previous:])

    # Keep the text of unclosed spans without relying on HTML parsing by Telegram.
    spans.sort(key=lambda span: (span[0], -span[1]))
    offset = 0
    for chunk in telegram_chunks("".join(pieces)):
        end = offset + utf16_length(chunk)
        if chunk.strip():
            entities = [
                MessageEntity(
                    type=kind,
                    offset=max(start, offset) - offset,
                    length=min(stop, end) - max(start, offset),
                )
                for start, stop, kind in spans
                if start < end and stop > offset
            ]
            yield chunk, entities
        offset = end
