/* Lazy daily-paper loader with stable 20-card pages and hash routing. */
(function () {
  "use strict";

  var PAGE_SIZE = 20;
  var manifest = null;
  var generation = 0;
  var currentPage = 1;
  var main = document.querySelector("main[data-date]");
  var results = document.getElementById("day-results");
  var status = document.getElementById("day-status");
  var pagination = document.getElementById("day-pagination");
  var previous = document.getElementById("day-prev");
  var pageSelect = document.getElementById("day-page");
  var pageTotal = document.getElementById("day-page-total");
  var next = document.getElementById("day-next");
  var stats = document.getElementById("day-stats");
  if (!main || !results || !status || !pagination || !previous ||
      !pageSelect || !pageTotal || !next) return;

  var date = String(main.dataset.date || "");
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) {
    status.textContent = "页面日期无效，无法加载论文数据。";
    return;
  }
  if (!window.RadarCard || !window.RadarCard.buildCard) {
    status.textContent = "卡片组件加载失败，请刷新页面重试。";
    return;
  }

  function element(tag, className, value) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (value != null) node.textContent = String(value);
    return node;
  }

  function naturalNumber(value, fallback) {
    var parsed = Number(value);
    return Number.isFinite(parsed) && parsed >= 0 ? Math.floor(parsed) : fallback;
  }

  function totalPapers() {
    if (!manifest) return 0;
    if (manifest.total != null) return naturalNumber(manifest.total, 0);
    if (manifest.paper_count != null) {
      return naturalNumber(manifest.paper_count, 0);
    }
    var counts = manifest.priority_counts || manifest.counts || {};
    return Object.keys(counts).reduce(function (sum, key) {
      return sum + naturalNumber(counts[key], 0);
    }, 0);
  }

  function pageCount() {
    if (!manifest) return 0;
    if (manifest.page_count != null) {
      return naturalNumber(manifest.page_count, 0);
    }
    return Math.ceil(totalPapers() / PAGE_SIZE);
  }

  function clampPage(page) {
    var count = pageCount();
    if (!count) return 1;
    return Math.min(Math.max(1, naturalNumber(page, 1)), count);
  }

  function fragmentAnchor() {
    var raw = window.location.hash.slice(1);
    if (!raw) return "";
    try {
      return decodeURIComponent(raw);
    } catch (error) {
      return raw;
    }
  }

  function pageFromUrl() {
    var params = new URLSearchParams(window.location.search);
    return Math.max(1, naturalNumber(params.get("page"), 1));
  }

  function pageForAnchor(anchor) {
    var pages = (manifest && manifest.anchor_pages) || {};
    return anchor && pages[anchor] != null ?
      naturalNumber(pages[anchor], 0) : 0;
  }

  function blurActiveEditor() {
    var active = document.activeElement;
    if (active && active !== document.body && typeof active.blur === "function") {
      active.blur();
    }
  }

  function writeUrl(page, mode, preserveHash) {
    var url = new URL(window.location.href);
    if (page > 1) url.searchParams.set("page", String(page));
    else url.searchParams.delete("page");
    if (!preserveHash) url.hash = "";
    window.history[mode === "push" ? "pushState" : "replaceState"](
      null, "", url.pathname + url.search + url.hash
    );
  }

  function renderStats() {
    if (!stats || !manifest) return;
    var counts = manifest.priority_counts || manifest.counts || {};
    var cards = [
      ["论文总数", totalPapers(), ""],
      ["High", naturalNumber(counts.High, 0), "#27500A"],
      ["Medium", naturalNumber(counts.Medium, 0), "#633806"],
      ["Low", naturalNumber(counts.Low, 0), "#5F5E5A"]
    ];
    if (naturalNumber(counts.Unscored, 0)) {
      cards.push(["待评分", naturalNumber(counts.Unscored, 0), ""]);
    }
    var fragment = document.createDocumentFragment();
    cards.forEach(function (item) {
      var card = element("div", "stat");
      card.appendChild(element("div", "stat-label", item[0]));
      var value = element("div", "stat-val", item[1]);
      if (item[2]) value.style.color = item[2];
      card.appendChild(value);
      fragment.appendChild(card);
    });
    stats.replaceChildren(fragment);
  }

  function renderPagination(loading) {
    var count = pageCount();
    var options = document.createDocumentFragment();
    var optionCount = Math.max(1, count);
    for (var page = 1; page <= optionCount; page += 1) {
      var option = element("option", "", count ? "第 " + page + " 页" : "无页面");
      option.value = String(page);
      options.appendChild(option);
    }
    pageSelect.replaceChildren(options);
    pageSelect.value = String(currentPage);
    pageSelect.disabled = loading || count === 0;
    previous.disabled = loading || currentPage <= 1 || count === 0;
    next.disabled = loading || currentPage >= count || count === 0;
    pageTotal.textContent = "共 " + count + " 页";
    pagination.hidden = count === 0;
  }

  function pageRecords(payload) {
    if (Array.isArray(payload)) return payload;
    if (!payload || typeof payload !== "object") return null;
    if (Array.isArray(payload.papers)) return payload.papers;
    if (Array.isArray(payload.records)) return payload.records;
    if (Array.isArray(payload.items)) return payload.items;
    return null;
  }

  function hydrate() {
    if (window.RadarUI && window.RadarUI.hydrate) {
      window.RadarUI.hydrate(results);
    } else {
      document.dispatchEvent(new CustomEvent("radar:content-ready", {
        detail: { root: results }
      }));
    }
  }

  function focusTarget(anchor) {
    if (!anchor) return;
    var target = document.getElementById(anchor);
    if (!target || !results.contains(target)) {
      status.textContent += " · 未在当前数据中找到定位论文";
      return;
    }
    target.classList.add("is-targeted");
    target.dataset.hidden = "0";
    target.tabIndex = -1;
    var schedule = window.requestAnimationFrame || function (callback) {
      window.setTimeout(callback, 0);
    };
    schedule(function () {
      try {
        target.scrollIntoView({ block: "center", behavior: "smooth" });
      } catch (error) {
        target.scrollIntoView();
      }
      try {
        target.focus({ preventScroll: true });
      } catch (error) {
        target.focus();
      }
    });
  }

  // The filters above the grid are applied after the cards are built, so the
  // count printed at render time is a claim the page cannot keep on its own.
  var baseStatus = "";
  var cardsOnPage = 0;   // NOT `pageTotal`: that name is the "共 N 页" element

  function describeFilters(hidden) {
    if (!cardsOnPage || !hidden) return "";
    if (hidden >= cardsOnPage) {
      return " · 本页 " + hidden + " 篇都被上方筛选隐藏了";
    }
    return " · 已按筛选隐藏 " + hidden + " 篇";
  }

  // The "all hidden — 显示全部" notice itself is drawn by radar-ui.js, which
  // owns the filters, so every page with a filter bar gets the same one.
  document.addEventListener("radar:filters-applied", function (event) {
    if (!baseStatus) return;
    var detail = event.detail || {};
    status.textContent = baseStatus + describeFilters(detail.hidden || 0);
  });

  function showPage(records, targetAnchor) {
    var fragment = document.createDocumentFragment();
    records.slice(0, PAGE_SIZE).forEach(function (record) {
      fragment.appendChild(window.RadarCard.buildCard(record));
    });
    results.replaceChildren(fragment);
    results.setAttribute("aria-busy", "false");
    // The base line has to exist before hydrate() runs the filters, or the
    // first render dispatches into a listener with nothing to append to and
    // the note only appears once a filter is touched.
    var count = pageCount();
    cardsOnPage = records.length;
    baseStatus = "全天 " + totalPapers() + " 篇 · 第 " +
      currentPage + " / " + count + " 页 · 当前页 " + cardsOnPage + " 篇";
    status.textContent = baseStatus;
    hydrate();
    renderPagination(false);
    focusTarget(targetAnchor);
  }

  function showError(message, retry) {
    results.setAttribute("aria-busy", "false");
    var box = element("div", "empty");
    box.appendChild(element("p", "", message));
    var button = element("button", "queue-page-button", "重试");
    button.type = "button";
    button.addEventListener("click", retry);
    box.appendChild(button);
    results.replaceChildren(box);
    status.textContent = message;
    renderPagination(false);
  }

  function loadPage(page, options) {
    options = options || {};
    // Commit an open note before cards are replaced, including browser
    // back/forward and hash-driven cross-page navigation.
    blurActiveEditor();
    generation += 1;
    var requestGeneration = generation;
    currentPage = clampPage(page);
    var targetAnchor = options.targetAnchor || "";
    if (options.historyMode) {
      writeUrl(currentPage, options.historyMode, Boolean(targetAnchor));
    }
    renderPagination(true);
    results.setAttribute("aria-busy", "true");
    status.textContent = "正在加载第 " + currentPage + " 页…";

    if (!pageCount()) {
      results.replaceChildren(element("p", "empty", "当天没有论文。"));
      results.setAttribute("aria-busy", "false");
      status.textContent = "全天 0 篇";
      renderPagination(false);
      return Promise.resolve();
    }

    var requestedPage = currentPage;
    var revision = manifest.revision ?
      "?v=" + encodeURIComponent(String(manifest.revision)) : "";
    var url = "data/day/" + encodeURIComponent(date) +
      "/page-" + requestedPage + ".json" + revision;
    return fetch(url, { cache: "no-store" })
      .then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json();
      })
      .then(function (payload) {
        if (requestGeneration !== generation) return;
        if (payload && payload.revision && manifest.revision &&
            String(payload.revision) !== String(manifest.revision)) {
          throw new Error("数据版本正在切换，请稍后重试");
        }
        if (payload && payload.page != null &&
            naturalNumber(payload.page, 0) !== requestedPage) {
          throw new Error("页面分片编号不一致，请稍后重试");
        }
        var records = pageRecords(payload);
        if (!records) throw new Error("页面分片格式无效");
        showPage(records, targetAnchor);
      })
      .catch(function (error) {
        if (requestGeneration !== generation) return;
        showError("论文数据加载失败：" + error.message, function () {
          init(true);
        });
      });
  }

  function routeFromLocation(historyMode) {
    var anchor = fragmentAnchor();
    var anchorPage = pageForAnchor(anchor);
    var page = anchorPage || pageFromUrl();
    return loadPage(page, {
      targetAnchor: anchor,
      historyMode: historyMode || ""
    });
  }

  function init(isRetry) {
    manifest = null;
    generation += 1;
    var requestGeneration = generation;
    status.textContent = isRetry ? "正在重新同步论文数据…" : "正在加载论文数据…";
    results.setAttribute("aria-busy", "true");
    renderPagination(true);
    return fetch("data/day/" + encodeURIComponent(date) + "/manifest.json", {
      cache: "no-store"
    })
      .then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json();
      })
      .then(function (data) {
        if (requestGeneration !== generation) return;
        if (!data || typeof data !== "object") {
          throw new Error("manifest 格式无效");
        }
        if (data.date && String(data.date) !== date) {
          throw new Error("manifest 日期不一致");
        }
        manifest = data;
        renderStats();
        return routeFromLocation("replace");
      })
      .catch(function (error) {
        if (requestGeneration !== generation) return;
        showError("每日数据索引加载失败：" + error.message, function () {
          init(true);
        });
      });
  }

  function navigate(page) {
    if (!manifest) return;
    blurActiveEditor();
    loadPage(page, { historyMode: "push" });
  }

  previous.addEventListener("click", function () {
    if (!previous.disabled) navigate(currentPage - 1);
  });
  next.addEventListener("click", function () {
    if (!next.disabled) navigate(currentPage + 1);
  });
  pageSelect.addEventListener("change", function () {
    if (!pageSelect.disabled) navigate(Number(pageSelect.value));
  });
  window.addEventListener("popstate", function () {
    if (manifest) routeFromLocation("");
  });
  window.addEventListener("hashchange", function () {
    if (manifest) routeFromLocation("replace");
  });

  init(false);
})();
