from __future__ import annotations

from dataclasses import dataclass
from threading import Lock
from typing import Any
from uuid import uuid4


@dataclass
class BacktestJob:
    job_id: str
    status: str = "queued"
    progress: float = 0.0
    message: str = "等待开始"
    result: Any = None
    error: str | None = None


class BacktestJobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, BacktestJob] = {}
        self._lock = Lock()

    def create(self) -> BacktestJob:
        with self._lock:
            job = BacktestJob(job_id=str(uuid4()))
            self._jobs[job.job_id] = job
            return job

    def get(self, job_id: str) -> BacktestJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def update(self, job_id: str, **kwargs) -> BacktestJob | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            for key, value in kwargs.items():
                if hasattr(job, key) and value is not None:
                    setattr(job, key, value)
            if isinstance(job.progress, (int, float)):
                job.progress = max(0.0, min(100.0, float(job.progress)))
            return job


job_store = BacktestJobStore()
