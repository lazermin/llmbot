"""Изолированный разбор вложений в отдельном процессе.

PDF и DOCX приходят от пользователя, поэтому их разбор нельзя выполнять в
потоке бота: у потока нет ни таймаута, ни ограничения памяти, и отменённая
задача оставляет его работать. Здесь разбор идёт в дочернем процессе с
жёсткими RLIMIT; по таймауту и отмене группа убивается, а после завершения
лидера добиваются осиротевшие потомки.
"""

import asyncio
import logging
import os
import pickle
import signal
import sys
import tempfile
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
# Attachment ограничен TEXT_LIMIT и шестью OCR-страницами, поэтому больше
# этого объёма корректный разбор не отдаёт. Тот же предел стоит на запись
# результата в дочернем процессе, а не только на чтение в родителе.
MAX_OUTPUT = 64 * 1024**2
ISOLATED_EXTENSIONS = {".pdf", ".docx"}

_slots = asyncio.Semaphore(MAX_PARSES)


def _apply_limits() -> None:
    if resource is None:
        return
    for limit, value in (
        (resource.RLIMIT_AS, (PARSE_MEMORY, PARSE_MEMORY)),
        (resource.RLIMIT_CPU, (PARSE_CPU, PARSE_CPU + 5)),
        (resource.RLIMIT_CORE, (0, 0)),
        # Результат разбора не может превысить MAX_OUTPUT: запись сверх
        # лимита обрывает процесс, и родитель не получает гигантский файл.
        (resource.RLIMIT_FSIZE, (MAX_OUTPUT, MAX_OUTPUT)),
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


def _kill_group(process: asyncio.subprocess.Process) -> None:
    # start_new_session=True делает ребёнка лидером собственной группы,
    # поэтому её id совпадает с pid. Группу добиваем и после нормального
    # завершения лидера: осиротевший потомок не должен пережить разбор.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        try:
            process.kill()
        except ProcessLookupError:
            pass


async def _stop(process: asyncio.subprocess.Process) -> None:
    _kill_group(process)
    try:
        await asyncio.shield(process.wait())
    except asyncio.CancelledError:
        pass
    except ProcessLookupError:
        pass


async def _send_and_wait(process: asyncio.subprocess.Process, payload: bytes) -> None:
    # Отправка входит в общий таймаут: ребёнок может зависнуть на импорте
    # ещё до чтения stdin, и тогда большой payload висит в drain.
    if process.stdin is not None:
        try:
            process.stdin.write(payload)
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass
    await process.wait()


async def _isolated(filename: str, data: bytes, *, ocr: bool) -> Attachment:
    worker = Path(__file__).resolve().with_name("sandbox.py")
    payload = pickle.dumps((filename, data, ocr), protocol=5)
    # Результат пишем во временный файл, а не в pipe: размер записи ограничен
    # RLIMIT_FSIZE дочернего процесса, поэтому родитель физически не может
    # получить неограниченный буфер. stderr тоже не буферизуем: библиотеки
    # могут писать туда неограниченно много.
    output = tempfile.TemporaryFile()
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-E", "-s", str(worker),
            stdin=asyncio.subprocess.PIPE,
            stdout=output,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=str(worker.parent),
            env=_child_env(),
            start_new_session=True,
        )
        try:
            await asyncio.wait_for(_send_and_wait(process, payload), PARSE_TIMEOUT)
        except asyncio.TimeoutError:
            await _stop(process)
            raise AttachmentError(
                "Обработка файла заняла слишком много времени. Отправьте файл меньшего размера."
            )
        except asyncio.CancelledError:
            await _stop(process)
            raise
        _kill_group(process)

        if process.returncode != 0:
            # Содержимое файла и traceback в журнал не попадают.
            LOG.warning("Разбор вложения прерван (код %s).", process.returncode)
            raise AttachmentError("Не удалось прочитать документ. Проверьте его формат и целостность.")

        output.seek(0, os.SEEK_END)
        if output.tell() > MAX_OUTPUT:
            LOG.warning("Разбор вложения вернул неожиданно большой результат.")
            raise AttachmentError("Не удалось прочитать документ. Проверьте его формат и целостность.")
        output.seek(0)
        stdout = output.read(MAX_OUTPUT)

        try:
            status, value = pickle.loads(stdout)
        except Exception as error:
            raise AttachmentError("Не удалось прочитать документ. Проверьте его формат и целостность.") from error
        if status == "error":
            raise AttachmentError(str(value))
        if not isinstance(value, Attachment):
            raise AttachmentError("Не удалось прочитать документ. Проверьте его формат и целостность.")
        return value
    finally:
        output.close()


async def extract_attachment(filename: str, data: bytes, *, ocr: bool = False) -> Attachment:
    if resource is None or PurePath(filename).suffix.lower() not in ISOLATED_EXTENSIONS:
        return await asyncio.to_thread(extract_text, filename, data, ocr=ocr)
    async with _slots:
        return await _isolated(filename, data, ocr=ocr)


if __name__ == "__main__":
    raise SystemExit(_child_main())
