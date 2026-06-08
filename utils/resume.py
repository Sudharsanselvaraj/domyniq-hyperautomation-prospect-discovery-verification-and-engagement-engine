"""
utils/resume.py — Resumable Run Checkpointing

Saves pipeline state to data/.checkpoints/<domain>.json after each stage
so a run can be resumed from where it left off.

Stage-level checkpoints are saved after Stage 1 (Ocean.io), Stage 2 (Prospeo),
and Stage 3 (EazyReach) complete. Per-item checkpoints within Stage 2 and 3
allow resuming mid-stage so expensive API calls are not repeated.

Interview talking point:
  "Stage 2 and 3 make one API call per company/contact.
   For 25 companies × 5 contacts that's 125+ calls. If the run
   dies at call 120, checkpointing means we resume from contact 120,
   not start over. This is critical for expensive quota operations."
"""

import json
from pathlib import Path
from typing import Any, Optional

from utils.logger import get_logger

logger = get_logger(__name__)

CHECKPOINT_DIR = Path("data/.checkpoints")


class ResumableRun:
    def __init__(self, domain: str) -> None:
        self.domain = domain.replace("/", "_").replace(".", "_")
        CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
        self._path = CHECKPOINT_DIR / f"{self.domain}.json"
        self._state: dict[str, Any] = self._load()

    def _load(self) -> dict:
        if self._path.exists():
            try:
                return json.loads(self._path.read_text())
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning(f"Could not load checkpoint: {exc}")
        return {}

    def _save(self) -> None:
        try:
            self._path.write_text(json.dumps(self._state, default=str))
        except OSError as exc:
            logger.warning(f"Could not save checkpoint: {exc}")

    def save(self) -> None:
        """Public save hook for signal handlers."""
        self._save()

    def has_checkpoint(self) -> bool:
        return self._path.exists() and bool(self._state)

    def get(self, key: str) -> Optional[Any]:
        return self._state.get(key)

    def set(self, key: str, value: Any) -> None:
        self._state[key] = value
        self._save()

    def clear(self) -> None:
        self._state = {}
        if self._path.exists():
            self._path.unlink()

    # ── Stage-level checkpoints ────────────────────────────────────────────────

    def get_stage_done(self, stage: str) -> bool:
        return self._state.get(f"stage_{stage}_done", False)

    def mark_stage_done(self, stage: str, data: Any) -> None:
        self._state[f"stage_{stage}_done"] = True
        self._state[f"stage_{stage}_data"] = data
        self._save()
        logger.debug(f"Checkpoint saved for stage: {stage}")

    def get_stage_data(self, stage: str) -> Optional[Any]:
        return self._state.get(f"stage_{stage}_data")

    # ── Per-item checkpoints (for mid-stage resume) ────────────────────────────

    def is_item_processed(self, stage: str, item_key: str) -> bool:
        """Check if a single item (company domain, linkedin url, etc.) was already processed."""
        processed = self._state.get(f"stage_{stage}_processed", [])
        return item_key in processed

    def mark_item_processed(self, stage: str, item_key: str, data: Optional[Any] = None) -> None:
        """Mark a single item as processed and optionally store its result."""
        key = f"stage_{stage}_processed"
        if key not in self._state:
            self._state[key] = []
        if item_key not in self._state[key]:
            self._state[key].append(item_key)

        if data is not None:
            results_key = f"stage_{stage}_results"
            if results_key not in self._state:
                self._state[results_key] = []
            self._state[results_key].append(data)

        self._save()

    def get_item_results(self, stage: str) -> list[Any]:
        """Return stored results for a stage's per-item checkpoint."""
        return self._state.get(f"stage_{stage}_results", [])

    def clear_item_checkpoint(self, stage: str) -> None:
        """Clear per-item tracking for a stage (call after stage completes successfully)."""
        self._state.pop(f"stage_{stage}_processed", None)
        self._state.pop(f"stage_{stage}_results", None)
        self._save()
