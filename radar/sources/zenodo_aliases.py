"""Zenodo concept-DOI resolution with an on-disk cache.

OpenAlex indexes a Zenodo upload twice: once under its *concept* DOI
(``10.5281/zenodo.22057603``, always the latest version) and once under the
*version* DOI (``10.5281/zenodo.22057604``). The two are consecutive integers,
so no string rule can join them; the Zenodo record API is the authority
(``conceptdoi`` in ``GET /api/records/<recid>``).

The cache file (``data/aliases/zenodo.json``)::

    {"schema_version": 1, "zenodo": {"<version_doi>": "<concept_doi>", ...}}

Every DOI in the map, concept DOIs included, maps to its concept DOI so a
cached DOI is never looked up twice. Lookups are bounded per run and stop at
the first network failure so an outage cannot stall the daily job; unknown
DOIs keep their own identity until the next run resolves them.

Ported from v1 ``pipeline/doi_aliases.py`` (ADR-0031).
"""
from __future__ import annotations

import json
import pathlib
import re
import time
from typing import Iterable

import requests

from radar.core import identity
from radar.core.atomic import atomic_write_json

ZENODO_RECORD_API = "https://zenodo.org/api/records/{recid}"
SCHEMA_VERSION = 1
DEFAULT_MAX_LOOKUPS = 100
DEFAULT_PAUSE_SECONDS = 1.0  # Zenodo allows 60 anonymous requests/minute

_ZENODO_RECID = re.compile(r"^10\.5281/zenodo\.(\d+)$")


def load(path: pathlib.Path) -> dict:
    """Read the cache file; a missing or unreadable file yields an empty cache."""
    try:
        data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict):
        data = {}
    zenodo = data.get("zenodo")
    return {
        "schema_version": SCHEMA_VERSION,
        "zenodo": dict(zenodo) if isinstance(zenodo, dict) else {},
    }


def save(path: pathlib.Path, aliases: dict) -> None:
    atomic_write_json(pathlib.Path(path), {
        "schema_version": SCHEMA_VERSION,
        "zenodo": dict(sorted((aliases.get("zenodo") or {}).items())),
    })


def flat(aliases: dict) -> dict:
    """The ``{version_doi: concept_doi}`` map consumed by ``identity``."""
    return dict(aliases.get("zenodo") or {})


def zenodo_recid(doi) -> str:
    match = _ZENODO_RECID.match(identity.normalize_doi(doi))
    return match.group(1) if match else ""


def resolve_zenodo(
    dois: Iterable,
    cache_path: pathlib.Path,
    *,
    timeout: float = 10.0,
    max_lookups: int = DEFAULT_MAX_LOOKUPS,
    pause: float = DEFAULT_PAUSE_SECONDS,
    session=None,
) -> tuple[dict, dict]:
    """Resolve unknown Zenodo DOIs and return ``(flat_alias_map, report)``.

    ``report`` counts ``looked_up`` / ``resolved`` / ``failed`` /
    ``skipped_cap``. The cache file is rewritten only when something new was
    learned.
    """
    aliases = load(cache_path)
    zenodo = aliases["zenodo"]
    report = {"looked_up": 0, "resolved": 0, "failed": 0, "skipped_cap": 0}

    todo: list[str] = []
    for raw in dois:
        doi = identity.normalize_doi(raw)
        if doi and zenodo_recid(doi) and doi not in zenodo and doi not in todo:
            todo.append(doi)

    getter = (session or requests).get
    changed = False
    for doi in todo:
        if doi in zenodo:
            continue  # learned from an earlier lookup in this run
        if report["looked_up"] >= max_lookups:
            report["skipped_cap"] += 1
            continue
        if report["looked_up"] and pause:
            time.sleep(pause)
        report["looked_up"] += 1
        recid = zenodo_recid(doi)
        try:
            resp = getter(ZENODO_RECORD_API.format(recid=recid),
                          timeout=timeout,
                          headers={"Accept": "application/json"})
            status = getattr(resp, "status_code", 200)
            if status == 404:
                zenodo[doi] = doi  # withdrawn or never public: only itself
                changed = True
                report["resolved"] += 1
                continue
            if status >= 400:
                raise RuntimeError(f"HTTP {status}")
            data = resp.json()
        except Exception:
            # Network trouble tends to affect every remaining lookup; stop
            # here and let the next run retry the unresolved DOIs.
            report["failed"] += 1
            break
        concept = identity.normalize_doi((data or {}).get("conceptdoi"))
        own = identity.normalize_doi((data or {}).get("doi"))
        target = concept or doi
        zenodo[doi] = target
        if own and own != doi:
            zenodo[own] = target
        if concept and concept != doi:
            zenodo.setdefault(concept, concept)
        changed = True
        report["resolved"] += 1

    if changed:
        save(cache_path, aliases)
    return flat(aliases), report
