"""
utils/resume.py — Resumable Run Checkpointing

Saves pipeline state to data/.checkpoints/<domain>.json after each stage
so a run can be resumed from where it left off.

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

    def get_stage_done(self, stage: str) -> bool:
        return self._state.get(f"stage_{stage}_done", False)

    def mark_stage_done(self, stage: str, data: Any) -> None:
        self._state[f"stage_{stage}_done"] = True
        self._state[f"stage_{stage}_data"] = data
        self._save()
        logger.debug(f"Checkpoint saved for stage: {stage}")

    def get_stage_data(self, stage: str) -> Optional[Any]:
        return self._state.get(f"stage_{stage}_data")
