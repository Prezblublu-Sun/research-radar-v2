"""Where data lives. Every module takes a DataRoot explicitly.

v1 bound ``ROOT / "data"`` at import time in several modules, which meant a
test that relocated the data directory could still write into the real one
(it happened). Nothing in v2 computes a data path at import.
"""
from __future__ import annotations

import pathlib
from dataclasses import dataclass


@dataclass(frozen=True)
class DataRoot:
    root: pathlib.Path

    @classmethod
    def at(cls, path) -> "DataRoot":
        return cls(pathlib.Path(path))

    # --- append-only streams -------------------------------------------------
    @property
    def runs(self) -> pathlib.Path:
        return self.root / "runs"

    @property
    def random_reading(self) -> pathlib.Path:
        return self.root / "random_reading"

    @property
    def visuals(self) -> pathlib.Path:
        return self.root / "visuals"

    # --- small rewritten snapshots -------------------------------------------
    @property
    def marks(self) -> pathlib.Path:
        return self.root / "marks"

    @property
    def digest(self) -> pathlib.Path:
        return self.root / "digest"

    @property
    def aliases(self) -> pathlib.Path:
        return self.root / "aliases"

    @property
    def eval(self) -> pathlib.Path:
        return self.root / "eval"

    # --- derived, gitignored --------------------------------------------------
    @property
    def cache(self) -> pathlib.Path:
        """Rebuildable caches live beside data/, not inside it."""
        return self.root.parent / ".radar-cache"

    def run_file(self, run_id: str, run_type: str) -> pathlib.Path:
        """``data/runs/YYYY/<run_id>-<run_type>.jsonl`` — one file per run."""
        return self.runs / run_id[:4] / f"{run_id}-{run_type}.jsonl"
