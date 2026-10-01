/* Reading list — full cards for every paper marked in this browser.
 *
 * ADR-0016 addendum (2026-09-17). Marks live in localStorage as
 * `radar:mark:<identity_key>` -> { state, at, note, title, date, direction,
 * priority }. This page groups them by state, searches title / note /
 * direction, and renders the complete card by fetching the paper's day shard
 * (data/day/<date>/manifest.json -> page-N.json) through the shared
 * RadarCard renderer, so marks and notes stay editable in place.
 *
 * Zero dependencies. Nothing is written back to Git; the list is as private
 * and as ephemeral as the marks themselves.
 */
(function () {
  "use strict";

  var PAGE_SIZE = 20;
  var results = document.getElementById("reading-results");
  var status = document.getElementById("reading-status");
  var tabsWrap = document.getElementById("reading-state");
  var queryInput = document.getElementById("reading-query");
  var sortSelect = document.getElementById("reading-sort");
  var pagination = document.getElementById("reading-pagination");
  var previous = document.getElementById("reading-prev");
  var next = document.getElementById("reading-next");
  var pageSelect = document.getElementById("reading-page");
  var pageTotal = document.getElementById("reading-page-total");
  var copyButton = document.getElementById("reading-copy");
  var tagSelect = document.getElementById("reading-tag");
  if (!results || !status || !tabsWrap || !queryInput || !sortSelect ||
      !pagination || !previous || !next || !pageSelect || !pageTotal) return;
  if (!window.RadarCard || !window.RadarCard.buildCard) {
    status.textContent = "卡片组件加载失败，请刷新页面重试。";
    return;
  }

  // ADR-0034: 有启发 left this row and became a tag, which the 标签 picker
  // filters on — so a paper can be 已阅读 *and* 有启发 at the same time.
  var STATE_LABELS = {
    "to-read": "待阅读", "read": "已阅读", "ignore": "忽略"
  };
  var TABS = ["to-read", "read", "ignore", "note", "all"];

  var view = { tab: "to-read", query: "", tag: "", sort: "marked", page: 1 };
  var generation = 0;
  var manifestCache = {};
  var pageCache = {};
  var recordCache = {};

  function element(tag, className, value) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (value != null) node.textContent = String(value);
    return node;
  }

  function lsGet(key) {
    try {
      var raw = localStorage.getItem(key);
      return raw == null ? null : JSON.parse(raw);
    } catch (error) {
      return null;
    }
  }

  // ---- marks --------------------------------------------------------------

  function normTags(value) {
    if (!Array.isArray(value)) return [];
    var out = [];
    for (var index = 0; index < value.length; index += 1) {
      var tag = typeof value[index] === "string" ? value[index].trim() : "";
      if (tag && out.indexOf(tag) < 0) out.push(tag);
    }
    return out.sort();
  }

  function collectMarks() {
    var out = [];
    var count = 0;
    try { count = localStorage.length; } catch (error) { count = 0; }
    for (var index = 0; index < count; index += 1) {
      var key = localStorage.key(index);
      if (!key || key.indexOf("radar:mark:") !== 0) continue;
      var record = lsGet(key);
      if (!record || typeof record !== "object") continue;
      var mark = {
        idkey: key.slice("radar:mark:".length),
        state: typeof record.state === "string" ? record.state : "",
        tags: normTags(record.tags),
        at: typeof record.at === "string" ? record.at : "",
        note: typeof record.note === "string" ? record.note : "",
        title: typeof record.title === "string" ? record.title : "",
        date: typeof record.date === "string" ? record.date : "",
        direction: typeof record.direction === "string" ? record.direction : "",
        priority: typeof record.priority === "string" ? record.priority : ""
      };
      // A cleared mark is kept in storage as a tombstone so the sync can
      // propagate the deletion; it is not part of the reading trail.
      if (!mark.state && !mark.note && !mark.tags.length) continue;
      out.push(mark);
    }
    return out;
  }

  function inTab(mark, tab) {
    if (tab === "all") return true;
    if (tab === "note") return Boolean(mark.note);
    return mark.state === tab;
  }

  function hasTag(mark, tag) {
    return !tag || mark.tags.indexOf(tag) >= 0;
  }

  // Every tag in use, commonest first, for the picker.
  function knownTags(all) {
    var counts = {};
    all.forEach(function (mark) {
      mark.tags.forEach(function (tag) {
        counts[tag] = (counts[tag] || 0) + 1;
      });
    });
    return Object.keys(counts).sort(function (a, b) {
      if (counts[b] !== counts[a]) return counts[b] - counts[a];
      return a < b ? -1 : (a > b ? 1 : 0);
    }).map(function (tag) { return { tag: tag, count: counts[tag] }; });
  }

  function matchesQuery(mark, query) {
    if (!query) return true;
    var haystack = [mark.title, mark.note, mark.tags.join(" "),
      mark.direction, mark.date, mark.idkey].join(" ").toLowerCase();
    return query.split(/\s+/).every(function (term) {
      return term === "" || haystack.indexOf(term) >= 0;
    });
  }

  function sortMarks(marks) {
    var copy = marks.slice();
    if (view.sort === "date") {
      copy.sort(function (a, b) { return (b.date || "").localeCompare(a.date || ""); });
    } else if (view.sort === "title") {
      copy.sort(function (a, b) { return (a.title || "").localeCompare(b.title || ""); });
    } else {
      copy.sort(function (a, b) { return (b.at || "").localeCompare(a.at || ""); });
    }
    return copy;
  }

  function visibleMarks(all) {
    var query = view.query.trim().toLowerCase();
    return sortMarks(all.filter(function (mark) {
      return inTab(mark, view.tab) && hasTag(mark, view.tag) &&
        matchesQuery(mark, query);
    }));
  }

  // ---- full records from the day shards ----------------------------------

  function fetchJson(url) {
    return fetch(url, { cache: "no-store" }).then(function (response) {
      if (!response.ok) throw new Error("HTTP " + response.status);
      return response.json();
    });
  }

  function anchorFor(idkey) {
    return String(idkey).replace(/[^A-Za-z0-9_-]/g, "-");
  }

  function manifestFor(date) {
    if (!manifestCache[date]) {
      manifestCache[date] = fetchJson(
        "data/day/" + encodeURIComponent(date) + "/manifest.json"
      );
    }
    return manifestCache[date];
  }

  function pageFor(date, page, revision) {
    var key = date + "/" + page;
    if (!pageCache[key]) {
      var suffix = revision ? "?v=" + encodeURIComponent(String(revision)) : "";
      pageCache[key] = fetchJson(
        "data/day/" + encodeURIComponent(date) + "/page-" + page + ".json" + suffix
      );
    }
    return pageCache[key];
  }

  function pageRecords(payload) {
    if (Array.isArray(payload)) return payload;
    if (!payload || typeof payload !== "object") return [];
    return payload.papers || payload.records || payload.items || [];
  }

  function recordFor(mark) {
    if (Object.prototype.hasOwnProperty.call(recordCache, mark.idkey)) {
      return Promise.resolve(recordCache[mark.idkey]);
    }
    if (!/^\d{4}-\d{2}-\d{2}$/.test(mark.date)) {
      recordCache[mark.idkey] = null;
      return Promise.resolve(null);
    }
    var lookup = manifestFor(mark.date).then(function (manifest) {
      var anchor = anchorFor(mark.idkey);
      var pages = (manifest && manifest.anchor_pages) || {};
      var candidates = [];
      if (pages[anchor] != null) candidates.push(Number(pages[anchor]));
      Object.keys(pages).forEach(function (key) {
        if (key.indexOf(anchor + "--") === 0) candidates.push(Number(pages[key]));
      });
      if (!candidates.length) {
        var total = Number(manifest && manifest.page_count) || 0;
        for (var page = 1; page <= Math.min(total, 25); page += 1) candidates.push(page);
      }
      return candidates.reduce(function (chain, page) {
        return chain.then(function (found) {
          if (found) return found;
          return pageFor(mark.date, page, manifest && manifest.revision)
            .then(function (payload) {
              return pageRecords(payload).filter(function (record) {
                return record && record.identity_key === mark.idkey;
              })[0] || null;
            });
        });
      }, Promise.resolve(null));
    }).catch(function () { return null; });
    return lookup.then(function (record) {
      recordCache[mark.idkey] = record;
      return record;
    });
  }

  // ---- rendering ----------------------------------------------------------

  function fallbackRecord(mark) {
    return {
      identity_key: mark.idkey,
      title: mark.title || mark.idkey,
      date: mark.date,
      direction: mark.direction,
      priority: mark.priority || "Low",
      relevance_to_user: "完整卡片不在当前站点数据中（可能来自旧数据或已被去重合并）。",
      doi: mark.idkey.indexOf("doi:") === 0 ? mark.idkey.slice(4) : "",
      url: mark.idkey.indexOf("arxiv:") === 0 ?
        "https://arxiv.org/abs/" + mark.idkey.slice(6) : ""
    };
  }

  function decorate(card, mark, isFallback) {
    var stamp = element("div", "reading-stamp");
    stamp.appendChild(element("span", "reading-stamp__state m-" + (mark.state || "note"),
      STATE_LABELS[mark.state] || "仅笔记"));
    mark.tags.forEach(function (tag) {
      stamp.appendChild(element("span", "reading-stamp__tag", tag));
    });
    if (mark.at) {
      stamp.appendChild(element("span", "reading-stamp__at", "标记于 " + mark.at.slice(0, 10)));
    }
    if (isFallback) {
      var search = element("a", "reading-stamp__search", "去搜索页查找 →");
      search.href = "search.html?q=" + encodeURIComponent(mark.title || mark.idkey);
      stamp.appendChild(search);
    }
    var note = element("div", "reading-note");
    note.hidden = !mark.note;
    note.textContent = mark.note ? "笔记：" + mark.note : "";
    var tools = card.querySelector(".rui-card-tools");
    card.insertBefore(stamp, tools || null);
    card.insertBefore(note, tools || null);
  }

  function hydrate() {
    if (window.RadarUI && window.RadarUI.hydrate) {
      window.RadarUI.hydrate(results);
    } else {
      document.dispatchEvent(new CustomEvent("radar:content-ready", {
        detail: { root: results }
      }));
    }
    // This page shows exactly what the user marked; never let the daily-page
    // priority / mark filters hide anything here.
    results.querySelectorAll(".paper").forEach(function (card) {
      card.dataset.hidden = "0";
    });
  }

  // The picker is rebuilt from the marks themselves, so a tag that was just
  // added to a card appears without a reload and one that was removed from
  // the last paper holding it disappears.
  function updateTagPicker(all) {
    if (!tagSelect) return;
    var known = knownTags(all);
    if (view.tag && !known.some(function (e) { return e.tag === view.tag; })) {
      view.tag = "";  // the last paper with this tag lost it
    }
    tagSelect.textContent = "";
    var any = document.createElement("option");
    any.value = "";
    any.textContent = "全部标签";
    tagSelect.appendChild(any);
    known.forEach(function (entry) {
      var option = document.createElement("option");
      option.value = entry.tag;
      option.textContent = entry.tag + " (" + entry.count + ")";
      tagSelect.appendChild(option);
    });
    tagSelect.value = view.tag;
    tagSelect.disabled = known.length === 0;
  }

  function updateTabs(all) {
    updateTagPicker(all);
    TABS.forEach(function (tab) {
      var button = tabsWrap.querySelector('[data-state="' + tab + '"]');
      if (!button) return;
      // Counted inside the chosen tag, so the numbers match what a click
      // actually shows.
      var count = all.filter(function (mark) {
        return inTab(mark, tab) && hasTag(mark, view.tag);
      }).length;
      var label = button.dataset.label || button.textContent;
      button.dataset.label = label;
      button.textContent = label + " (" + count + ")";
      button.classList.toggle("is-active", tab === view.tab);
    });
  }

  function renderPagination(count) {
    var pages = Math.max(1, Math.ceil(count / PAGE_SIZE));
    var options = document.createDocumentFragment();
    for (var page = 1; page <= pages; page += 1) {
      var option = element("option", "", "第 " + page + " 页");
      option.value = String(page);
      options.appendChild(option);
    }
    pageSelect.replaceChildren(options);
    pageSelect.value = String(view.page);
    previous.disabled = view.page <= 1;
    next.disabled = view.page >= pages;
    pageTotal.textContent = "共 " + pages + " 页";
    pagination.hidden = count <= PAGE_SIZE;
  }

  function writeUrl() {
    var params = new URLSearchParams();
    if (view.tab !== "to-read") params.set("state", view.tab);
    if (view.query.trim()) params.set("q", view.query.trim());
    if (view.sort !== "marked") params.set("sort", view.sort);
    if (view.page > 1) params.set("page", String(view.page));
    var query = params.toString();
    window.history.replaceState(null, "", query ? "?" + query : "reading.html");
  }

  function render() {
    generation += 1;
    var requestGeneration = generation;
    var all = collectMarks();
    var list = visibleMarks(all);
    var pages = Math.max(1, Math.ceil(list.length / PAGE_SIZE));
    view.page = Math.min(Math.max(1, view.page), pages);
    updateTabs(all);
    renderPagination(list.length);
    writeUrl();

    if (!all.length) {
      results.replaceChildren(element("p", "empty",
        "这个浏览器里还没有任何标记。在今日、队列或搜索页的卡片上点“待阅读”“已阅读”等即可加入。"));
      status.textContent = "共 0 条";
      return;
    }
    if (!list.length) {
      results.replaceChildren(element("p", "empty", "没有符合当前分栏和搜索词的标记。"));
      status.textContent = "共 0 条 · 全部标记 " + all.length + " 条";
      return;
    }

    var slice = list.slice((view.page - 1) * PAGE_SIZE, view.page * PAGE_SIZE);
    results.setAttribute("aria-busy", "true");
    status.textContent = "共 " + list.length + " 条 · 第 " + view.page + " / " + pages +
      " 页 · 正在加载卡片…";

    Promise.all(slice.map(recordFor)).then(function (records) {
      if (requestGeneration !== generation) return;
      var fragment = document.createDocumentFragment();
      var missing = 0;
      slice.forEach(function (mark, index) {
        var record = records[index];
        if (!record) missing += 1;
        var card = window.RadarCard.buildCard(record || fallbackRecord(mark),
          { dailyLink: true });
        decorate(card, mark, !record);
        fragment.appendChild(card);
      });
      results.replaceChildren(fragment);
      results.setAttribute("aria-busy", "false");
      hydrate();
      if (window.RadarCard.enhanceVisuals) window.RadarCard.enhanceVisuals(results);
      status.textContent = "共 " + list.length + " 条 · 第 " + view.page + " / " + pages +
        " 页" + (missing ? " · " + missing + " 条只有标记时保存的摘要信息" : "");
    });
  }

  // ---- events -------------------------------------------------------------

  tabsWrap.addEventListener("click", function (event) {
    var button = event.target.closest("[data-state]");
    if (!button) return;
    view.tab = button.dataset.state;
    view.page = 1;
    render();
  });

  if (tagSelect) {
    tagSelect.addEventListener("change", function () {
      view.tag = tagSelect.value;
      view.page = 1;
      render();
    });
  }

  var timer = null;
  queryInput.addEventListener("input", function () {
    clearTimeout(timer);
    timer = setTimeout(function () {
      view.query = queryInput.value;
      view.page = 1;
      render();
    }, 150);
  });

  sortSelect.addEventListener("change", function () {
    view.sort = sortSelect.value;
    view.page = 1;
    render();
  });

  previous.addEventListener("click", function () { view.page -= 1; render(); });
  next.addEventListener("click", function () { view.page += 1; render(); });
  pageSelect.addEventListener("change", function () {
    view.page = Number(pageSelect.value) || 1;
    render();
  });

  // A mark changed on a card: its tab membership may have changed. Re-render
  // after radar-ui.js has saved the new state (it listens on the same event).
  results.addEventListener("change", function (event) {
    if (event.target && event.target.classList.contains("rui-mark-radio")) {
      setTimeout(render, 0);
    }
  });
  results.addEventListener("click", function (event) {
    if (event.target && event.target.classList.contains("rui-mark-radio")) {
      setTimeout(render, 0); // covers the re-click-to-clear case
    }
  });
  results.addEventListener("focusout", function (event) {
    if (!event.target || !event.target.classList.contains("rui-note-ta")) return;
    var card = event.target.closest(".paper");
    var note = card && card.querySelector(".reading-note");
    if (!note) return;
    var value = event.target.value;
    note.hidden = !value;
    note.textContent = value ? "笔记：" + value : "";
    var button = tabsWrap.querySelector('[data-state="note"]');
    if (button) setTimeout(function () { updateTabs(collectMarks()); }, 0);
  });

  // radar-ui.js fires this for every mark write, tags included.
  document.addEventListener("radar:mark-changed", function () {
    setTimeout(function () { updateTabs(collectMarks()); }, 0);
  });

  if (copyButton) {
    copyButton.addEventListener("click", function () {
      var list = visibleMarks(collectMarks());
      var lines = list.map(function (mark) {
        var link = mark.idkey.indexOf("doi:") === 0 ?
          "https://doi.org/" + mark.idkey.slice(4) :
          mark.idkey.indexOf("arxiv:") === 0 ?
            "https://arxiv.org/abs/" + mark.idkey.slice(6) : "";
        var head = "- " + (link ? "[" + (mark.title || mark.idkey) + "](" + link + ")" :
          (mark.title || mark.idkey));
        var tail = [mark.date, STATE_LABELS[mark.state] || "仅笔记"]
          .concat(mark.tags.map(function (tag) { return "#" + tag; }))
          .filter(Boolean).join(" · ");
        return head + " — " + tail + (mark.note ? "\n  笔记：" + mark.note.replace(/\n/g, " ") : "");
      });
      var text = lines.join("\n");
      var done = function () {
        copyButton.textContent = "✓ 已复制 " + list.length + " 条";
        setTimeout(function () { copyButton.textContent = "复制当前清单为 Markdown"; }, 2000);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, function () {
          window.prompt("复制下面的清单：", text);
        });
      } else {
        window.prompt("复制下面的清单：", text);
      }
    });
  }

  // ---- init from URL ------------------------------------------------------

  (function initFromUrl() {
    var params = new URLSearchParams(window.location.search);
    var tab = params.get("state");
    if (tab && TABS.indexOf(tab) >= 0) view.tab = tab;
    view.query = params.get("q") || "";
    queryInput.value = view.query;
    var sort = params.get("sort");
    if (sort && ["marked", "date", "title"].indexOf(sort) >= 0) {
      view.sort = sort;
      sortSelect.value = sort;
    }
    view.page = Math.max(1, Number(params.get("page")) || 1);
  })();

  window.addEventListener("storage", function (event) {
    if (event.key && event.key.indexOf("radar:mark:") === 0) render();
  });

  render();
})();
