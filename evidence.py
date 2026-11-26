import re
import shlex
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from html import escape
from itertools import islice

LINE = re.compile(r"[^\n]*\n|[^\n]+$")
ISO_STAMP = re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?")
CLOCK = re.compile(r"(?<!\d)\d{2}:\d{2}(?::\d{2})?(?!\d)")
ERROR = re.compile(r"\b(?:error|fail\w*|fatal|timeout|panic|denied|ошиб\w*|сбой|авари\w*)\b", re.I)
WORD = re.compile(r"[\w.-]{3,}")
REFERENCE = re.compile(r"\[\[F(\d{1,9}):(?:L(\d{1,9})(?:-L(\d{1,9}))?|P(\d{1,5}))\]\]")
FIND_HELP = "Пример: /find --from 14:20 --to 14:30 timeout. Можно указать дату ISO или время."


@dataclass(frozen=True)
class Search:
    query: str = ""
    start: datetime | time | None = None
    end: datetime | time | None = None

    def matches(self, line: str) -> bool:
        if self.query and self.query.casefold() not in line.casefold():
            return False
        anchor = self.start if self.start is not None else self.end
        if anchor is None:
            return True
        pattern = ISO_STAMP if isinstance(anchor, datetime) else CLOCK
        match = pattern.search(line)
        if match is None:
            return False
        try:
            value = parse_bound(match.group(), upper=False)
        except ValueError:
            return False
        return (
            (self.start is None or self.start <= value)
            and (self.end is None or value <= self.end)
        )


def parse_bound(value: str, *, upper: bool) -> datetime | time:
    if CLOCK.fullmatch(value):
        stamp = time.fromisoformat(value)
        return stamp.replace(second=59, microsecond=999999) if upper and len(value) == 5 else stamp
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if upper and len(value) == 10:
        stamp = stamp.replace(hour=23, minute=59, second=59, microsecond=999999)
    elif upper and len(value) == 16:
        stamp = stamp.replace(second=59, microsecond=999999)
    return stamp.astimezone(timezone.utc).replace(tzinfo=None) if stamp.tzinfo else stamp


def parse_search(text: str) -> Search:
    tokens = iter(shlex.split(text))
    terms = []
    start = end = None
    for token in tokens:
        if token in {"--from", "--to"}:
            value = next(tokens, "")
            if not value:
                raise ValueError(FIND_HELP)
            bound = parse_bound(value, upper=token == "--to")
            if token == "--from":
                start = bound
            else:
                end = bound
        else:
            terms.append(token)
    if start is not None and end is not None and (type(start) is not type(end) or start > end):
        raise ValueError(FIND_HELP)
    query = " ".join(terms).strip()
    if not query and start is None and end is None:
        raise ValueError(FIND_HELP)
    return Search(query, start, end)


def compact(line: str, needle: str = "", limit: int = 450) -> str:
    if len(line) <= limit:
        return line
    index = line.casefold().find(needle.casefold()) if needle else 0
    start = max(0, index - limit // 3)
    return ("…" if start else "") + line[start:start + limit] + ("…" if start + limit < len(line) else "")


def find_lines(
    text: str, search: Search, max_hits: int = 10, repeats: Counter | None = None,
) -> tuple[int, dict[int, str]]:
    hits = 0
    selected = {}
    previous = deque(maxlen=2)
    after = 0
    for number, match in enumerate(LINE.finditer(text), 1):
        line = match.group().rstrip("\r\n")
        if search.matches(line):
            hits += 1
            if repeats is not None:
                timestamp = ISO_STAMP.search(line) or CLOCK.search(line)
                message = compact((line[timestamp.end():] if timestamp else line).strip(), limit=180)
                if message and (message in repeats or len(repeats) < 250):
                    repeats[message] += 1
            if hits <= max_hits:
                selected.update(previous)
                selected[number] = compact(line, search.query)
                after = 2
        elif after:
            selected[number] = compact(line)
            after -= 1
        previous.append((number, compact(line)))
    return hits, selected


def choose_lines(text: str, question: str, budget: int, search: Search | None) -> tuple[dict[int, str], bool]:
    if search is not None:
        _, focused = find_lines(text, search, max_hits=30)
        limit = 0
        result = {}
        for number, line in sorted(focused.items()):
            limit += len(line) + len(str(number)) + 3
            if limit > budget:
                break
            result[number] = line
        return result, True

    words = list(dict.fromkeys(WORD.findall(question.casefold())))[:24]
    if len(text) <= budget:
        result = {}
        used = 0
        for number, match in enumerate(LINE.finditer(text), 1):
            line = match.group().rstrip("\r\n")
            used += len(line) + len(str(number)) + 3
            if used > budget:
                return result, True
            result[number] = line
        return result, False

    by_priority = {0: {}, 1: {}, 2: {}}
    previous = deque(maxlen=2)
    after_left = 0
    after_priority = 0
    for number, match in enumerate(LINE.finditer(text), 1):
        line = match.group().rstrip("\r\n")
        lower = line.casefold()
        word = next((word for word in words if word in lower), "")
        priority = 2 if word else 1 if ERROR.search(line) else 0
        short = compact(line, word)
        if len(by_priority[priority]) < 150 and (priority or number <= 25):
            by_priority[priority].update(previous)
            by_priority[priority][number] = short
            if priority >= after_priority or not after_left:
                after_priority, after_left = priority, 2
        elif after_left:
            by_priority[after_priority][number] = short
            after_left -= 1
        previous.append((number, short))

    result = {}
    used = 0
    for priority in (2, 1, 0):
        for number, line in by_priority[priority].items():
            cost = len(line) + len(str(number)) + 3
            if number not in result and used + cost <= budget:
                result[number] = line
                used += cost
    return dict(sorted(result.items())), True


@dataclass
class Evidence:
    lines: dict[int, dict[int, str]] = field(default_factory=dict)
    pages: dict[int, dict[int, str]] = field(default_factory=dict)


def verify_references(answer: str, evidence: Evidence) -> tuple[str, str]:
    citations = {}

    def substitute(match: re.Match) -> str:
        file_id, first, last, page = match.groups()
        file_number = int(file_id)
        if page:
            excerpt = evidence.pages.get(file_number, {}).get(int(page))
            if excerpt is None:
                return "[Источник не сопоставлен]"
            label = f"Файл №{file_number}, стр. {page}"
        else:
            start, end = int(first), int(last or first)
            lines = evidence.lines.get(file_number, {})
            if not 0 < start <= end <= start + 7 or any(number not in lines for number in range(start, end + 1)):
                return "[Источник не сопоставлен]"
            label = f"Файл №{file_number}, строка {start}" if start == end else f"Файл №{file_number}, строки {start}–{end}"
            excerpt = "\n".join(f"{number}: {lines[number]}" for number in range(start, end + 1))
        if label not in citations and len(citations) < 12:
            citations[label] = compact(excerpt, limit=360)
        return f"〔{label}〕"

    text = REFERENCE.sub(substitute, answer)
    appendix = ""
    if citations:
        appendix = "\n\n<b>Фрагменты доказательств</b>\n" + "\n".join(
            f"{label}\n<pre>{escape(excerpt)}</pre>" for label, excerpt in islice(citations.items(), 12)
        )
    return text, appendix
