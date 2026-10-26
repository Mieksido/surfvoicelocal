"""单 worker 优先级调度 + 可取消。

为什么串行：一张卡上并发跑同一个模型只会让显存抖动、单条更慢。
为什么优先级：直播里「打赏语音」要能插到旁白前面。
为什么在线程里跑：模型是阻塞的 Python，塞进事件循环会卡住整个服务。
"""

from __future__ import annotations

import asyncio
import itertools
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Callable


class QueueFull(Exception):
    pass


class Job:
    def __init__(self, priority: int, seq: int):
        self.id = uuid.uuid4().hex[:12]
        self.priority = priority
        self.seq = seq
        self.cancel = threading.Event()
        self.queue: asyncio.Queue = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def emit(self, frame: dict) -> None:
        """供工作线程调用：把帧投回事件循环里的队列。"""
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(self.queue.put_nowait, frame)

    def close(self) -> None:
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(self.queue.put_nowait, None)


class Scheduler:
    def __init__(self, maxsize: int = 64):
        self.maxsize = maxsize
        self._pq: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._seq = itertools.count()
        self._exec = ThreadPoolExecutor(max_workers=1, thread_name_prefix="voxd-gpu")
        self._task: asyncio.Task | None = None
        self._active: Job | None = None
        self._queued: dict[str, Job] = {}

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="voxd-worker")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._exec.shutdown(wait=False, cancel_futures=True)

    def depth(self) -> dict:
        return {"active": self._active.id if self._active else None, "queued": len(self._queued)}

    async def submit(self, run: Callable[[Job], None], priority: int = 0) -> Job:
        if len(self._queued) >= self.maxsize:
            raise QueueFull(f"队列已满（{self.maxsize}）")
        job = Job(priority, next(self._seq))
        job.bind(asyncio.get_running_loop())
        job.run = run  # type: ignore[attr-defined]
        self._queued[job.id] = job
        await self._pq.put((-int(priority), job.seq, job))
        return job

    def cancel(self, job_id: str | None = None) -> int:
        n = 0
        if self._active and job_id in (None, self._active.id):
            self._active.cancel.set()
            n += 1
        for j in list(self._queued.values()):
            if job_id in (None, j.id):
                j.cancel.set()
                n += 1
        return n

    async def _loop(self) -> None:
        while True:
            _, _, job = await self._pq.get()
            self._queued.pop(job.id, None)
            self._active = job
            try:
                if job.cancel.is_set():
                    job.emit({"type": "cancelled"})
                    continue
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(self._exec, job.run, job)
            except Exception as e:  # noqa: BLE001
                job.emit({"type": "error", "error": str(e)})
            finally:
                job.close()
                self._active = None
