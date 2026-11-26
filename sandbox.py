"""Изолированный разбор вложений в отдельном процессе.

PDF и DOCX приходят от пользователя, поэтому их разбор нельзя выполнять в
потоке бота: у потока нет ни таймаута, ни ограничения памяти, и отменённая
задача оставляет его работать. Здесь разбор идёт в дочернем процессе с
жёсткими RLIMIT; по таймауту или отмене вся группа процессов убивается.
"""

import asyncio
import logging
import os
import pickle
import signal
import sys
import traceback
from pathlib import Path, PurePath

from attachments import Attachment, AttachmentError, extract_text

try:
    import resource
except ImportError:  # Windows: изоляция недоступна, разбор идёт в потоке.
    resource = None  # type: ignore[assignment]

LOG = logging.getLogger("llmbot")

PARSE_TIMEOUT = 60.0
PARSE_MEMORY = 1024**3
PARSE_CPU = 50
MAX_PARSES = 2
ISOLATED_EXTENSIONS = {".pdf", ".docx"}

_slots = asyncio.Semaphore(MAX_PARSES)


def _apply_limits() -> None:
    if resource is None:
        return
    for limit, value in (
        (resource.RLIMIT_AS, (PARSE_MEMORY, PARSE_MEMORY)),
        (resource.RLIMIT_CPU, (PARSE_CPU, PARSE_CPU + 5)),
        (resource.RLIMIT_CORE, (0, 0)),
    ):
        try:
            resource.setrlimit(limit, value)
        except (OSError, ValueError):
            pass


def _child_main() -> int:
    _apply_limits()
    filename, data, ocr = pickle.load(sys.stdin.buffer)
    try:
        attachment = extract_text(filename, data, ocr=ocr)
    except AttachmentError as error:
        pickle.dump(("error", str(error)), sys.stdout.buffer, protocol=5)
    except MemoryError:
        pickle.dump(("error", "Файл слишком сложен для обработки. Отправьте файл меньшего размера."), sys.stdout.buffer, protocol=5)
    except BaseException:
        traceback.print_exc()
        return 1
    else:
        pickle.dump(("ok", attachment), sys.stdout.buffer, protocol=5)
    sys.stdout.buffer.flush()
    return 0


def _child_env() -> dict[str, str]:
    # Парсеру не нужны токены и ключи бота: среда собирается с нуля.
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
    }


async def _stop(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
    try:
        await asyncio.shield(process.wait())
    except asyncio.CancelledError:
        pass
    except ProcessLookupError:
        pass


async def _isolated(filename: str, data: bytes, *, ocr: bool) -> Attachment:
    worker = Path(__file__).resolve().with_name("sandbox.py")
    payload = pickle.dumps((filename, data, ocr), protocol=5)
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-E", "-s", str(worker),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(worker.parent),
        env=_child_env(),
        start_new_session=True,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(payload), PARSE_TIMEOUT)
    except asyncio.TimeoutError:
        await _stop(process)
        raise AttachmentError(
            "Обработка файла заняла слишком много времени. Отправьте файл меньшего размера."
        )
    except asyncio.CancelledError:
        await _stop(process)
        raise

    if process.returncode != 0:
        # Содержимое файла и traceback в журнал не попадают.
        LOG.warning("Разбор вложения прерван (код %s, stderr %s байт).", process.returncode, len(stderr))
        raise AttachmentError("Не удалось прочитать документ. Проверьте его формат и целостность.")

    try:
        status, value = pickle.loads(stdout)
    except Exception as error:
        raise AttachmentError("Не удалось прочитать документ. Проверьте его формат и целостность.") from error
    if status == "error":
        raise AttachmentError(str(value))
    if not isinstance(value, Attachment):
        raise AttachmentError("Не удалось прочитать документ. Проверьте его формат и целостность.")
    return value


async def extract_attachment(filename: str, data: bytes, *, ocr: bool = False) -> Attachment:
    if resource is None or PurePath(filename).suffix.lower() not in ISOLATED_EXTENSIONS:
        return await asyncio.to_thread(extract_text, filename, data, ocr=ocr)
    async with _slots:
        return await _isolated(filename, data, ocr=ocr)


if __name__ == "__main__":
    raise SystemExit(_child_main())
