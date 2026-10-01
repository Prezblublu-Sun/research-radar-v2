"""Issue sync and the digest watermark semantics (ported from v1)."""
from __future__ import annotations

import json

from radar.marks import apply_sync, digest
from radar.store import marks as ms


def _mark(state="to-read", at="2026-09-22T11:00:00Z", **extra):
    return {"state": state, "at": at, **extra}


def _write(root, device, **marks):
    ms.write_device(root, ms.validate_payload({"schema_version": 1, "device": device, "marks": marks}))


# --- apply ----------------------------------------------------------------------

def test_apply_writes_the_device_file_and_summarises(data_root):
    body = "```json\n" + json.dumps({"schema_version": 1, "device": "dev-1a2b", "marks": {
        "doi:10.1/a": _mark(note="n"), "doi:10.1/b": _mark("read")}}) + "\n```"
    result = apply_sync.apply(body, data_root)
    assert result.ok and (data_root.marks / "dev-1a2b.json").exists()
    assert "2 条标记" in result.message and "待阅读 1 条" in result.message


def test_apply_rejects_without_writing_and_dry_run_writes_nothing(data_root):
    bad = apply_sync.apply('{"schema_version": 1, "device": "../x", "marks": {}}', data_root)
    assert not bad.ok and "rejected" in bad.message and not data_root.marks.exists()
    dry = apply_sync.apply(json.dumps({"schema_version": 1, "device": "dev-1a2b", "marks": {}}), data_root, dry_run=True)
    assert dry.ok and "dry run" in dry.message and not data_root.marks.exists()


# --- digest ---------------------------------------------------------------------

def test_a_paper_is_mailed_once_and_then_only_listed(data_root, tmp_path):
    _write(data_root, "dev-aaaa", **{"doi:10.1/a": _mark(title="Alpha")})
    out = tmp_path / "d"
    first = digest.run(data_root, out, site_url="https://x.github.io/r/")
    assert first.new == 1 and (out / "digest-comment.md").exists()
    assert "Alpha" in (out / "digest-comment.md").read_text(encoding="utf-8")
    assert (data_root.digest / "to-read.json").exists()
    second = digest.run(data_root, out, site_url="https://x.github.io/r/")
    assert second.new == 0 and second.pending == 1 and not (out / "digest-comment.md").exists()
    assert "Alpha" in (out / "digest-body.md").read_text(encoding="utf-8")


def test_putting_a_paper_back_reports_it_again_and_leaving_names_nothing(data_root, tmp_path):
    _write(data_root, "dev-aaaa", **{"doi:10.1/a": _mark(title="Alpha")})
    digest.run(data_root, tmp_path)
    _write(data_root, "dev-aaaa", **{"doi:10.1/a": _mark("read", at="2026-09-23T11:00:00Z", title="Alpha")})
    gone = digest.run(data_root, tmp_path)
    assert gone.new == 0 and gone.cleared == 1 and not (tmp_path / "digest-comment.md").exists()
    _write(data_root, "dev-aaaa", **{"doi:10.1/a": _mark(at="2026-09-24T11:00:00Z", title="Alpha")})
    back = digest.run(data_root, tmp_path)
    assert back.new == 1


def test_a_dry_run_does_not_consume_and_an_idle_run_leaves_the_watermark(data_root, tmp_path):
    _write(data_root, "dev-aaaa", **{"doi:10.1/a": _mark(title="Alpha")})
    digest.run(data_root, tmp_path, dry_run=True)
    assert not (data_root.digest / "to-read.json").exists()
    digest.run(data_root, tmp_path)
    stamp = (data_root.digest / "to-read.json").read_text(encoding="utf-8")
    digest.run(data_root, tmp_path)
    assert (data_root.digest / "to-read.json").read_text(encoding="utf-8") == stamp


def test_markdown_from_a_mark_cannot_break_out(data_root, tmp_path):
    _write(data_root, "dev-aaaa", **{"doi:10.1/(a)b": _mark(title="x](https://evil) [y", note="`z`\n| t |")})
    digest.run(data_root, tmp_path)
    body = (tmp_path / "digest-body.md").read_text(encoding="utf-8")
    # The brackets are escaped, so the title cannot close the link text early;
    # the only live link target on the row is the DOI resolver.
    assert "- [x\\](https://evil) \\[y](https://doi.org/10.1/%28a%29b)" in body
    assert "[x](https://evil)" not in body and "`z`" not in body and "\\| t \\|" in body


def test_every_identity_scheme_links_somewhere_sensible():
    assert digest.paper_url("doi:10.1/x") == "https://doi.org/10.1/x"
    assert digest.paper_url("arxiv:2601.1v1") == "https://arxiv.org/abs/2601.1v1"
    assert digest.paper_url("pmid:7") == "https://pubmed.ncbi.nlm.nih.gov/7/"
    assert digest.paper_url("openalex:W1") == "https://openalex.org/W1"
    assert digest.paper_url("noid:abc") == ""
    assert digest.site_url_for("Prezblublu-Sun/research-radar-v2") == "https://prezblublu-sun.github.io/research-radar-v2/"


def test_a_long_list_is_capped_with_a_pointer_to_the_site(data_root, tmp_path):
    marks = {f"doi:10.1/{n}": _mark(at=f"2026-09-{(n % 28) + 1:02d}T11:00:00Z", title=f"P{n}") for n in range(230)}
    _write(data_root, "dev-aaaa", **marks)
    result = digest.run(data_root, tmp_path, site_url="https://x.github.io/r/", github_output=tmp_path / "gh.txt")
    body = (tmp_path / "digest-body.md").read_text(encoding="utf-8")
    assert "另有 30 篇" in body and "https://x.github.io/r/reading.html" in body
    assert result.outputs == {"has_new": "true", "new_count": "230", "pending_count": "230"}
    assert "pending_count=230" in (tmp_path / "gh.txt").read_text(encoding="utf-8")
