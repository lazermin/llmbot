#!/usr/bin/env python3
import argparse
import fcntl
import getpass
import io
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from dotenv.parser import parse_stream

from credentials import KEY_NAME, REQUIRE_KEY_ENV

CONFIG = Path("/etc/llmbot")
ENV_FILE = CONFIG / ".env"
CREDENTIAL = CONFIG / f"{KEY_NAME}.cred"
UNIT = "llmbot.service"
DROP_IN = Path("/etc/systemd/system") / f"{UNIT}.d" / "40-llm-api-key.conf"
PROGRAM = Path("/opt/llmbot")


def run(*args: str, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=True, **kwargs)


def check_service(credential: Path, *, env_file: Path | None = None) -> None:
    run(
        "systemd-run", "--quiet", "--wait", "--pipe", "--collect",
        "--property=User=llmbot", "--property=Group=llmbot",
        f"--property=LoadCredentialEncrypted={KEY_NAME}:{credential}",
        "--property=UnsetEnvironment=LLM_API_KEY",
        f"--setenv={REQUIRE_KEY_ENV}=1", f"--setenv=LLMBOT_ENV_FILE={env_file or ENV_FILE}",
        str(PROGRAM / ".venv/bin/python"), str(PROGRAM / "bot.py"), "--check-config",
        stdin=subprocess.DEVNULL,
    )


def atomic_write(path: Path, data: bytes, mode: int, owner: tuple[int, int] | None = None) -> None:
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            if owner is not None:
                os.fchown(stream.fileno(), *owner)
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temp.unlink(missing_ok=True)


def restore_file(path: Path, data: bytes | None, mode: int) -> None:
    if data is None:
        path.unlink(missing_ok=True)
    else:
        atomic_write(path, data, mode)


def remove_plaintext(original: bytes) -> tuple[str, bytes]:
    entries = list(parse_stream(io.StringIO(original.decode("utf-8"), newline="")))
    if any(entry.error for entry in entries):
        raise ValueError("Проверьте синтаксис /etc/llmbot/.env. Файл не изменён.")
    values = [entry.value for entry in entries if entry.key == "LLM_API_KEY" and entry.value is not None]
    clean = "".join(entry.original.string for entry in entries if entry.key != "LLM_API_KEY")
    return (values[-1] if values else "").strip(), clean.encode("utf-8")


def migrate(key_type: str, replace: bool) -> None:
    meta = ENV_FILE.stat()
    if ENV_FILE.is_symlink() or meta.st_nlink != 1:
        raise ValueError("Используйте обычный файл /etc/llmbot/.env без дополнительных ссылок.")
    original = ENV_FILE.read_bytes()
    key, clean_env = remove_plaintext(original)
    previous_credential = CREDENTIAL.read_bytes() if CREDENTIAL.is_file() else None
    previous_drop_in = DROP_IN.read_bytes() if DROP_IN.is_file() else None
    if not replace and not key and previous_credential is not None and previous_drop_in is not None:
        check_service(CREDENTIAL)
        print("LLM_API_KEY уже хранится как зашифрованный credential.")
        return
    if replace or not key:
        if not sys.stdin.isatty():
            raise ValueError("Для ввода нового ключа запустите скрипт в интерактивном терминале.")
        key = getpass.getpass("Новый LLM_API_KEY (ввод скрыт): ").strip()
    if not key or any(character.isspace() for character in key):
        raise ValueError("LLM_API_KEY должен быть непустым и без пробельных символов.")

    encrypted = run(
        "systemd-creds", "encrypt", f"--name={KEY_NAME}", f"--with-key={key_type}", "-", "-",
        input=key.encode("utf-8"), stdout=subprocess.PIPE,
    ).stdout
    if not encrypted.strip():
        raise ValueError("Systemd не сформировал зашифрованный credential.")

    # Check the same file content and permissions the service will see after removal.
    env_owner = (meta.st_uid, meta.st_gid)
    env_mode = meta.st_mode & 0o777
    with tempfile.TemporaryDirectory(prefix=".credential-check-", dir=CONFIG) as temporary:
        directory = Path(temporary)
        os.chown(directory, 0, meta.st_gid)
        directory.chmod(0o750)
        candidate = directory / CREDENTIAL.name
        candidate_env = directory / ENV_FILE.name
        atomic_write(candidate, encrypted, 0o600)
        atomic_write(candidate_env, clean_env, env_mode, env_owner)
        atomic_write(directory / "telegram-format.txt", (CONFIG / "telegram-format.txt").read_bytes(), 0o640, env_owner)
        check_service(candidate, env_file=candidate_env)

    DROP_IN.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    drop_in = (
        f"[Service]\nLoadCredentialEncrypted={KEY_NAME}:{CREDENTIAL}\n"
        f"Environment={REQUIRE_KEY_ENV}=1\nUnsetEnvironment=LLM_API_KEY\n"
    ).encode("utf-8")
    env_changed = False
    try:
        atomic_write(CREDENTIAL, encrypted, 0o600)
        atomic_write(DROP_IN, drop_in, 0o644)
        run("systemctl", "daemon-reload")
        if ENV_FILE.read_bytes() != original:
            raise ValueError("Файл .env был изменён во время настройки. Повторите команду.")
        if clean_env != original:
            env_changed = True
            atomic_write(ENV_FILE, clean_env, env_mode, env_owner)
        run("systemctl", "restart", UNIT)
        run("systemctl", "is-active", "--quiet", UNIT)
    except (OSError, subprocess.CalledProcessError, ValueError):
        if env_changed and ENV_FILE.read_bytes() == clean_env:
            atomic_write(ENV_FILE, original, env_mode, env_owner)
        restore_file(DROP_IN, previous_drop_in, 0o644)
        restore_file(CREDENTIAL, previous_credential, 0o600)
        subprocess.run(("systemctl", "daemon-reload"), check=False)
        subprocess.run(("systemctl", "restart", UNIT), check=False)
        raise
    print("LLM_API_KEY зашифрован, llmbot.service перезапущен.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Зашифровать LLM_API_KEY для системного llmbot.service.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--replace", action="store_true", help="скрыто ввести новый ключ и заменить текущий")
    mode.add_argument("--check-service", action="store_true", help="проверить установленный credential через systemd")
    parser.add_argument("--with-key", choices=("auto", "host", "tpm2", "host+tpm2"), default="auto")
    args = parser.parse_args()
    try:
        if os.geteuid() != 0:
            raise ValueError("Запустите скрипт от root.")
        if not ENV_FILE.is_file() or not (PROGRAM / "bot.py").is_file():
            raise ValueError("Сначала установите или обновите системный llmbot.")
        if any(shutil.which(name) is None for name in ("systemd-creds", "systemd-run", "systemctl")):
            raise ValueError("Требуются systemd-creds, systemd-run и systemctl с поддержкой LoadCredentialEncrypted.")
        with (CONFIG / ".credentials.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if args.check_service:
                if not (DROP_IN.is_file() and CREDENTIAL.is_file()):
                    raise ValueError("Проверьте наличие зашифрованного ключа и дополнения к llmbot.service.")
                check_service(CREDENTIAL)
            else:
                migrate(args.with_key, args.replace)
    except subprocess.CalledProcessError as error:
        print(f"Команда {error.cmd[0]} завершилась с кодом {error.returncode}. Проверьте вывод выше.", file=sys.stderr)
        return 1
    except (OSError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
