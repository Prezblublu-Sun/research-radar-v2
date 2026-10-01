"""The set of works the corpus already holds — derived, never authoritative.

v1 kept ``seen_dois.json`` as the dedup state and wrote it non-atomically;
a truncated file read back as an empty list and the next run would have
re-fetched and re-scored the whole window. Here the set is *derived* from
the run logs, which are the only thing that is committed. A cache under
``.radar-cache/`` makes repeated local runs fast; CI has no cache and
rebuilds every time (~2 s per year of data). A damaged cache can only cost
a rebuild.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from radar.core.atomic import atomic_write_json
from radar.paths import DataRoot
from radar.store.runs import list_runs, scan_keys

CACHE_NAME = "seen_keys.json"


@dataclass
class SeenKeys:
    keys: set[str] = field(default_factory=set)
    through: str = ""      # name of the newest run file folded in
    n_files: int = 0
    rebuilt: bool = False

    @classmethod
    def rebuild(cls, root: DataRoot) -> "SeenKeys":
        keys: set[str] = set()
        files = list_runs(root)
        for path in files:
            keys |= scan_keys(path)
        return cls(keys=keys, through=files[-1].name if files else "",
                   n_files=len(files), rebuilt=True)

    @classmethod
    def load(cls, root: DataRoot) -> "SeenKeys":
        """Use the cache if it still describes the run directory; else rebuild."""
        files = list_runs(root)
        newest = files[-1].name if files else ""
        cache = root.cache / CACHE_NAME
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            if (data.get("through") == newest and data.get("n_files") == len(files)
                    and isinstance(data.get("keys"), list)):
                return cls(keys=set(data["keys"]), through=newest, n_files=len(files))
        except (OSError, ValueError, AttributeError):
            pass
        return cls.rebuild(root)

    def save_cache(self, root: DataRoot) -> None:
        atomic_write_json(root.cache / CACHE_NAME, {
            "through": self.through, "n_files": self.n_files,
            "keys": sorted(self.keys),
        }, indent=None)

    def __contains__(self, key: str) -> bool:
        return key in self.keys

    def __len__(self) -> int:
        return len(self.keys)
