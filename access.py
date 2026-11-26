import asyncio
import logging
import os
import sqlite3
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

LOG = logging.getLogger("llmbot")
MAX_QUEUE = 12
MAX_CONCURRENT = 4


def state_dir() -> Path:
    configured = os.getenv("LLMBOT_STATE_DIR") or os.getenv("STATE_DIRECTORY")
    if configured:
        return Path(configured)
    return Path(os.getenv("XDG_STATE_HOME") or Path.home() / ".local/state") / "llmbot"


class Access:
    def __init__(self, owner: int, database: Path | None = None) -> None:
        self.owner = owner
        if database is not None:
            database.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(database, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
            database.chmod(0o600)
        self.db = sqlite3.connect(database if database is not None else ":memory:")
        with self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS allowed (user_id INTEGER PRIMARY KEY, label TEXT NOT NULL)")

    def permits(self, user_id: int) -> bool:
        return user_id == self.owner or self.db.execute(
            "SELECT 1 FROM allowed WHERE user_id = ?", (user_id,)
        ).fetchone() is not None

    def allow(self, user_id: int, label: str) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO allowed VALUES (?, ?)", (user_id, label))

    def revoke(self, user_id: int) -> bool:
        with self.db:
            return self.db.execute("DELETE FROM allowed WHERE user_id = ?", (user_id,)).rowcount > 0

    def users(self) -> list[tuple[int, str]]:
        return self.db.execute("SELECT user_id, label FROM allowed ORDER BY user_id").fetchall()


@dataclass
class Work:
    pending: deque[Callable[[], Awaitable]] = field(default_factory=deque)
    task: asyncio.Task | None = None


class Jobs:
    def __init__(self) -> None:
        self.work: dict[int, Work] = {}
        self.slots = asyncio.Semaphore(MAX_CONCURRENT)
        self.tasks: set[asyncio.Task] = set()

    def submit(self, user_id: int, operation: Callable[[], Awaitable]) -> bool:
        work = self.work.get(user_id)
        if work is not None:
            if len(work.pending) >= MAX_QUEUE:
                return False
            work.pending.append(operation)
            return True
        work = self.work[user_id] = Work(deque([operation]))
        task = work.task = asyncio.create_task(self.run(user_id, work))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return True

    async def run(self, user_id: int, work: Work) -> None:
        try:
            while work.pending:
                operation = work.pending.popleft()
                try:
                    async with self.slots:
                        await operation()
                except Exception as error:
                    # Provider errors may include submitted material; log only their type.
                    LOG.warning("Ошибка при обработке запроса (%s).", type(error).__name__)
        finally:
            if self.work.get(user_id) is work:
                del self.work[user_id]

    def stop(self, user_id: int) -> bool:
        work = self.work.pop(user_id, None)
        if work is None:
            return False
        work.pending.clear()
        work.task.cancel()
        return True

    async def close(self) -> None:
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
