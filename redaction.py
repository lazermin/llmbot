import re
from collections import Counter

MASK = "[СКРЫТО]"
SECRET_KEY = (
    r"(?:[\w-]{0,80}[_-])?(?:password|passwd|pwd|secret|token|api[_-]?key|"
    r"access[_-]?key|private[_-]?key|cookie)"
)
PATTERNS = (
    ("приватные ключи", re.compile(
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)"
    ), 0),
    ("данные авторизации", re.compile(
        r"(?im)\b(?:proxy-)?authorization\b[\"']?\s*[:=]\s*[\"']?(?:bearer|basic)\s+([^\s\"',;]+)"
    ), 1),
    ("данные авторизации", re.compile(r"(?im)\b(?:set-)?cookie\s*:\s*([^\r\n]+)"), 1),
    ("токены", re.compile(
        r"\b(?:[0-9]{5,16}:[A-Za-z0-9_-]{20,}|"
        r"(?:sk-(?:proj-)?|gh[pousr]_|github_pat_|xox[baprs]-)[A-Za-z0-9_-]{16,}|"
        r"AKIA[A-Z0-9]{16})\b"
    ), 0),
    ("учётные данные в URL", re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s/@:]+:([^\s/@]+)@"), 1),
    ("пароли и ключи конфигурации", re.compile(
        r"""(?im)^[ \t]*(?:export[ \t]+)?[\"']?""" + SECRET_KEY
        + r"""\b[\"']?[ \t]*[=:][ \t]*([^\r\n]+)"""
    ), 1),
    ("пароли и ключи конфигурации", re.compile(
        r"(?im)\b" + SECRET_KEY
        + r"""\b[\"']?[ \t]*[=:][ \t]*("(?:\\.|[^"\n\\])*"|'(?:\\.|[^'\n\\])*'|[^\s,;"'<>\[\]{}&?#]+)"""
    ), 1),
)


def redact(text: str) -> tuple[str, Counter]:
    counts = Counter()

    for category, pattern, group in PATTERNS:
        def substitute(match: re.Match) -> str:
            value = match.group(group)
            if value.strip().strip("\"'") == MASK:
                return match.group()
            counts[category] += 1
            start, end = match.span(group)
            return (
                match.group()[:start - match.start()] + MASK
                + "".join(char for char in value if char in "\r\n")
                + match.group()[end - match.start():]
            )

        text = pattern.sub(substitute, text)
    return text, counts
