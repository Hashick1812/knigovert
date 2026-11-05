"""Фоновые задачи: длинная конвертация не должна подвешивать окно."""

from __future__ import annotations

import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Optional

from .model import ConversionError


@dataclass
class Job:
    id: str
    status: str = "queued"  # queued | running | done | error
    stage: str = "В очереди"
    progress: Optional[float] = None
    result: dict = field(default_factory=dict)
    error: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "status": self.status,
            "stage": self.stage,
            "progress": self.progress,
            "result": self.result,
            "error": self.error,
            "warnings": self.warnings,
        }


class JobQueue:
    def __init__(self, workers: int = 2):
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="knigovert")
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def submit(self, work: Callable[[Job], dict]) -> Job:
        job = Job(uuid.uuid4().hex[:12])
        with self._lock:
            self._jobs[job.id] = job
        self._executor.submit(self._run, job, work)
        return job

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    @staticmethod
    def _run(job: Job, work: Callable[[Job], dict]) -> None:
        job.status = "running"
        job.stage = "Начинаю"
        try:
            job.result = work(job)
            job.status = "done"
            job.stage = "Готово"
            job.progress = 1.0
        except ConversionError as error:
            job.error = str(error)
            job.status = "error"
        except MemoryError:
            job.error = "Не хватило памяти: файл слишком большой для этого компьютера."
            job.status = "error"
        except PermissionError:
            job.error = "Нет доступа к папке сохранения или файл открыт в другой программе."
            job.status = "error"
        except Exception as error:  # Ошибка в чужом файле не должна ронять приложение.
            traceback.print_exc()
            job.error = f"Не удалось обработать файл: {error}"
            job.status = "error"

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
