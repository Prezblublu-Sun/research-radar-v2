"""Work identity: the one definition of "is this the same paper".

Exact identifiers only. Every rule maps identifiers that the issuing registry
itself declares to be the *same work*; no title, author or date similarity is
ever used (strict dedup — fuzzy matching has too many edge cases). Rules, in
precedence order:

1. DOI (case-folded, resolver prefixes stripped). Three registry-specific
   canonical forms: ``10.48550/arxiv.<id>`` is the DOI arXiv mints for its own
   preprint, so it *is* the arXiv id; figshare ``…figshare.<n>.v<k>`` is a
   version of concept ``…figshare.<n>``; Zenodo version DOIs are distinct
   integers from their concept DOI and go through an alias table resolved
   from the Zenodo API (``radar.sources.zenodo_aliases``).
2. arXiv id with the ``vN`` suffix removed.
3. PubMed PMID.
4. OpenAlex work id.

Two keys, two jobs:

* ``canonical_key`` decides *which records are the same work* (dedup).
* ``identity_key`` is the *public* key — ``doi:<raw>`` / ``arxiv:<raw>`` —
  that names anchors, browser marks and the visual registry. It keeps the
  arXiv version and the DOI's original case, and it never changes shape.

Ported from v1 ``render/identity.py`` (ADR-0031) and ``v2_schema.identity_key``,
which v1 defined in two places.
"""
from __future__ import annotations

import re

ARXIV_DOI_PREFIX = "10.48550/arxiv."
FIGSHARE_PREFIX = "10.6084/m9.figshare."
ZENODO_PREFIX = "10.5281/zenodo."

_DOI_URL_PREFIXES = (
    "https://doi.org/", "http://doi.org/",
    "https://dx.doi.org/", "http://dx.doi.org/",
    "doi:",
)
_FIGSHARE_VERSION = re.compile(r"\.v\d+$")
_ARXIV_VERSION = re.compile(r"v\d+$", re.IGNORECASE)
_ARXIV_URL = re.compile(r"arxiv\.org/(?:abs|pdf)/([^\s?#]+)", re.IGNORECASE)
_OPENALEX_WORK = re.compile(r"^W\d+$")


def normalize_doi(doi) -> str:
    """Lower-case a DOI and strip resolver / ``doi:`` prefixes."""
    text = str(doi or "").strip()
    low = text.lower()
    for prefix in _DOI_URL_PREFIXES:
        if low.startswith(prefix):
            low = low[len(prefix):].strip()
            break
    return low


def arxiv_base_id(arxiv_id) -> str:
    """``2609.08102v1`` -> ``2609.08102``; accepts ``arXiv:`` and URL forms."""
    text = str(arxiv_id or "").strip()
    if not text:
        return ""
    if "arxiv.org/" in text.lower():
        return arxiv_id_from_url(text)
    if text.lower().startswith("arxiv:"):
        text = text[len("arxiv:"):].strip()
    text = text.strip("/")
    if text.lower().endswith(".pdf"):
        text = text[:-4]
    return _ARXIV_VERSION.sub("", text).lower()


def arxiv_id_from_url(url) -> str:
    """Extract the arXiv id from an ``arxiv.org/abs|pdf/<id>`` URL, or ``""``."""
    match = _ARXIV_URL.search(str(url or ""))
    if not match:
        return ""
    ident = match.group(1).strip("/")
    if ident.lower().endswith(".pdf"):
        ident = ident[:-4]
    return _ARXIV_VERSION.sub("", ident).lower()


def canonical_doi(doi, aliases: dict | None = None) -> str:
    """Registry-aware canonical DOI (empty string when there is no DOI)."""
    d = normalize_doi(doi)
    if not d:
        return ""
    if d.startswith(FIGSHARE_PREFIX):
        return _FIGSHARE_VERSION.sub("", d)
    if d.startswith(ZENODO_PREFIX) and aliases:
        return normalize_doi(aliases.get(d) or d)
    return d


def openalex_work_id(paper: dict) -> str:
    """``W123`` from ``openalex_id`` or, for OpenAlex records, from ``id``."""
    raw = str(paper.get("openalex_id") or "").strip()
    if not raw and str(paper.get("source") or "") == "openalex":
        raw = str(paper.get("id") or "").strip()
    if not raw:
        return ""
    raw = raw.rstrip("/").rsplit("/", 1)[-1].upper()
    return raw if _OPENALEX_WORK.match(raw) else ""


def canonical_key(paper: dict, aliases: dict | None = None) -> str:
    """The canonical identity of a paper record, or ``""``.

    ``aliases`` is the flat ``{version_doi: concept_doi}`` map maintained by
    ``radar.sources.zenodo_aliases``. Keys are ``doi:``, ``arxiv:``, ``pmid:``
    or ``openalex:`` prefixed, all lower-case.
    """
    doi = normalize_doi(paper.get("doi"))
    if doi:
        if doi.startswith(ARXIV_DOI_PREFIX):
            base = arxiv_base_id(doi[len(ARXIV_DOI_PREFIX):])
            if base:
                return f"arxiv:{base}"
        return f"doi:{canonical_doi(doi, aliases)}"
    arxiv = arxiv_base_id(paper.get("arxiv_id"))
    if arxiv:
        return f"arxiv:{arxiv}"
    pmid = str(paper.get("pmid") or "").strip()
    if pmid.isdigit():
        return f"pmid:{pmid}"
    work = openalex_work_id(paper)
    if work:
        return f"openalex:{work}"
    return ""


def identity_key(paper: dict) -> str:
    """The public key: ``doi:<doi>`` or ``arxiv:<arxiv_id>`` or ``""``.

    Names anchors, browser marks and the visual registry, so it keeps the
    raw DOI case and the arXiv version and never changes shape. Use
    :func:`canonical_key` to decide whether two records are one work.
    """
    doi = (paper.get("doi") or "").strip()
    if doi:
        return f"doi:{doi}"
    arxiv = (paper.get("arxiv_id") or "").strip()
    if arxiv:
        return f"arxiv:{arxiv}"
    return ""
