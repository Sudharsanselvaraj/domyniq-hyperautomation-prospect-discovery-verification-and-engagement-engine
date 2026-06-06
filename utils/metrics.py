"""
utils/metrics.py — Execution Metrics

Tracks per-stage counts and durations. Passed through the orchestrator
so every service can record its own numbers without coupling to each other.

Interview talking point:
  "Metrics are first-class citizens, not an afterthought.
   Having them in a shared object means the final summary report
   is built incrementally as the pipeline runs."
"""

import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class StageMetrics:
    name: str
    input_count: int = 0
    output_count: int = 0
    duration_seconds: float = 0.0
    success: bool = True
    error_count: int = 0
    _start: Optional[float] = field(default=None, repr=False)

    def start(self) -> "StageMetrics":
        self._start = time.monotonic()
        return self

    def finish(self) -> "StageMetrics":
        if self._start is not None:
            self.duration_seconds = time.monotonic() - self._start
        return self


class PipelineMetrics:
    """
    Mutable metrics object threaded through the orchestrator.
    Each stage updates it; main.py reads it for the final report.
    """

    def __init__(self) -> None:
        self.stages: list[StageMetrics] = []
        self.companies_found: int = 0
        self.contacts_found: int = 0
        self.verified_emails: int = 0
        self.emails_sent: int = 0
        self.emails_failed: int = 0
        self._pipeline_start: float = time.monotonic()

    def add_stage(self, name: str) -> StageMetrics:
        """Create, register and return a new stage timer."""
        stage = StageMetrics(name=name)
        self.stages.append(stage)
        return stage

    @property
    def total_duration_seconds(self) -> float:
        return time.monotonic() - self._pipeline_start

    def to_dict(self) -> dict:
        return {
            "companies_found": self.companies_found,
            "contacts_found": self.contacts_found,
            "verified_emails": self.verified_emails,
            "emails_sent": self.emails_sent,
            "emails_failed": self.emails_failed,
            "total_duration_seconds": round(self.total_duration_seconds, 2),
            "stages": [
                {
                    "name": s.name,
                    "input": s.input_count,
                    "output": s.output_count,
                    "duration_s": round(s.duration_seconds, 2),
                    "success": s.success,
                }
                for s in self.stages
            ],
        }
