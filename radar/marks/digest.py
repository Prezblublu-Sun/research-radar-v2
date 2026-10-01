"""The 待阅读 digest: one standing issue, body refreshed silently, a comment
only for papers that are new since the last run (the comment is the mail).

"New" is a set difference against the watermark in ``data/digest/to-read.json``,
not a timestamp: editing a note on an old mark does not resurface it, and
putting a paper back into 待阅读 after clearing it does. Every mark field is
free text that came from a browser, so everything interpolated into the
Markdown is escaped here.
"""
from __future__ import annotations

import json
import pathlib
import re
import urllib.parse
from dataclasses import dataclass

from radar.paths import DataRoot
from radar.store import marks as _marks

STATE_VERSION = 1
BODY_LIMIT = 200
COMMENT_LIMIT = 50
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def site_url_for(repo: str) -> str:
    """``owner/name`` -> the Pages URL, or "" when unknown."""
    owner, _, name = (repo or "").partition("/")
    return f"https://{owner.lower()}.github.io/{name}/" if owner and name else ""


def plain(value, limit: int = 300) -> str:
    text = " ".join(_CONTROL.sub(" ", str(value or "")).split())
    for char in ("\\", "[", "]", "`", "<", ">", "|", "*", "_"):
        text = text.replace(char, "\\" + char)
    return text[:limit]


def paper_url(identity_key: str) -> str:
    scheme, _, rest = identity_key.partition(":")
    if not rest:
        return ""
    quoted = urllib.parse.quote(rest, safe="/")
    return {"doi": f"https://doi.org/{quoted}", "arxiv": f"https://arxiv.org/abs/{quoted}",
            "pmid": f"https://pubmed.ncbi.nlm.nih.gov/{quoted}/",
            "openalex": f"https://openalex.org/{quoted}"}.get(scheme, "")


def render_row(identity_key: str, mark: dict) -> str:
    title = plain(mark.get("title")) or plain(identity_key)
    url = paper_url(identity_key)
    head = f"[{title}]({url})" if url else title
    facts = [plain(mark.get("date"), 32), plain(mark.get("direction"), 64), plain(mark.get("priority"), 32)]
    facts += ["#" + plain(tag, _marks.MAX_TAG) for tag in (mark.get("tags") or [])]
    tail = " · ".join(f for f in facts if f)
    row = f"- {head}" + (f"\n  {tail}" if tail else "")
    note = plain(mark.get("note"), 400)
    return row + (f"\n  > {note}" if note else "")


def render_list(pairs: list[tuple[str, dict]], limit: int, site_url: str) -> str:
    rows = [render_row(key, mark) for key, mark in pairs[:limit]]
    if len(pairs) > limit:
        rows.append(f"- …另有 {len(pairs) - limit} 篇，见 [阅读清单]({site_url}reading.html)" if site_url
                    else f"- …另有 {len(pairs) - limit} 篇")
    return "\n".join(rows)


def load_state(path: pathlib.Path) -> dict:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"schema_version": STATE_VERSION, "keys": []}
    if not isinstance(state, dict):
        return {"schema_version": STATE_VERSION, "keys": []}
    keys = state.get("keys")
    state["keys"] = sorted(k for k in keys if isinstance(k, str)) if isinstance(keys, list) else []
    return state


def build_digest(marks: dict[str, dict], previous: list[str]) -> dict:
    pending = _marks.by_state(marks, "to-read")
    current = {key for key, _ in pending}
    seen = set(previous)
    return {"pending": pending, "new": [(k, m) for k, m in pending if k not in seen],
            "cleared": sorted(seen - current), "keys": sorted(current),
            "counts": {s: len(_marks.by_state(marks, s)) for s in ("to-read", "read", "ignore")},
            "tags": _marks.tag_counts(marks)}


def render_body(digest: dict, site_url: str) -> str:
    counts = digest["counts"]
    lines = ["## 待阅读", "",
             f"共 **{len(digest['pending'])}** 篇。这条正文每次运行都会整体刷新；新增的论文会单独回复一条评论（那条才会发邮件）。", ""]
    lines.append(render_list(digest["pending"], BODY_LIMIT, site_url) if digest["pending"] else "_目前没有待阅读的论文。_")
    lines += ["", "---", "", f"全部标记：待阅读 {counts['to-read']} · 已阅读 {counts['read']} · 忽略 {counts['ignore']}"]
    if digest["tags"]:
        lines.append("标签：" + " · ".join(f"{plain(t, _marks.MAX_TAG)} {n}" for t, n in list(digest["tags"].items())[:12]))
    lines += ["", "由 `.github/workflows/marks-digest.yml` 自动维护。标记来自 `data/marks/`"
              + (f"，在 [网站]({site_url}) 上标注即可。" if site_url else "。")]
    return "\n".join(lines)


def render_comment(digest: dict, site_url: str) -> str:
    # Departures are deliberately not named: a silent run still advances the
    # watermark, so by the time a comment happens they are an arbitrary slice.
    return "\n".join([f"### 新增 {len(digest['new'])} 篇待阅读", "", render_list(digest["new"], COMMENT_LIMIT, site_url),
                      "", f"当前待阅读共 {len(digest['pending'])} 篇，完整清单见上方正文。"])


@dataclass
class DigestResult:
    pending: int
    new: int
    cleared: int
    changed: bool
    outputs: dict[str, str]


def run(root: DataRoot, out_dir: pathlib.Path, *, site_url: str = "", dry_run: bool = False,
        github_output: pathlib.Path | None = None) -> DigestResult:
    state_path = root.digest / "to-read.json"
    state = load_state(state_path)
    digest = build_digest(_marks.load_all(root), state["keys"])
    out_dir = pathlib.Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "digest-body.md").write_text(render_body(digest, site_url), encoding="utf-8")
    comment_path = out_dir / "digest-comment.md"
    if digest["new"]:
        comment_path.write_text(render_comment(digest, site_url), encoding="utf-8")
    else:
        comment_path.unlink(missing_ok=True)   # no new papers = no file = no mail
    changed = digest["keys"] != state["keys"]
    if not dry_run and changed:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({"schema_version": STATE_VERSION, "updated_at": _marks.utc_now(),
                                          "keys": digest["keys"]}, ensure_ascii=False, indent=1), encoding="utf-8")
    outputs = {"has_new": "true" if digest["new"] else "false", "new_count": str(len(digest["new"])),
               "pending_count": str(len(digest["pending"]))}
    if github_output:
        with open(github_output, "a", encoding="utf-8") as handle:
            for key, value in outputs.items():
                handle.write(f"{key}={value}\n")
    return DigestResult(pending=len(digest["pending"]), new=len(digest["new"]), cleared=len(digest["cleared"]),
                        changed=changed, outputs=outputs)
