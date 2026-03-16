"""State Persistence - Save and restore bot state for crash recovery."""

import json
import os
import time
from typing import Optional

from utils.logger import get_logger

logger = get_logger("persistence")

STATE_FILE = "data/bot_state.json"


class StatePersistence:
    """Persists bot state to disk for crash recovery."""

    def __init__(self, state_file: str = STATE_FILE):
        self._state_file = state_file
        self._state_dir = os.path.dirname(state_file)

    def save(self, state: dict):
        """Save state to disk."""
        try:
            if self._state_dir:
                os.makedirs(self._state_dir, exist_ok=True)

            state["_saved_at"] = time.time()
            state["_saved_at_str"] = time.strftime("%Y-%m-%d %H:%M:%S")

            # Write to temp file first, then rename (atomic)
            tmp_file = self._state_file + ".tmp"
            with open(tmp_file, "w") as f:
                json.dump(state, f, indent=2, default=str)
            os.replace(tmp_file, self._state_file)

            logger.debug("State saved to disk")
        except Exception as e:
            logger.error(f"Failed to save state: {e}")

    def load(self) -> Optional[dict]:
        """Load state from disk."""
        try:
            if not os.path.exists(self._state_file):
                return None

            with open(self._state_file, "r") as f:
                state = json.load(f)

            saved_at = state.get("_saved_at", 0)
            age = time.time() - saved_at
            logger.info(
                f"Loaded saved state from {state.get('_saved_at_str', 'unknown')} "
                f"({age:.0f}s ago)"
            )
            return state
        except Exception as e:
            logger.error(f"Failed to load state: {e}")
            return None

    def clear(self):
        """Clear saved state."""
        try:
            if os.path.exists(self._state_file):
                os.remove(self._state_file)
                logger.info("Saved state cleared")
        except Exception as e:
            logger.error(f"Failed to clear state: {e}")
