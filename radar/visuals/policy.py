"""Shared fail-closed rights policy for public paper figures.

Article-level Creative Commons metadata does not necessarily cover every
figure.  Captions and alt text can carry narrower third-party credits, so the
enricher and the public renderer must apply the same conservative predicate.
"""

from __future__ import annotations

import re


_FIXED_RIGHTS_MARKERS = (
    "all rights reserved",
    "not included in the creative commons",
    "not covered by the creative commons",
    "excluded from the creative commons",
    "third-party material",
    "third party material",
)

_CREATION_PLATFORM = (
    r"(?:bio\s*render(?:\.com)?|illustrae|mind\s+the\s+graph|"
    r"servier\s+medical\s+art|shutterstock|adobe\s+stock|"
    r"istock(?:photo)?|getty\s+images?|freepik|canva)"
)

# Direct asset credits are a separate grammar from ordinary scientific
# passive voice.  The credit itself is deliberately Unicode/case agnostic;
# names and organisations can legitimately be lowercase, mixed-case, or use
# non-Latin scripts.  Method objects are filtered after matching rather than
# using capitalization as a proxy for authorship.
_DIRECT_ASSET_BYLINE = re.compile(
    r"\b(?:image|photo(?:graph)?|figure|illustration|graphic|diagram|artwork)"
    r"\s*(?:[-–—,:]\s*)?"
    r"(?:(?:created|made|provided|supplied)\s+)?by\b\s*"
    r"(?:[:：]\s*)?(?:[\"'“”‘’]\s*)?(?:©\s*)?"
    r"(?:[\"'“”‘’]\s*)?(?P<credit>\w[^.;\n]{0,159})",
    re.IGNORECASE,
)

_SCIENTIFIC_BY_OBJECT = re.compile(
    r"^(?:(?:the|a|an)\s+)?(?:"
    r"apply(?:ing)?\b|using\b|via\b|means\s+of\b|"
    r"(?:inverse\s+)?fourier\s+(?:transform(?:ation)?|analysis)\b|"
    r"(?:fft|pca)\b|finite[- ]element\s+analysis\b|"
    r"bayesian\s+optim(?:ization|isation)\b|"
    r"design\s+[a-z0-9]+\b|"
    r"(?:generated|produced|computed|predicted|reconstructed)\s+by\b|"
    r"(?:surrogate\s+|neural\s+|finite[- ]element\s+)?"
    r"(?:model|network|solver|algorithm|method|approach|process|"
    r"simulation|experiment|dataset|transformation|transform|filter)\b|"
    r"(?:varying|combining|computing|solving|transforming|filtering|"
    r"interpolating|normalizing|plotting|mapping|sampling|training|"
    r"optimizing|evaluating|setting|selecting|rotating|scaling|"
    r"convolving)\b)",
    re.IGNORECASE,
)

_RIGHTS_PATTERNS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    # Copyright words and symbols are explicit enough to fail closed even
    # when converter punctuation has been lost or changed.
    r"©",
    r"\bcopyright(?:ed)?\b",

    # Common attribution grammar, including punctuation between the action
    # and source/permission.  Keep the span bounded to avoid matching two
    # unrelated statements in a long scientific caption.
    r"\b(?:reproduced|reprinted|adapted)\b.{0,80}"
    r"\b(?:from|with\s+(?:kind\s+)?permission)\b",
    r"\bwith\s+(?:kind\s+)?permission\b",
    r"\bcourtesy\s*(?::|of\b)",
    r"\b(?:image|photo(?:graph)?|figure|illustration)\s*"
    r"(?:[-–—,:]\s*)?credits?\b",
    r"\b(?:modified|redrawn)\s+from\b",
    r"\btaken\s+from\s+(?!(?:(?:the|an?)\s+)?(?:data(?:set)?|test\s+set|"
    r"simulation|experiment|model|sample|field|solution|measurement)s?\b)",

    # Explicit author/source credits.  Do not reject arbitrary ``from``:
    # captions legitimately describe data, predictions, and examples from a
    # method or dataset.  These forms are narrow attribution structures seen
    # in production arXiv captions.
    r"^(?:(?:fig(?:ure)?\.?)\s*[a-z0-9.()_-]+\s*[:.,-]?\s*)?"
    r"from\s+(?-i:[A-Z])[\w'’-]*"
    r"(?:\s+(?-i:[A-Z])[\w'’-]*)*\s+et\s+al\.",
    r"\breference\s+measurements?\s+from\s+"
    r"(?-i:[A-Z])[\w'’-]*(?:\s+et\s+al\.)?",

    # Named diagram/stock platforms are distinct enough to block only when
    # paired with creation/credit language, or an explicit licence credit.
    rf"\b(?:created|made|prepared|drawn|illustrated|generated|designed)\s+"
    rf"(?:with|in|using|via|by|on)\s+(?:the\s+)?{_CREATION_PLATFORM}\b",
    rf"\b{_CREATION_PLATFORM}\b.{{0,80}}\b(?:licen[cs]e|credit|attribution)\b",
    rf"\b(?:stock\s+)?(?:image|photo|photograph|illustration|graphic)\b"
    rf".{{0,50}}\b(?:from|courtesy\s+of)\s+(?:the\s+)?{_CREATION_PLATFORM}\b",
    r"\bstock\s+(?:image|photo(?:graph)?|illustration|graphic)\b",

    # Broken converter output observed online: ``(45, source:)``.  Requiring
    # the parenthesized numeric citation avoids rejecting scientific phrases
    # such as ``source function`` or ``data source: simulation``.
    r"\(\s*(?:\[\s*)?\d+(?:\s*\])?\s*,\s*source\s*:\s*\)",
))

_PLACEHOLDER_CAPTION = re.compile(
    r"[\[(]?\s*(?:uncaptioned\s+(?:image|figure|graphic|photo(?:graph)?)|"
    r"(?:refer|see)\s+(?:to\s+)?(?:the\s+)?caption|"
    r"no\s+caption(?:\s+available)?|"
    r"(?:without|missing)\s+caption|graphical\s+abstract|"
    r"(?:fig(?:ure)?\.?\s*\d+[a-z]?|image|graphic))"
    r"\s*[\])]?[.:!]?",
    re.IGNORECASE,
)


def has_third_party_figure_rights(*values: object) -> bool:
    """Return true when caption/alt text carries third-party rights risk."""
    for value in values:
        if not isinstance(value, str) or not value.strip():
            continue
        compact = " ".join(value.split())
        lower = compact.casefold()
        if any(marker in lower for marker in _FIXED_RIGHTS_MARKERS):
            return True
        for match in _DIRECT_ASSET_BYLINE.finditer(compact):
            credit = match.group("credit").strip()
            if credit and not _SCIENTIFIC_BY_OBJECT.match(credit):
                return True
        if any(pattern.search(compact) for pattern in _RIGHTS_PATTERNS):
            return True
    return False


def has_reviewable_figure_caption(value: object) -> bool:
    """Return true only for non-empty, non-placeholder figure captions."""
    if not isinstance(value, str):
        return False
    compact = " ".join(value.split()).strip()
    return bool(compact) and _PLACEHOLDER_CAPTION.fullmatch(compact) is None
