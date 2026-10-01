"""Shipped browser bundles and their content-hashed URLs.

GitHub Pages serves static files with ``Cache-Control: max-age=600``; a
bundle referenced by bare name reached a returning reader up to ten minutes
late (longer with a tab left open). Every reference carries the file's
content hash, so a changed bundle is a changed URL.
"""
from __future__ import annotations

import functools
import hashlib
import pathlib
import shutil

STATIC_DIR = pathlib.Path(__file__).resolve().parent / "static"
BUNDLES = ("radar-ui.css", "radar-ui.js", "radar-card.js", "radar-day.js",
           "radar-queue.js", "radar-search.js", "radar-search-worker.js",
           "radar-reading.js", "radar-workbench.js", "radar-random.js")


@functools.lru_cache(maxsize=None)
def asset_version(name: str) -> str:
    try:
        return hashlib.sha256((STATIC_DIR / name).read_bytes()).hexdigest()[:10]
    except OSError:
        return ""


def asset(name: str) -> str:
    version = asset_version(name)
    return f"{name}?v={version}" if version else name


def copy_static(out_dir: pathlib.Path) -> list[str]:
    out_dir = pathlib.Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    copied = []
    for name in BUNDLES:
        src = STATIC_DIR / name
        if src.exists():
            shutil.copyfile(src, out_dir / name)
            copied.append(name)
    return copied


def versions() -> dict[str, str]:
    return {name: asset_version(name) for name in BUNDLES if asset_version(name)}
