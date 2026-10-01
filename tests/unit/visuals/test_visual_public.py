"""The public visual boundary (ported from v1 test_visual_registry_join)."""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from radar.visuals import public as vp


DIRECTIONS = {
    "fea_surrogate": {
        "display_name": "FEA & Surrogate Modelling",
        "color": "#345678",
    }
}

THIRD_PARTY_PRODUCTION_CAPTIONS = (
    "Copyright: Dietmar Schulze.",
    "Created with BioRender.com (License number: AV27FQ9RWZ).",
    "Created in BioRender. Crook, J. (https://BioRender.com/e8aq7lf).",
    "Principle of SBO, reproduced from [12].",
    "Figure 3: From Keil et al. 2021: On the floor with domain Ω.",
    "Reference measurements from Choi et al. of wake characteristics.",
    "Biomaterial ink synthesis workflow (made using Illustrae [29]).",
    "Topographic map of water catchment (45, source:).",
    "BEAR for studying the mechanics of additively manufactured components. "
    "(Photo credit: Aldair E. Gongora and Bowen Xu, Boston University).",
    "Image by Example Artist.",
    "Photograph by Example Photographer.",
    "Illustration by Example Studio.",
    "Graphic—by Example Agency.",
    "The three-dimensional printing protocol developed by MX3D uses a weld "
    "head attached to a robotic arm (image by Joris Laarman, "
    "www.jorislaarman.com).",
)


def _available_visual(*, image_url: str, source_url: str,
                      license_name: str = "CC BY 4.0") -> dict:
    return {
        "status": "available",
        "image_url": image_url,
        "caption": "Figure caption",
        "source_label": "Figure 1",
        "source_url": source_url,
        "license": license_name,
        "alt": "A research figure",
        "width": 1200,
        "height": 800,
        "checked_at": "2026-08-12T10:00:00Z",
        "provider": "must-not-be-public",
        "reason": "must-not-be-public",
        "selector_version": 2,
        "selector_error_at": "2026-08-12T11:00:00Z",
        "selector_error_reason": "must-not-be-public",
    }


def _write_registry(data_dir: pathlib.Path, folder: str,
                    records: dict[str, dict]) -> None:
    target = data_dir / folder
    target.mkdir(parents=True, exist_ok=True)
    (target / "index.json").write_text(json.dumps({
        "schema_version": "v1",
        "updated_at": "2026-08-12T10:00:00Z",
        "records": records,
    }), encoding="utf-8")


def _paper() -> dict:
    return {
        "source": "openalex",
        "doi": "10.1234/visual",
        "title": "Visual registry paper",
        "abstract": "Synthetic abstract.",
        "authors": ["A. Author"],
        "venue": "Test Venue",
        "year": 2026,
        "date": "2026-08-12",
        "direction": "fea_surrogate",
        "direction_name": "FEA & Surrogate Modelling",
        "first_seen_at": "2026-08-12T03:00:00Z",
        "llm": {
            "priority": "High",
            "relevance_level": "Direct",
            "summary_zh": {},
            "summary_en": {},
            "flags": {},
        },
    }


def _write_corpus(data_dir: pathlib.Path) -> None:
    daily = data_dir / "daily"
    daily.mkdir(parents=True, exist_ok=True)
    paper = _paper()
    (daily / "2026-08-12.json").write_text(json.dumps({
        "schema_version": "v2",
        "date": "2026-08-12",
        "date_precision": "day",
        "papers": [paper],
    }), encoding="utf-8")
    manifests = data_dir / "manifests"
    manifests.mkdir(parents=True, exist_ok=True)
    (manifests / "2026-08-12.json").write_text(json.dumps({
        "run_status": "success",
        "quality_flags": [],
    }), encoding="utf-8")


def test_public_svg_visual_requires_verified_arxiv_shape_and_dimensions():
    base = {
        **_available_visual(
            image_url=(
                "https://arxiv.org/html/2608.12345v2/figures/workflow.svg"
            ),
            source_url="https://arxiv.org/abs/2608.12345",
        ),
        "provider": "arxiv",
        "media_type": "image/svg+xml",
        "width": 900,
        "height": 600,
    }
    safe = vp.safe_visual_record(base)
    assert safe is not None
    assert safe["media_type"] == "image/svg+xml"

    unsafe_variants = (
        {"media_type": "image/png"},
        {"provider": "pmc"},
        {"image_url": "https://export.arxiv.org/html/2608.12345v2/f.svg"},
        {"image_url": "https://arxiv.org/html/2608.12345/f.svg"},
        {"image_url": "https://arxiv.org/html/2608.12345v0/f.svg"},
        {"image_url": "https://arxiv.org/html/2608.54321v1/f.svg"},
        {"image_url": "https://arxiv.org/html/2608.12345v2/../evil.svg"},
        {"image_url": "https://arxiv.org/html/2608.12345v2//evil.svg"},
        {"image_url": "https://arxiv.org:443/html/2608.12345v2/f.svg"},
        {"image_url": "https://arxiv.org/html/2608.12345v2/f.svg?raw=1"},
        {"image_url": "https://arxiv.org/html/2608.12345v2/f%2esvg"},
        {"source_url": "https://publisher.example/article"},
        {"source_url": "https://arxiv.org/abs/2608.12345?download=1"},
        {"caption": "Figure 1: Architecture adopted from [23]."},
        {"width": 20, "height": 20},
        {"width": None},
    )
    for changes in unsafe_variants:
        assert vp.safe_visual_record({**base, **changes}) is None


def test_public_raster_media_type_must_match_file_suffix():
    base = _available_visual(
        image_url="https://arxiv.org/html/2608.12345/figure.png",
        source_url="https://arxiv.org/abs/2608.12345",
    )
    assert vp.safe_visual_record({
        **base, "media_type": "image/png",
    }) is not None
    assert vp.safe_visual_record({
        **base, "media_type": "image/jpeg",
    }) is None


def test_public_mdpi_raster_requires_official_matching_work():
    image_base = (
        "https://mdpi-res.com/d_attachment/buildings/buildings-16-02321/"
        "article_deploy/html/images/buildings-16-02321-g001-550.jpg"
    )
    source_base = "https://www.mdpi.com/2075-5309/16/12/2321"
    base = {
        **_available_visual(image_url=image_base, source_url=source_base),
        "provider": "mdpi",
        "media_type": "image/jpeg",
    }
    safe = vp.safe_visual_record(base)
    assert safe is not None
    assert safe["image_url"] == image_base
    assert safe["source_url"] == source_base
    assert safe["media_type"] == "image/jpeg"
    assert safe["provider"] == "mdpi"
    without_media_type = dict(base)
    without_media_type.pop("media_type")
    safe_without_media_type = vp.safe_visual_record(
        without_media_type
    )
    assert safe_without_media_type is not None
    assert "media_type" not in safe_without_media_type
    assert safe_without_media_type["provider"] == "mdpi"

    old_png = {
        **_available_visual(
            image_url=(
                "https://mdpi-res.com/d_attachment/metals/metals-06-00166/"
                "article_deploy/html/images/metals-06-00166-g001.png"
            ),
            source_url="https://www.mdpi.com/2075-4701/6/7/166",
        ),
        "provider": "mdpi",
        "media_type": "image/png",
    }
    assert vp.safe_visual_record(old_png) is not None

    for slug, issn in (
        ("applsci", "2076-3417"),
        ("buildings", "2075-5309"),
        ("coatings", "2079-6412"),
        ("designs", "2411-9660"),
        ("jmmp", "2504-4494"),
        ("metals", "2075-4701"),
        ("psf", "2673-9984"),
    ):
        mapped = {
            **_available_visual(
                image_url=(
                    f"https://mdpi-res.com/d_attachment/{slug}/"
                    f"{slug}-16-02321/article_deploy/html/images/"
                    f"{slug}-16-02321-g001-550.jpg"
                ),
                source_url=f"https://www.mdpi.com/{issn}/16/12/2321",
            ),
            "provider": "mdpi",
        }
        assert vp.safe_visual_record(mapped) is not None

    for missing_dimension in ("width", "height"):
        missing = dict(base)
        missing.pop(missing_dimension)
        assert vp.safe_visual_record(missing) is None

    unsafe_variants = (
        {"provider": None},
        {"provider": "pmc"},
        {"image_url": image_base.replace("mdpi-res.com", "images.example.com")},
        {"image_url": image_base.replace("mdpi-res.com", "cdn.mdpi-res.com")},
        {"image_url": image_base.replace("https://", "http://")},
        {"image_url": image_base.replace("mdpi-res.com", "mdpi-res.com:444")},
        {"image_url": image_base.replace("mdpi-res.com", "user@mdpi-res.com")},
        {"image_url": image_base.replace("mdpi-res.com", "mdpi-res.com:443")},
        {"image_url": image_base + "?download=1"},
        {"image_url": image_base.replace("-550.jpg", ".jpg")},
        {"image_url": image_base.replace("g001", "g000")},
        {"image_url": image_base.replace("g001-550.jpg", "g001-550.png")},
        {"image_url": image_base.replace("buildings-16-02321-g001", "buildings-16-02322-g001")},
        {"image_url": image_base.replace("buildings-16-02321/", "buildings-16-02322/")},
        {"source_url": source_base.replace("www.mdpi.com", "mdpi.com")},
        {"source_url": source_base.replace("https://", "http://")},
        {"source_url": source_base.replace("www.mdpi.com", "www.mdpi.com:444")},
        {"source_url": source_base.replace("www.mdpi.com", "user@www.mdpi.com")},
        {"source_url": source_base.replace("www.mdpi.com", "www.mdpi.com:443")},
        {"source_url": source_base + "?type=check_update&version=1"},
        {"source_url": source_base.replace("2075-5309", "2076-3417")},
        {"source_url": source_base.replace("/16/12/2321", "/15/12/2321")},
        {"source_url": source_base.replace("/16/12/2321", "/16/12/2322")},
        {"media_type": "image/png"},
        {"width": 20, "height": 20},
        {"width": True},
        {"height": False},
        {"width": 100_001},
        {"height": 100_001},
    )
    for changes in unsafe_variants:
        assert vp.safe_visual_record({**base, **changes}) is None


def test_public_visual_boundary_rejects_rights_in_caption_or_alt():
    base = _available_visual(
        image_url="https://arxiv.org/html/2608.12345/figure.png",
        source_url="https://arxiv.org/abs/2608.12345",
    )
    for text in THIRD_PARTY_PRODUCTION_CAPTIONS:
        assert vp.safe_visual_record({
            **base, "caption": text, "alt": "Scientific result",
        }) is None
        assert vp.safe_visual_record({
            **base, "caption": "Scientific result", "alt": text,
        }) is None

    assert vp.safe_visual_record({
        **base,
        "caption": "Figure 1: Scientific result.",
        "alt": "Stress field",
        "source_label": "Photograph by Example Photographer",
    }) is None

    assert vp.safe_visual_record({
        **base,
        "caption": "Figure 1: Scientific result.",
        "alt": "Stress field",
        "source_label": "Image by Example Artist",
    }) is None

    assert vp.safe_visual_record({
        **base,
        "caption": "Figure 1: Image generated by the surrogate model.",
        "alt": "Stress field",
        "source_label": "arXiv · 论文插图",
    }) is not None

    for direct_credit in (
        "Diagram by: 'ana pérez'.",
        "Artwork by © naïve atelier.",
        "Photograph created by mélange studio.",
        "Figure made by “mixedCase collective”.",
        "Illustration provided by 株式会社アート.",
        "Graphic supplied by 李明.",
    ):
        assert vp.safe_visual_record({
            **base, "caption": direct_credit, "alt": "Scientific result",
        }) is None

    for scientific_phrase in (
        "Figure 2: image by Fourier transformation.",
        "Figure 2: image by inverse Fourier transformation.",
        "Figure 2: image by FFT.",
        "Figure 2: image by PCA.",
        "Figure 2: image by finite element analysis.",
        "Figure 2: image by Bayesian optimization.",
        "Figure 2: image by Design A.",
        "Figure 2: Image by Applying a Fourier transform.",
        "Figure 2: image by applying a Gaussian filter.",
        "Figure 2: image generated by the surrogate model.",
        "Figure 2: image produced by model predictions.",
        "The image bytes are decoded before plotting.",
        "Each image byte is normalized independently.",
        "The figure bypasses the interpolation stage.",
        "The graphic byproduct is removed during preprocessing.",
    ):
        assert vp.safe_visual_record({
            **base, "caption": scientific_phrase,
            "alt": "Scientific result",
        }) is not None


def test_public_visual_boundary_requires_caption_but_not_useful_alt():
    base = _available_visual(
        image_url="https://arxiv.org/html/2608.12345/figure.png",
        source_url="https://arxiv.org/abs/2608.12345",
    )
    for caption in (
        "", "   ", "[Uncaptioned image]", "See caption.",
        "Figure 1:", "Graphical abstract:", "[No caption available]",
        "Uncaptioned photograph",
    ):
        assert vp.safe_visual_record({
            **base, "caption": caption,
        }) is None

    safe = vp.safe_visual_record({
        **base,
        "caption": "Figure 1: Validated scientific result.",
        "alt": "[Uncaptioned image]",
    })
    assert safe is not None
    assert "alt" not in safe


def test_public_visual_boundary_temporarily_hides_arxiv_table_thumbnails():
    base = _available_visual(
        image_url="https://arxiv.org/html/2608.12345/figure.png",
        source_url="https://arxiv.org/abs/2608.12345",
    )
    for caption in (
        "Table 1: Simulation datasets used for experiments.",
        "Tab. IV: Overview of the material parameters.",
    ):
        assert vp.safe_visual_record({
            **base, "provider": "arxiv", "caption": caption,
        }) is None

    for caption in (
        "Figure 2: Results are summarized in Table 1.",
        "Figure 3: Normalized prediction error.",
        "The table-driven method produces this validated stress field.",
    ):
        assert vp.safe_visual_record({
            **base, "provider": "arxiv", "caption": caption,
        }) is not None

    # JATS figures have their own structural selector; a PMC scientific figure
    # is not suppressed merely because its caption happens to start this way.
    assert vp.safe_visual_record({
        **base, "provider": "pmc",
        "caption": "Table 1: Simulation datasets used for experiments.",
    }) is not None


