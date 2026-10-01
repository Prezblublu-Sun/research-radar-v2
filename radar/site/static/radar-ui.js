/* radar-ui.js — ADR-0016 client-side curation affordances (D3, D4, D5).
 *
 * Zero dependencies, vanilla DOM. Loaded with `defer` so the DOM is parsed
 * before this IIFE runs. One bundle serves every page type; each block
 * feature-detects its anchor elements and no-ops when they are absent:
 *
 *   - daily pages:      direction tabs + priority filter (D3) + per-card
 *                       reading marks/notes (D4) + mail button (D5)
 *   - high/medium pages: same per-card controls (cards reused verbatim)
 *   - my-marks.html:    export-all-marks + listing
 *
 * localStorage keys (ADR-0016 §3, ADR-0032, ADR-0034):
 *   radar:filter:priority   -> ["High","Medium",...]            (D3)
 *   radar:filter:marks      -> ["to-read","read","ignore","none"]
 *   radar:filter:tags       -> ["有启发", ...]                   (ADR-0034)
 *   radar:mark:<idkey>      -> { state, tags, at, note, ...meta }
 *   radar:marks-schema      -> the shape version this browser migrated to
 *   radar:device            -> this browser's name in data/marks/
 *   radar:gh-token          -> optional fine-grained token (ADR-0033)
 *
 * A mark is one `state` (待读 / 已读 / 忽略, mutually exclusive) plus any
 * number of free-text `tags` — ADR-0034; 有启发 used to be a state. One
 * rule, markFilterFn(), decides visibility for the daily pages, the queue
 * and the reading list, so the three surfaces cannot drift apart.
 *
 * Marks are no longer ephemeral: ADR-0032 hands them to `data/marks/` and
 * ADR-0033 does it automatically when a token is stored. `at` therefore
 * decides merges across devices, which is why every write stamps it and the
 * one-time migration does not.
 */
(function () {
  "use strict";

  function lsGet(key, fallback) {
    try {
      var raw = localStorage.getItem(key);
      return raw == null ? fallback : JSON.parse(raw);
    } catch (e) {
      return fallback;
    }
  }
  function lsSet(key, value) {
    try {
      localStorage.setItem(key, JSON.stringify(value));
    } catch (e) {
      /* private mode / quota — degrade silently */
    }
  }
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  var PRIORITY_DEFAULT = ["High", "Medium", "Unscored"];

  // ADR-0034. A paper's *state* says where it sits in the triage flow and
  // only one can hold at a time; its *tags* are judgements about it and any
  // number can hold at once. "有启发" used to be a state, which made "read,
  // and worth something" impossible to say and left 待读 unfilterable
  // outside the daily pages.
  var MARK_STATES = ["to-read", "read", "ignore"];
  var STATE_LABELS = {
    "to-read": "待阅读", "read": "已阅读", "ignore": "忽略", "none": "未标记"
  };
  var MARKS_DEFAULT = MARK_STATES.concat(["none"]);  // "none" = 未标记
  var INSPIRING_TAG = "有启发";
  var TAGS_KEY = "radar:filter:tags";
  // Mirrors the caps in pipeline/marks_store.py, which rejects a payload
  // that breaks them — better to clamp here than to have the sync bounce.
  var MAX_TAGS = 24;
  var MAX_TAG = 40;

  function normTags(value) {
    if (!Array.isArray(value)) return [];
    var out = [];
    for (var index = 0; index < value.length && out.length < MAX_TAGS; index += 1) {
      if (typeof value[index] !== "string") continue;
      var tag = value[index].replace(/\s+/g, " ").trim().slice(0, MAX_TAG);
      if (tag && out.indexOf(tag) < 0) out.push(tag);
    }
    // Sorted so two browsers that added the same tags in a different order
    // produce the same file and stop churning the sync commits.
    return out.sort();
  }

  function tagsOf(record) {
    return record ? normTags(record.tags) : [];
  }

  // ADR-0034 migration. `at` is deliberately left alone: bumping it would
  // make this browser win every merge against a device holding newer marks,
  // silently reverting work done elsewhere. The server-side validator maps
  // the retired state too, so a tab that never reloads still syncs correctly.
  var SCHEMA_KEY = "radar:marks-schema";
  var SCHEMA_NOW = 3;

  function migrateMarks() {
    var done = 0;
    try {
      done = parseInt(localStorage.getItem(SCHEMA_KEY) || "0", 10) || 0;
    } catch (error) {
      return;  // no storage at all: nothing to migrate
    }
    if (done >= SCHEMA_NOW) return;

    var keys = [];
    try {
      for (var index = 0; index < localStorage.length; index += 1) {
        var key = localStorage.key(index);
        if (key && key.indexOf("radar:mark:") === 0) keys.push(key);
      }
    } catch (error) {
      return;
    }
    keys.forEach(function (key) {
      var record = lsGet(key, null);
      if (!record || typeof record !== "object") return;
      var tags = tagsOf(record);
      if (record.state === "interesting") {
        record.state = "read";
        if (tags.indexOf(INSPIRING_TAG) < 0) tags.push(INSPIRING_TAG);
      }
      record.tags = normTags(tags);
      lsSet(key, record);
    });

    // The saved filter names states, so it moves with them.
    var marks = lsGet("radar:filter:marks", null);
    if (Array.isArray(marks) && marks.indexOf("interesting") >= 0) {
      marks = marks.filter(function (state) { return state !== "interesting"; });
      if (marks.indexOf("read") < 0) marks.push("read");
      lsSet("radar:filter:marks", marks);
    }
    // The queue's retired three-way 忽略 select is dropped, not carried
    // over. It was a queue-scoped preference and the first cut of this
    // migration promoted it to the *global* mark filter — so a reader who
    // had once picked "只看已忽略" got a site that showed nothing anywhere.
    // Losing one page's preference is the cheaper mistake.
    try {
      localStorage.removeItem("radar:filter:queue-ignored");
      localStorage.removeItem("radar:filter:queue-hide-ignored");
    } catch (error) {
      /* private mode — nothing to clean up */
    }

    // Repair what schema 2 did to browsers that already ran it. Exactly
    // ["ignore"] as a *global* filter is not something a reader chooses —
    // it would mean unticking 待阅读, 已阅读 and 未标记 while keeping 忽略 —
    // it is what the queue-preference promotion above used to write. The
    // cost of being wrong is one re-tick; the cost of leaving it is a site
    // that shows nothing.
    if (done === 2) {
      var stored = lsGet("radar:filter:marks", null);
      if (Array.isArray(stored) && stored.length === 1 && stored[0] === "ignore") {
        lsSet("radar:filter:marks", MARKS_DEFAULT.slice());
      }
    }

    try {
      localStorage.setItem(SCHEMA_KEY, String(SCHEMA_NOW));
    } catch (error) {
      /* quota — the migration is idempotent and will simply run again */
    }
  }
  migrateMarks();

  function priorityFilter() {
    var v = lsGet("radar:filter:priority", PRIORITY_DEFAULT);
    return Array.isArray(v) ? v : PRIORITY_DEFAULT.slice();
  }
  function marksFilter() {
    var v = lsGet("radar:filter:marks", null);
    if (!Array.isArray(v)) return MARKS_DEFAULT.slice();
    var known = v.filter(function (state) {
      return MARKS_DEFAULT.indexOf(state) >= 0;
    });
    // A tab that predates ADR-0034 can still write the retired state; it
    // must not end up hiding every 已读 paper.
    if (v.indexOf("interesting") >= 0 && known.indexOf("read") < 0) {
      known.push("read");
    }
    return known;
  }
  function tagFilter() {
    return normTags(lsGet(TAGS_KEY, []));
  }
  function setTagFilter(tags) {
    lsSet(TAGS_KEY, normTags(tags));
  }
  // True when the filter excludes unmarked papers, so only marked ones can
  // appear. The queue uses it to load a handful of year shards instead of a
  // whole priority.
  function marksOnly() {
    return marksFilter().indexOf("none") < 0;
  }

  // The one rule for "does the mark filter show this paper". Built once per
  // pass so a 150-card queue does not re-read localStorage 300 times, and
  // shared with the queue so the two surfaces cannot drift apart. States are
  // OR'd; a selected tag narrows further (a paper needs any one of them).
  function markFilterFn() {
    var states = marksFilter();
    var wanted = tagFilter();
    return function (record) {
      var state = record && record.state ? record.state : "none";
      if (states.indexOf(state) < 0) return false;
      if (!wanted.length) return true;
      var have = tagsOf(record);
      for (var index = 0; index < wanted.length; index += 1) {
        if (have.indexOf(wanted[index]) >= 0) return true;
      }
      return false;
    };
  }

  function markRecord(idkey) {
    return lsGet("radar:mark:" + idkey, null);
  }

  function markState(idkey) {
    var record = markRecord(idkey);
    return record && typeof record.state === "string" ? record.state : "";
  }

  function markTags(idkey) {
    return tagsOf(markRecord(idkey));
  }

  function eachMarkRecord(visit) {
    var total = 0;
    try { total = localStorage.length; } catch (error) { total = 0; }
    for (var index = 0; index < total; index += 1) {
      var key = localStorage.key(index);
      if (!key || key.indexOf("radar:mark:") !== 0) continue;
      var record = lsGet(key, null);
      if (record && typeof record === "object") {
        visit(record, key.slice("radar:mark:".length));
      }
    }
  }

  // Every tag in use with how often, newest vocabulary first. Feeds both the
  // filter chips and the per-card autocomplete: tags are free text, so the
  // only defence against "有启发" and "有启发 " becoming two tags is showing
  // the reader what already exists.
  function allTags() {
    var counts = {};
    eachMarkRecord(function (record) {
      tagsOf(record).forEach(function (tag) {
        counts[tag] = (counts[tag] || 0) + 1;
      });
    });
    return Object.keys(counts).sort(function (a, b) {
      if (counts[b] !== counts[a]) return counts[b] - counts[a];
      return a < b ? -1 : (a > b ? 1 : 0);
    }).map(function (tag) {
      return { tag: tag, count: counts[tag] };
    });
  }

  // Every mark write goes through here so listeners (the queue's "hide
  // ignored" filter, and anything added later) never have to re-derive the
  // storage rules or poll localStorage.
  function announceMarkChange(idkey) {
    document.dispatchEvent(new CustomEvent("radar:mark-changed", {
      detail: { identity_key: idkey, state: markState(idkey) }
    }));
  }

  // ---- combined visibility: direction AND priority AND mark ----
  var dirFilter = "all";

  function applyFilters() {
    var prios = priorityFilter();
    var visible = markFilterFn();
    // A filter is only applied on a page that shows its control. The
    // workbench has neither bar: a stored "only 忽略" hid all 777 of its
    // cards, headings and counts still claiming they were there, and no
    // checkbox anywhere on the page to undo it (reported 2026-09-26).
    // Gating on the control makes that impossible to reintroduce.
    var gradeBar = document.getElementById("rui-priority-filter");
    var markBar = document.getElementById("rui-marks-filter");
    // The reading list shows exactly what the user marked; the daily-page
    // priority / mark filters must not hide anything there.
    var cards = document.querySelectorAll(".paper");
    if (document.querySelector("main[data-rui-no-filter]")) {
      cards.forEach(function (card) {
        card.dataset.hidden = "0";
      });
      announceFiltersApplied(cards.length, 0);
      return;
    }
    var hidden = 0;
    cards.forEach(function (card) {
      var d = card.dataset.direction || "";
      var pr = card.dataset.priority || "Low";
      var idk = card.dataset.identityKey || "";
      var dirOk = dirFilter === "all" || dirFilter === d;
      var prOk = !gradeBar || prios.indexOf(pr) >= 0;
      var mkOk = !markBar || visible(idk ? markRecord(idk) : null);
      var show = dirOk && prOk && mkOk;
      card.dataset.hidden = show ? "0" : "1";
      if (!show) hidden += 1;
    });
    announceFiltersApplied(cards.length, hidden);
    renderBlockedNotice(cards.length, hidden);
  }

  // When the filters hide every card, say which filters and offer the reset,
  // right under the bar that did it. Lives here rather than in each page's
  // script so that any page carrying a filter bar gets it — it was day-page
  // code at first, and the random-reading page, with the same bar, sat empty
  // without a word.
  var blockedNotice = null;
  function renderBlockedNotice(total, hidden) {
    var bars = document.querySelectorAll("#rui-priority-filter, #rui-marks-filter");
    if (!bars.length) return;
    var blocked = total > 0 && hidden >= total;
    if (!blockedNotice) {
      if (!blocked) return;
      blockedNotice = node("div", "empty day-blocked");
      blockedNotice.appendChild(node("p", "", ""));
      var reset = node("button", "queue-page-button", "显示全部");
      reset.type = "button";
      reset.addEventListener("click", clearFilters);
      blockedNotice.appendChild(reset);
      var anchor = bars[bars.length - 1];
      anchor.parentNode.insertBefore(blockedNotice, anchor.nextSibling);
    }
    blockedNotice.hidden = !blocked;
    if (!blocked) return;
    var active = activeFilters();
    blockedNotice.firstChild.textContent = "本页 " + hidden +
      " 篇论文都被上方的筛选隐藏了" +
      (active.length ? "：" + active.join("；") : "") + "。";
  }

  // A page that renders its own count has to be told what the filters then
  // did to it, or it goes on claiming "1 paper on this page" over an empty
  // grid — which is what a day holding a single Low paper looked like.
  // Emitted from here rather than from each control, so the direction tabs,
  // the priority bar, the mark bar, the tag chips and every mark write all
  // report through one path.
  function announceFiltersApplied(total, hidden) {
    document.dispatchEvent(new CustomEvent("radar:filters-applied", {
      detail: { total: total, hidden: hidden, shown: total - hidden }
    }));
  }

  // ---- direction tabs (ported from the old inline build_pages JS) ----
  var tabs = document.querySelectorAll(".tab");
  tabs.forEach(function (t) {
    t.addEventListener("click", function () {
      tabs.forEach(function (x) {
        x.classList.remove("active");
      });
      t.classList.add("active");
      dirFilter = t.dataset.filter;
      applyFilters();
    });
  });

  // ---- D3: priority checkbox bar ----
  var prioCbs = document.querySelectorAll(".rui-pf-cb");
  function collect(cbs) {
    var sel = [];
    cbs.forEach(function (c) {
      if (c.checked) sel.push(c.value);
    });
    return sel;
  }
  (function syncPrio() {
    var prios = priorityFilter();
    prioCbs.forEach(function (cb) {
      cb.checked = prios.indexOf(cb.value) >= 0;
    });
  })();
  prioCbs.forEach(function (cb) {
    cb.addEventListener("change", function () {
      lsSet("radar:filter:priority", collect(prioCbs));
      applyFilters();
    });
  });

  // ---- D4: marks filter checkbox bar ----
  var markCbs = document.querySelectorAll(".rui-mf-cb");
  (function syncMarks() {
    var marks = marksFilter();
    markCbs.forEach(function (cb) {
      cb.checked = marks.indexOf(cb.value) >= 0;
    });
  })();
  markCbs.forEach(function (cb) {
    cb.addEventListener("change", function () {
      lsSet("radar:filter:marks", collect(markCbs));
      applyFilters();
      announceFilterChange();
    });
  });

  // Tags only exist in this browser, so the tag half of the filter bar has
  // to be built here rather than rendered into the page by build_pages.
  function renderTagFilter() {
    var bar = document.getElementById("rui-marks-filter");
    if (!bar) return;
    var host = bar.querySelector(".rui-tagf");
    if (!host) {
      host = node("span", "rui-tagf");
      bar.appendChild(host);
    }
    host.textContent = "";
    var known = allTags();
    var active = tagFilter();
    // A selected tag that no paper carries any more still filters. Chips were
    // built from the tags in use only, so it vanished from the bar while
    // still hiding every card — a filter nobody could see or untick. Keep it
    // on screen, with its real count of zero, until it is clicked off.
    active.forEach(function (tag) {
      var present = known.some(function (entry) { return entry.tag === tag; });
      if (!present) known.push({ tag: tag, count: 0 });
    });
    if (!known.length) return;  // nothing tagged and nothing selected
    host.appendChild(node("b", "", "标签："));
    known.forEach(function (entry) {
      var chip = node("button", "rui-tagf-chip", entry.tag + " " + entry.count);
      chip.type = "button";
      chip.setAttribute("aria-pressed", active.indexOf(entry.tag) >= 0 ? "true" : "false");
      if (active.indexOf(entry.tag) >= 0) chip.dataset.on = "1";
      chip.addEventListener("click", function () {
        var next = tagFilter();
        var at = next.indexOf(entry.tag);
        if (at >= 0) next.splice(at, 1);
        else next.push(entry.tag);
        setTagFilter(next);
        renderTagFilter();
        applyFilters();
        announceFilterChange();
      });
      host.appendChild(chip);
    });
  }

  // The queue paginates server-side, so it cannot just re-run applyFilters:
  // it has to reload the view. One event, so every surface stays in step.
  function announceFilterChange() {
    document.dispatchEvent(new CustomEvent("radar:filter-changed"));
  }

  // Plain-language list of the filters currently narrowing this page, under
  // the same gating applyFilters uses — so it never names a filter the page
  // is not applying. The empty-page notes print this instead of guessing:
  // the first version listed the hidden cards' priority levels, which blamed
  // "等级：Low" when the mark filter was what had emptied the page.
  function activeFilters() {
    var out = [];
    if (dirFilter !== "all") {
      var tab = document.querySelector(".tab.active");
      out.push("方向 " + (tab ? tab.textContent.trim() : dirFilter));
    }
    if (document.getElementById("rui-priority-filter")) {
      var offPriority = [];
      prioCbs.forEach(function (cb) { if (!cb.checked) offPriority.push(cb.value); });
      if (offPriority.length) out.push("等级未勾选 " + offPriority.join("、"));
    }
    if (document.getElementById("rui-marks-filter")) {
      var offMarks = [];
      markCbs.forEach(function (cb) {
        if (!cb.checked) offMarks.push(STATE_LABELS[cb.value] || cb.value);
      });
      if (offMarks.length) out.push("标记未勾选 " + offMarks.join("、"));
      var tags = tagFilter();
      if (tags.length) out.push("只看标签 " + tags.join("、"));
    }
    return out;
  }

  // Turn every filter off and show whatever the page is holding.
  //
  // A day whose papers are all Low renders an empty grid under the default
  // filter (High/Medium/Unscored). Saying so is not enough — the reader came
  // to see the papers — so this is what the page's "显示全部" offers. It
  // resets rather than special-cases: silently overriding a filter the
  // reader chose would be worse than an empty page.
  function clearFilters() {
    var allTab = document.querySelector('.tab[data-filter="all"]');
    if (allTab) {
      tabs.forEach(function (tab) { tab.classList.remove("active"); });
      allTab.classList.add("active");
    }
    dirFilter = "all";
    // "Unscored" has no checkbox but cards can carry it.
    var prios = ["Unscored"];
    prioCbs.forEach(function (cb) {
      cb.checked = true;
      if (prios.indexOf(cb.value) < 0) prios.push(cb.value);
    });
    if (prioCbs.length) lsSet("radar:filter:priority", prios);
    markCbs.forEach(function (cb) { cb.checked = true; });
    if (markCbs.length) lsSet("radar:filter:marks", MARKS_DEFAULT.slice());
    setTagFilter([]);
    renderTagFilter();
    applyFilters();
    announceFilterChange();
  }

  function cardMeta(card) {
    return {
      title: card.dataset.title || "",
      date: card.dataset.date || "",
      direction: card.dataset.direction || "",
      priority: card.dataset.priority || ""
    };
  }

  // Every mark write goes through here: it merges the card's metadata, keeps
  // the record's shape honest, stamps the time (last write wins at merge, so
  // *every* change has to move `at`), and tells the page. Four callers —
  // the radios, the clear, the note and the tags — used to each do their own
  // slightly different version of this.
  function updateMark(idkey, card, change) {
    var record = mergeMeta(markRecord(idkey) ||
      { state: "", at: "", note: "", tags: [] }, card);
    if (typeof record.note !== "string") record.note = "";
    record.tags = normTags(record.tags);
    change(record);
    record.tags = normTags(record.tags);
    record.at = new Date().toISOString();
    lsSet("radar:mark:" + idkey, record);
    applyFilters();
    announceMarkChange(idkey);
    return record;
  }

  function mergeMeta(record, card) {
    var meta = cardMeta(card);
    Object.keys(meta).forEach(function (key) {
      if (meta[key]) record[key] = meta[key];
    });
    return record;
  }

  // ---- D5: mail one card to the owner (ADR-0016 addendum 2026-09-22) ----
  //
  // The site is a static GitHub Pages artifact with no backend, so nothing
  // here can actually send mail: the button composes the message and hands
  // it to the browser's registered mail client via a `mailto:` URL. The
  // address is assembled at run time rather than written out as a literal,
  // which keeps it out of the page source for naive address scrapers (the
  // rendered page is public either way).
  var MAIL_LOCAL = "sun1139156053";
  var MAIL_DOMAIN = "163.com";
  // Conservative ceiling for the whole mailto: URL. Windows' shell handler
  // historically truncates above ~2000 characters, and one CJK character
  // costs nine after percent-encoding, so the body is filled section by
  // section until the budget runs out. The untruncated text always goes to
  // the clipboard as well, so nothing is silently lost.
  var MAIL_URL_BUDGET = 1800;

  function node(tag, className, value) {
    var el = document.createElement(tag);
    if (className) el.className = className;
    if (value != null) el.textContent = String(value);
    return el;
  }

  function mailAddress() {
    return MAIL_LOCAL + "@" + MAIL_DOMAIN;
  }

  function cardText(card, selector) {
    var node = card.querySelector(selector);
    return node ? node.textContent.replace(/\s+/g, " ").trim() : "";
  }

  function cardLink(card) {
    var link = card.querySelector(".doi a");
    return link && link.href ? link.href : "";
  }

  function dayPageUrl(card) {
    var date = card.dataset.date || "";
    if (!/^\d{4}-\d{2}-\d{2}$/.test(date) || !card.id) return "";
    try {
      return new URL(date + ".html#" + encodeURIComponent(card.id),
        window.location.href).href;
    } catch (error) {
      return "";
    }
  }

  function mailSections(card, idkey) {
    var head = [];
    var title = card.dataset.title || cardText(card, ".paper-title");
    head.push("标题：" + title);
    var meta = [
      cardText(card, ".priority"), cardText(card, ".direction-pill"),
      card.dataset.date || ""
    ].filter(Boolean).join(" · ");
    if (meta) head.push("等级/方向/日期：" + meta);
    var authors = cardText(card, ".authors");
    if (authors) head.push("作者：" + authors);
    var venue = cardText(card, ".venue");
    if (venue) head.push("来源：" + venue);
    var link = cardLink(card);
    if (link) head.push("原文：" + link);
    var page = dayPageUrl(card);
    if (page) head.push("雷达卡片：" + page);
    if (idkey) head.push("身份键：" + idkey);

    var sections = [];
    var record = idkey ? markRecord(idkey) : null;
    if (record && record.note) sections.push("我的笔记：" + record.note);
    var relevance = cardText(card, ".relevance");
    if (relevance) sections.push(relevance);
    var summary = cardText(card, ".summary");
    if (summary) sections.push("中文摘要：" + summary);
    var boundary = cardText(card, ".why-not-core");
    if (boundary) sections.push(boundary);
    return { subject: "[Radar] " + title, head: head.join("\n"), sections: sections };
  }

  function mailtoUrl(subject, body) {
    return "mailto:" + mailAddress() + "?subject=" +
      encodeURIComponent(subject) + "&body=" + encodeURIComponent(body);
  }

  var MAIL_TRUNCATED_NOTE = "\n\n（完整内容见本页文本框，可直接复制粘贴。）";

  function mailtoBody(parts) {

    // Fill the mailto body up to the budget, head first. The truncation note
    // is appended after the loop, so reserve its encoded length up front or
    // the finished URL overshoots the budget by ~180 characters.
    //
    // A section that does not fit whole is cut down rather than dropped: one
    // Chinese paragraph costs ~1,000 characters once percent-encoded, so
    // dropping it would leave the mail with nothing but the title and links.
    // The reader gets the opening of the relevance note in the mail and the
    // untruncated text from the clipboard.
    var reserve = encodeURIComponent(MAIL_TRUNCATED_NOTE).length;

    function fits(candidate) {
      return mailtoUrl(parts.subject, candidate).length + reserve <= MAIL_URL_BUDGET;
    }

    var full = [parts.head].concat(parts.sections).join("\n\n");
    var body = parts.head;
    for (var index = 0; index < parts.sections.length; index += 1) {
      var section = parts.sections[index];
      if (fits(body + "\n\n" + section)) {
        body = body + "\n\n" + section;
        continue;
      }
      var shortened = section;
      while (shortened.length > 24) {
        shortened = shortened.slice(0, Math.floor(shortened.length * 0.8));
        if (fits(body + "\n\n" + shortened + "…")) {
          body = body + "\n\n" + shortened + "…";
          break;
        }
      }
      break;
    }
    return body.length < full.length ? body + MAIL_TRUNCATED_NOTE : body;
  }

  // A static page cannot send mail. `mailto:` only works when the machine has
  // a mail client registered for the scheme, which a webmail-only setup does
  // not — and the page has no way to detect that, so claiming "mail opened"
  // was a lie (reported 2026-09-22: the button appeared to only copy). The
  // button now opens a panel that always works: the full text sits in a
  // selected textarea, a copy is attempted, and the `mailto:` link is offered
  // as a real link the reader can choose to click.
  function buildMailPanel(card, idkey) {
    var parts = mailSections(card, idkey);
    var full = [parts.head].concat(parts.sections).join("\n\n");

    var panel = node("div", "rui-mail-wrap");
    var status = node("div", "rui-mail-status",
      "收件人 " + mailAddress() + " · 正在复制正文…");
    panel.appendChild(status);

    var area = document.createElement("textarea");
    area.className = "rui-mail-ta";
    area.readOnly = true;
    area.value = "收件人：" + mailAddress() + "\n主题：" + parts.subject +
      "\n\n" + full;
    panel.appendChild(area);

    var actions = node("div", "rui-mail-actions");
    var copy = node("button", "rui-btn rui-mail-copy", "复制全文");
    copy.type = "button";
    actions.appendChild(copy);
    var open = node("a", "rui-btn rui-secondary rui-mail-open", "用邮件客户端打开");
    open.href = mailtoUrl(parts.subject, mailtoBody(parts));
    actions.appendChild(open);
    var close = node("button", "rui-btn rui-secondary rui-mail-close", "收起");
    close.type = "button";
    actions.appendChild(close);
    panel.appendChild(actions);

    panel.appendChild(node("div", "rui-mail-note",
      "点“用邮件客户端打开”没有反应，说明本机没有注册默认邮件客户端；" +
      "直接复制上面的正文，粘贴到网页版邮箱即可。"));

    function copyAll() {
      if (!navigator.clipboard || !navigator.clipboard.writeText) {
        status.textContent = "收件人 " + mailAddress() +
          " · 浏览器不允许自动复制，请在文本框里全选复制。";
        return;
      }
      navigator.clipboard.writeText(area.value).then(function () {
        status.textContent = "收件人 " + mailAddress() + " · ✓ 正文已复制，可直接粘贴";
      }, function () {
        status.textContent = "收件人 " + mailAddress() +
          " · 自动复制被拒绝，请在文本框里全选复制。";
      });
    }
    copy.addEventListener("click", copyAll);
    close.addEventListener("click", function () {
      panel.remove();
    });
    copyAll();
    return panel;
  }

  function offerCardByMail(card, idkey) {
    var existing = card.querySelector(".rui-mail-wrap");
    if (existing) {
      existing.remove();
      return;
    }
    var panel = buildMailPanel(card, idkey);
    var tools = card.querySelector(".rui-card-tools");
    (tools || card).appendChild(panel);
    var area = panel.querySelector(".rui-mail-ta");
    if (area) area.select();
  }

  // One datalist for the whole page: free-text tags only stay a vocabulary
  // if the reader can see what they already used.
  function refreshTagDatalist() {
    var list = document.getElementById("rui-tags-known");
    if (!list) {
      list = document.createElement("datalist");
      list.id = "rui-tags-known";
      document.body.appendChild(list);
    }
    list.textContent = "";
    allTags().forEach(function (entry) {
      var option = document.createElement("option");
      option.value = entry.tag;
      list.appendChild(option);
    });
  }

  // The chip row is always rendered; CSS shows the remove buttons and the
  // add form only while the card's 标签 panel is open, so a closed card just
  // shows what it is tagged with.
  function renderTagEditor(card, idkey) {
    var wrap = card.querySelector(".rui-tag-wrap");
    if (!wrap) return;
    var current = markTags(idkey);
    wrap.textContent = "";

    function rerender(focusInput) {
      renderTagEditor(card, idkey);
      renderTagFilter();
      refreshTagDatalist();
      if (!focusInput) return;
      var next = card.querySelector(".rui-tag-input");
      if (next) next.focus();
    }

    function write(change) {
      updateMark(idkey, card, change);
      rerender(true);
    }

    var row = node("div", "rui-tag-row");
    current.forEach(function (tag) {
      var chip = node("span", "rui-tag", tag);
      var drop = node("button", "rui-tag-x", "×");
      drop.type = "button";
      drop.title = "移除标签 " + tag;
      drop.setAttribute("aria-label", "移除标签 " + tag);
      drop.addEventListener("click", function () {
        write(function (record) {
          record.tags = record.tags.filter(function (t) { return t !== tag; });
        });
      });
      chip.appendChild(drop);
      row.appendChild(chip);
    });
    if (!current.length) row.appendChild(node("span", "rui-tag-empty", "还没有标签"));
    wrap.appendChild(row);

    var form = node("div", "rui-tag-add");
    if (current.indexOf(INSPIRING_TAG) < 0) {
      // The one tag that used to be a state keeps a one-click path.
      var quick = node("button", "rui-tag-quick", "+ " + INSPIRING_TAG);
      quick.type = "button";
      quick.addEventListener("click", function () {
        write(function (record) { record.tags = record.tags.concat([INSPIRING_TAG]); });
      });
      form.appendChild(quick);
    }
    var input = document.createElement("input");
    input.className = "rui-tag-input";
    input.type = "text";
    input.maxLength = MAX_TAG;
    input.placeholder = "新标签，回车添加";
    input.setAttribute("list", "rui-tags-known");
    form.appendChild(input);
    var go = node("button", "rui-tag-go", "添加");
    go.type = "button";
    form.appendChild(go);

    function submit() {
      var clean = normTags([input.value]);
      if (!clean.length) return;
      if (current.indexOf(clean[0]) >= 0) {  // already there: just clear
        input.value = "";
        return;
      }
      write(function (record) { record.tags = record.tags.concat(clean); });
    }
    go.addEventListener("click", submit);
    input.addEventListener("keydown", function (event) {
      if (event.key !== "Enter") return;
      event.preventDefault();
      submit();
    });
    wrap.appendChild(form);
  }

  // ---- D4 + D5: per-card controls (also hydrates lazy queue cards) ----
  function hydrateCard(card) {
    if (card.dataset.ruiReady === "1") return;
    var idk = card.dataset.identityKey;
    if (!idk) return;
    card.dataset.ruiReady = "1";
    var rec = markRecord(idk);
    if (rec && rec.state) card.dataset.mark = rec.state;

    // D4 mark radios — 4-state to-read / read / interesting / ignore.
    // CHANGE B1: re-clicking the already-selected radio clears the mark
    // entirely. The click handler runs before `change` and reads the prior
    // state from localStorage (the source of truth, not the DOM): if the
    // clicked value already is the stored state, we deselect instead of
    // re-affirming — uncheck the radio, drop the mark, refresh the stripe.
    card.querySelectorAll(".rui-mark-radio").forEach(function (r) {
      if (rec && rec.state === r.value) r.checked = true;
      r.addEventListener("click", function (ev) {
        var prior = markRecord(idk);
        var priorState = prior && prior.state ? prior.state : "none";
        if (priorState !== r.value) return; // not a re-click: let `change` save
        // Re-click on the current mark: clear it. Standard radios don't
        // toggle off, so undo the default and unset explicitly.
        ev.preventDefault();
        r.checked = false;
        // Keep a record with an empty state rather than deleting the key: it
        // is the tombstone that lets the clear beat a stale mark held by
        // another device at merge time. A note and the tags survive.
        updateMark(idk, card, function (record) { record.state = ""; });
        delete card.dataset.mark; // back to the neutral .paper stripe
      });
      r.addEventListener("change", function () {
        updateMark(idk, card, function (record) { record.state = r.value; });
        card.dataset.mark = r.value;
      });
    });

    // D4 note — toggle textarea, prefilled, auto-save on blur (merge)
    var noteBtn = card.querySelector(".rui-note-btn");
    var noteWrap = card.querySelector(".rui-note-wrap");
    var ta = card.querySelector(".rui-note-ta");
    if (ta) ta.value = rec && rec.note ? rec.note : "";
    if (noteBtn && noteWrap) {
      noteBtn.addEventListener("click", function () {
        noteWrap.style.display =
          noteWrap.style.display === "block" ? "none" : "block";
      });
    }
    if (ta) {
      ta.addEventListener("blur", function () {
        if (ta.value === (markRecord(idk) || {}).note) return;  // untouched
        updateMark(idk, card, function (record) { record.note = ta.value; });
      });
    }

    // D4 tags — free-form judgements, any number per paper (ADR-0034).
    renderTagEditor(card, idk);
    var tagBtn = card.querySelector(".rui-tag-btn");
    var tagWrap = card.querySelector(".rui-tag-wrap");
    if (tagBtn && tagWrap) {
      tagBtn.addEventListener("click", function () {
        var open = tagWrap.dataset.open === "1";
        tagWrap.dataset.open = open ? "0" : "1";
        if (open) return;
        var input = tagWrap.querySelector(".rui-tag-input");
        if (input) input.focus();
      });
    }

    // D5 mail — hand this card to the owner's mail client (ADR-0016
    // addendum 2026-09-22, replaces the lit-system promote queue).
    var mBtn = card.querySelector(".rui-mail-btn");
    if (mBtn) {
      mBtn.addEventListener("click", function () {
        offerCardByMail(card, idk);
      });
    }
  }

  function hydrateCards(root) {
    var scope = root && root.querySelectorAll ? root : document;
    scope.querySelectorAll(".paper").forEach(hydrateCard);
    applyFilters();
  }

  document.addEventListener("radar:content-ready", function (event) {
    hydrateCards(event.detail && event.detail.root);
  });
  // Years holding a mark the current filter would show. A mark records the
  // paper's publication date, which is what names its year shard, so when
  // the filter excludes unmarked papers the queue can load those few shards
  // instead of the whole priority.
  function markedYears() {
    var visible = markFilterFn();
    var years = [];
    eachMarkRecord(function (record) {
      if (!visible(record)) return;
      var year = typeof record.date === "string" ? record.date.slice(0, 4) : "";
      if (!/^\d{4}$/.test(year)) year = "";
      if (years.indexOf(year) === -1) years.push(year);
    });
    return years;
  }

  renderTagFilter();
  refreshTagDatalist();

  window.RadarUI = {
    hydrate: hydrateCards,
    clearFilters: clearFilters,
    activeFilters: activeFilters,
    markRecord: markRecord,
    markState: markState,
    markTags: markTags,
    allTags: allTags,
    markFilter: markFilterFn,
    marksOnly: marksOnly,
    markedYears: markedYears,
    stateLabel: function (state) { return STATE_LABELS[state] || state; }
  };

  hydrateCards(document);

  // ---- library.html: hand the marks over to the repository (ADR-0032) ----
  //
  // The site is a static artifact and holds no credential, so the browser
  // cannot write to the repository. It packages the marks instead and opens a
  // pre-filled issue that the reader submits with their own GitHub session;
  // .github/workflows/marks-sync.yml validates the body and commits it. The
  // payload is always shown in a textarea as well, because a long one does
  // not fit in a URL and because nothing here should depend on a step the
  // page cannot verify.
  var SYNC_URL_BUDGET = 6000;

  function allMarkRecords() {
    var out = {};
    var total = 0;
    try { total = localStorage.length; } catch (error) { total = 0; }
    for (var index = 0; index < total; index += 1) {
      var key = localStorage.key(index);
      if (!key || key.indexOf("radar:mark:") !== 0) continue;
      var record = lsGet(key, null);
      if (!record || typeof record !== "object") continue;
      var state = typeof record.state === "string" ? record.state : "";
      var note = typeof record.note === "string" ? record.note : "";
      var at = typeof record.at === "string" ? record.at : "";
      var tags = tagsOf(record);
      // A tombstone (nothing left but a timestamp) has to travel too.
      if (!state && !note && !tags.length && !at) continue;
      out[key.slice("radar:mark:".length)] = {
        state: state,
        tags: tags,
        at: at,
        note: note,
        title: typeof record.title === "string" ? record.title : "",
        date: typeof record.date === "string" ? record.date : "",
        direction: typeof record.direction === "string" ? record.direction : "",
        priority: typeof record.priority === "string" ? record.priority : ""
      };
    }
    return out;
  }

  function deviceId() {
    var stored = lsGet("radar:device", null);
    if (typeof stored === "string" && /^[a-z0-9][a-z0-9_-]{2,31}$/.test(stored)) {
      return stored;
    }
    var random = "";
    for (var index = 0; index < 8; index += 1) {
      random += Math.floor(Math.random() * 16).toString(16);
    }
    var fresh = "dev-" + random;
    lsSet("radar:device", fresh);
    return fresh;
  }

  function repoSlug() {
    // Published at <owner>.github.io/<repo>/; a local preview has no target.
    var match = /^([a-z0-9-]+)\.github\.io$/i.exec(window.location.hostname);
    var segment = window.location.pathname.split("/").filter(Boolean)[0];
    return match && segment ? match[1] + "/" + segment : "";
  }

  function currentPayload() {
    return {
      schema_version: 1,
      device: deviceId(),
      updated_at: new Date().toISOString().replace(/\.\d+Z$/, "Z"),
      marks: allMarkRecords()
    };
  }

  // Byte-identical to what pipeline.marks_store.write_device produces
  // (json.dumps sort_keys=True, indent=1), so the issue route and the direct
  // write below cannot fight over formatting.
  function payloadText(payload) {
    var marks = {};
    Object.keys(payload.marks).sort().forEach(function (key) {
      var source = payload.marks[key];
      var ordered = {};
      Object.keys(source).sort().forEach(function (field) {
        ordered[field] = source[field];
      });
      marks[key] = ordered;
    });
    return JSON.stringify({
      device: payload.device,
      marks: marks,
      schema_version: payload.schema_version,
      updated_at: payload.updated_at
    }, null, 1);
  }

  function syncBody(payload) {
    return "```json\n" + payloadText(payload) + "\n```\n";
  }

  // ---- ADR-0033: optional automatic sync with a browser-held token ----
  //
  // The reader may paste a fine-grained PAT (this repository only, Contents
  // read+write). It is held in localStorage, so anyone who can reach this
  // browser profile can write to the repository; the library page states
  // that and offers a one-click "forget". Without a token nothing here runs
  // and the issue hand-off above stays the only route.
  var TOKEN_KEY = "radar:gh-token";
  var SYNC_DEBOUNCE_MS = 4000;
  var syncTimer = null;
  var syncInFlight = false;
  var syncQueued = false;
  var lastSyncedSignature = null;

  function syncToken() {
    var value = lsGet(TOKEN_KEY, "");
    return typeof value === "string" ? value.trim() : "";
  }

  function autoSyncReady() {
    return Boolean(syncToken() && repoSlug());
  }

  function utf8ToBase64(text) {
    var bytes = new TextEncoder().encode(text);
    var binary = "";
    for (var index = 0; index < bytes.length; index += 1) {
      binary += String.fromCharCode(bytes[index]);
    }
    return btoa(binary);
  }

  function ghFetch(path, options) {
    options = options || {};
    var headers = {
      "Accept": "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "Authorization": "Bearer " + syncToken()
    };
    if (options.body) headers["Content-Type"] = "application/json";
    return fetch("https://api.github.com" + path, {
      method: options.method || "GET",
      headers: headers,
      body: options.body ? JSON.stringify(options.body) : undefined
    });
  }

  function apiError(response) {
    if (response.status === 401) return "令牌无效或已过期（401）";
    if (response.status === 403) return "令牌没有这个仓库的写入权限（403）";
    if (response.status === 404) return "找不到仓库或路径，令牌可能没有勾选这个仓库（404）";
    if (response.status === 409) return "远端已被改动（409）";
    if (response.status === 422) return "GitHub 拒绝了这次写入（422）";
    return "HTTP " + response.status;
  }

  function putMarksFile(text, sha) {
    var body = {
      message: "marks: auto-sync from " + deviceId(),
      content: utf8ToBase64(text),
      branch: "main"
    };
    if (sha) body.sha = sha;
    return ghFetch(
      "/repos/" + repoSlug() + "/contents/data/marks/" + deviceId() + ".json",
      { method: "PUT", body: body }
    );
  }

  function readRemoteSha() {
    return ghFetch("/repos/" + repoSlug() + "/contents/data/marks/" +
      deviceId() + ".json?ref=main").then(function (response) {
      if (response.status === 404) return null;          // first sync
      if (!response.ok) throw new Error(apiError(response));
      return response.json().then(function (data) { return data.sha || null; });
    });
  }

  function pushMarks(force) {
    if (!autoSyncReady()) return Promise.resolve({ skipped: true });
    var payload = currentPayload();
    var signature = JSON.stringify(payload.marks);
    if (!force && signature === lastSyncedSignature) {
      return Promise.resolve({ unchanged: true });
    }
    var text = payloadText(payload);
    var count = Object.keys(payload.marks).length;

    function attempt(retriesLeft) {
      return readRemoteSha()
        .then(function (sha) { return putMarksFile(text, sha); })
        .then(function (response) {
          if (response.status === 409 && retriesLeft > 0) {
            // Another device wrote between our read and our write.
            return attempt(retriesLeft - 1);
          }
          if (!response.ok) throw new Error(apiError(response));
          lastSyncedSignature = signature;
          return { ok: true, count: count };
        });
    }
    return attempt(1);
  }

  function syncChip() {
    var chip = document.getElementById("rui-sync-chip");
    if (!chip) {
      chip = node("div", "rui-sync-chip");
      chip.id = "rui-sync-chip";
      chip.addEventListener("click", function () { chip.hidden = true; });
      document.body.appendChild(chip);
    }
    return chip;
  }

  function showSync(message, kind, sticky) {
    var chip = syncChip();
    chip.textContent = message;
    chip.className = "rui-sync-chip rui-sync-chip--" + kind;
    chip.hidden = false;
    if (chip.timer) clearTimeout(chip.timer);
    if (!sticky) {
      chip.timer = setTimeout(function () { chip.hidden = true; }, 2500);
    }
  }

  function runMarksPush(force) {
    if (!autoSyncReady()) return Promise.resolve();
    if (syncInFlight) {
      syncQueued = true;
      return Promise.resolve();
    }
    syncInFlight = true;
    showSync("正在同步标记…", "busy", true);
    return pushMarks(force).then(function (result) {
      if (result.ok) showSync("✓ 已同步 " + result.count + " 条标记", "ok");
      else if (result.unchanged) showSync("标记无变化", "ok");
      else syncChip().hidden = true;
    }, function (error) {
      // A real HTTP answer, not a guess: say exactly what GitHub refused.
      showSync("同步失败：" + error.message, "bad", true);
    }).then(function () {
      syncInFlight = false;
      if (syncQueued) {
        syncQueued = false;
        return runMarksPush(false);
      }
    });
  }

  function scheduleMarksPush() {
    if (!autoSyncReady()) return;
    clearTimeout(syncTimer);
    syncTimer = setTimeout(function () { runMarksPush(false); }, SYNC_DEBOUNCE_MS);
  }

  document.addEventListener("radar:mark-changed", scheduleMarksPush);
  document.addEventListener("visibilitychange", function () {
    // Leaving the page with a debounce still pending would lose the edit
    // until the next visit; flush instead of waiting it out.
    if (document.visibilityState === "hidden" && syncTimer) {
      clearTimeout(syncTimer);
      syncTimer = null;
      runMarksPush(false);
    }
  });

  var syncBtn = document.getElementById("rui-sync-marks");
  if (syncBtn) {
    syncBtn.addEventListener("click", function () {
      var host = document.getElementById("rui-sync-panel");
      if (!host) return;
      if (host.firstChild) {
        host.replaceChildren();
        return;
      }
      var device = deviceId();
      var marks = allMarkRecords();
      var count = Object.keys(marks).length;
      var payload = {
        schema_version: 1,
        device: device,
        updated_at: new Date().toISOString().replace(/\.\d+Z$/, "Z"),
        marks: marks
      };
      var body = syncBody(payload);

      var status = node("div", "rui-mail-status",
        "设备 " + device + " · " + count + " 条标记 · 正在复制…");
      host.appendChild(status);

      var area = document.createElement("textarea");
      area.className = "rui-mail-ta";
      area.readOnly = true;
      area.value = body;
      host.appendChild(area);

      var actions = node("div", "rui-mail-actions");
      var copy = node("button", "rui-btn", "复制内容");
      copy.type = "button";
      actions.appendChild(copy);

      var slug = repoSlug();
      var title = "marks sync " + device;
      var base = slug ? "https://github.com/" + slug + "/issues/new" : "";
      var withBody = base + "?labels=marks-sync&title=" +
        encodeURIComponent(title) + "&body=" + encodeURIComponent(body);
      var withoutBody = base + "?labels=marks-sync&title=" +
        encodeURIComponent(title) + "&body=" +
        encodeURIComponent("把上一步复制的内容粘贴到这里，然后提交。\n\n");
      var fits = withBody.length <= SYNC_URL_BUDGET;

      if (base) {
        var open = node("a", "rui-btn rui-secondary",
          fits ? "打开 GitHub 提交页（已预填）" : "打开 GitHub 提交页（需粘贴）");
        open.href = fits ? withBody : withoutBody;
        open.target = "_blank";
        open.rel = "noopener noreferrer";
        actions.appendChild(open);
      }
      var close = node("button", "rui-btn rui-secondary", "收起");
      close.type = "button";
      actions.appendChild(close);
      host.appendChild(actions);

      host.appendChild(node("div", "rui-mail-note", base
        ? "提交后由 marks-sync 工作流校验并写入 data/marks/，" +
          "只接受仓库所有者本人开的 issue；完成后它会回帖并关闭该 issue。"
        : "本地预览没有对应的仓库地址；复制内容后到已发布的站点或直接在 GitHub 上新建 issue 提交。"));

      function copyAll() {
        if (!navigator.clipboard || !navigator.clipboard.writeText) {
          status.textContent = "设备 " + device + " · " + count +
            " 条标记 · 浏览器不允许自动复制，请在文本框里全选复制。";
          return;
        }
        navigator.clipboard.writeText(area.value).then(function () {
          status.textContent = "设备 " + device + " · " + count +
            " 条标记 · ✓ 已复制" + (fits ? "（提交页也已预填）" : "，请粘贴到提交页");
        }, function () {
          status.textContent = "设备 " + device + " · " + count +
            " 条标记 · 自动复制被拒绝，请在文本框里全选复制。";
        });
      }
      copy.addEventListener("click", copyAll);
      close.addEventListener("click", function () { host.replaceChildren(); });
      copyAll();
      area.select();
    });
  }

  // ---- library.html: the automatic-sync settings (ADR-0033) ----
  var tokenInput = document.getElementById("rui-token-input");
  if (tokenInput) {
    var tokenStatus = document.getElementById("rui-token-status");
    var saveBtn = document.getElementById("rui-token-save");
    var forgetBtn = document.getElementById("rui-token-forget");
    var nowBtn = document.getElementById("rui-token-now");

    function renderTokenStatus(message, kind) {
      tokenStatus.className = "rui-mail-status" + (kind ? " rui-token--" + kind : "");
      if (message) {
        tokenStatus.textContent = message;
        return;
      }
      var token = syncToken();
      if (!token) {
        tokenStatus.textContent = "未启用：改动标记后需要手动走上面的 issue 流程。";
      } else if (!repoSlug()) {
        tokenStatus.textContent = "已保存令牌，但本地预览没有仓库地址，自动同步不会运行。";
      } else {
        tokenStatus.textContent = "已启用：设备 " + deviceId() + " · 令牌 …" +
          token.slice(-4) + " · 改动后约 " + (SYNC_DEBOUNCE_MS / 1000) + " 秒自动提交。";
      }
    }

    function updateButtons() {
      var has = Boolean(syncToken());
      forgetBtn.disabled = !has;
      nowBtn.disabled = !has;
    }

    renderTokenStatus();
    updateButtons();

    saveBtn.addEventListener("click", function () {
      var value = tokenInput.value.trim();
      if (!value) {
        renderTokenStatus("请先粘贴令牌。", "bad");
        return;
      }
      lsSet(TOKEN_KEY, value);
      tokenInput.value = "";
      updateButtons();
      if (!repoSlug()) {
        renderTokenStatus("已保存，但本地预览没有仓库地址，无法验证。", "bad");
        return;
      }
      // Verify by actually writing: a token that cannot push is not "saved".
      renderTokenStatus("正在用一次真实提交验证令牌…");
      runMarksPush(true).then(function () {
        renderTokenStatus();
      });
    });

    forgetBtn.addEventListener("click", function () {
      if (!confirm("确定从这个浏览器删除令牌？自动同步会停止，已同步的数据不受影响。")) {
        return;
      }
      try { localStorage.removeItem(TOKEN_KEY); } catch (error) { /* ignore */ }
      lastSyncedSignature = null;
      updateButtons();
      renderTokenStatus("令牌已删除，自动同步已停止。", "ok");
    });

    nowBtn.addEventListener("click", function () {
      renderTokenStatus("正在同步…");
      runMarksPush(true).then(function () { renderTokenStatus(); });
    });
  }

  // ---- my-marks.html: export-all + listing ----
  var exportBtn = document.getElementById("rui-export-marks");
  if (exportBtn) {
    function collectMarks() {
      var out = {};
      for (var i = 0; i < localStorage.length; i++) {
        var k = localStorage.key(i);
        if (k && k.indexOf("radar:mark:") === 0) {
          var parsed;
          try {
            parsed = JSON.parse(localStorage.getItem(k));
          } catch (e) {
            parsed = localStorage.getItem(k);
          }
          // Skip tombstones: a cleared mark is not part of the reading trail.
          if (parsed && typeof parsed === "object" && !parsed.state &&
              !parsed.note && !tagsOf(parsed).length) continue;
          out[k] = parsed;
        }
      }
      return out;
    }
    exportBtn.addEventListener("click", function () {
      var data = collectMarks();
      var blob = new Blob([JSON.stringify(data, null, 2)], {
        type: "application/json"
      });
      var a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = "radar-marks-" + new Date().toISOString().slice(0, 10) + ".json";
      document.body.appendChild(a);
      a.click();
      a.remove();
    });
    var listEl = document.getElementById("rui-marks-list");
    if (listEl) {
      var marks = collectMarks();
      var keys = Object.keys(marks);
      if (!keys.length) {
        listEl.innerHTML = "<p>当前浏览器还没有阅读标记。</p>";
      } else {
        var rows = keys.map(function (k) {
          var r = marks[k] || {};
          var title = esc(r.title || k.replace("radar:mark:", ""));
          var titleCell = /^\d{4}-\d{2}-\d{2}$/.test(r.date || "") ?
            '<a href="' + esc(r.date) + '.html">' + title + "</a>" : title;
          return "<tr><td>" + titleCell + "</td><td>" + esc(r.date || "") +
            "</td><td>" + esc(r.state || "") + "</td><td>" +
            esc(r.note || "") + "</td><td><code>" + esc(r.at || "") +
            "</code></td></tr>";
        });
        listEl.innerHTML =
          '<div class="table-scroll"><table class="rui-table"><thead><tr><th>论文</th>' +
          "<th>日期</th><th>标记</th><th>笔记</th><th>更新时间</th></tr></thead><tbody>" +
          rows.join("") + "</tbody></table></div>";
      }
    }
  }

})();
