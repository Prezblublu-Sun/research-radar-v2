/* Metadata-first progressive search — ADR-0027. */
(function () {
  "use strict";

  // The page loads this bundle with a ?v=<content hash> so a deploy is not
  // stuck behind the ten-minute cache. The worker is fetched from here
  // rather than from the page, so it has to inherit that query or it keeps
  // running yesterday's copy.
  var assetQuery = (function () {
    var src = (document.currentScript && document.currentScript.src) || "";
    var at = src.indexOf("?");
    return at >= 0 ? src.slice(at) : "";
  })();

  var PAGE_SIZE = 50;
  var queryInput = document.getElementById("search-query");
  var direction = document.getElementById("search-direction");
  var priority = document.getElementById("search-priority");
  var relevance = document.getElementById("search-relevance");
  var year = document.getElementById("search-year");
  var deepToggle = document.getElementById("search-deep-toggle");
  var deepYear = document.getElementById("search-deep-year");
  var status = document.getElementById("search-status");
  var results = document.getElementById("search-results");
  var more = document.getElementById("search-more");
  if (!queryInput || !results || !window.Worker) return;

  var worker = new Worker("radar-search-worker.js" + assetQuery);
  var manifest = null;
  var loaded = 0;
  var failedYears = [];
  var loadedDeep = {};
  var requestId = 0;
  var limit = PAGE_SIZE;
  var timer = null;

  function element(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function currentFilters() {
    return {
      direction: direction.value,
      priority: priority.value,
      relevance: relevance.value,
      year: year.value
    };
  }

  function updateUrl() {
    var params = new URLSearchParams();
    if (queryInput.value.trim()) params.set("q", queryInput.value.trim());
    var filters = currentFilters();
    Object.keys(filters).forEach(function (key) {
      if (filters[key]) params.set(key, filters[key]);
    });
    if (deepToggle.checked) {
      params.set("deep", "1");
      if (deepYear.value) params.set("deepYear", deepYear.value);
    }
    var queryString = params.toString();
    history.replaceState(null, "", queryString ? "?" + queryString : "search.html");
  }

  function sendSearch() {
    if (!manifest || !loaded) return;
    if (deepToggle.checked && !deepYear.value) {
      status.textContent = "深度搜索需要先选择一个年份。";
      results.replaceChildren();
      more.hidden = true;
      return;
    }
    updateUrl();
    requestId += 1;
    worker.postMessage({
      type: "search", requestId: requestId,
      query: queryInput.value, filters: currentFilters(),
      deep: deepToggle.checked, limit: limit
    });
  }

  function scheduleSearch(resetLimit) {
    if (resetLimit) limit = PAGE_SIZE;
    clearTimeout(timer);
    timer = setTimeout(sendSearch, 180);
  }

  function renderResult(paper) {
    var card = element("article", "search-result");
    var title = element("a", "search-result__title", paper.title || "Untitled");
    var anchor = paper.anchor || String(paper.identity_key || "").replace(
      /[^A-Za-z0-9_-]/g, "-"
    );
    title.href = paper.date + ".html#" + anchor;
    card.appendChild(title);

    var metadata = element("div", "search-result__meta");
    [paper.date, paper.direction_name || paper.direction, paper.authors,
     paper.venue].filter(Boolean).forEach(function (value) {
      metadata.appendChild(element("span", "", value));
    });
    card.appendChild(metadata);

    var chips = element("div", "search-result__chips");
    if (paper.priority) chips.appendChild(element(
      "span", "priority priority--" + paper.priority.toLowerCase(), paper.priority
    ));
    if (paper.relevance_level) chips.appendChild(element(
      "span", "relevance-level lvl-" + paper.relevance_level.toLowerCase(),
      paper.relevance_level
    ));
    (paper.tags || []).slice(0, 4).forEach(function (tag) {
      chips.appendChild(element("span", "tag", tag));
    });
    if (paper.term) chips.appendChild(element("span", "term", paper.term));
    card.appendChild(chips);
    return card;
  }

  worker.onmessage = function (event) {
    var message = event.data || {};
    if (message.type !== "results" || message.requestId !== requestId) return;
    var fragment = document.createDocumentFragment();
    (message.results || []).forEach(function (paper) {
      fragment.appendChild(renderResult(paper));
    });
    results.replaceChildren(fragment);
    var failure = failedYears.length ? "；失败年份：" + failedYears.join("、") : "";
    status.textContent = "已载入 " + loaded + " / " + manifest.total +
      " 条轻量记录；找到 " + message.total + " 条结果" + failure;
    more.hidden = message.results.length >= message.total;
  };
  worker.onerror = function () {
    status.textContent = "搜索 Worker 启动失败，请刷新页面重试。";
  };

  function loadDeepYear(selectedYear) {
    if (!selectedYear || loadedDeep[selectedYear]) {
      scheduleSearch(true);
      return;
    }
    status.textContent = "正在载入 " + selectedYear + " 年深度索引…";
    fetch("search-deep-" + selectedYear + ".json")
      .then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json();
      })
      .then(function (records) {
        worker.postMessage({ type: "appendDeep", records: records });
        loadedDeep[selectedYear] = true;
        scheduleSearch(true);
      })
      .catch(function (error) {
        status.textContent = selectedYear + " 年深度索引加载失败：" + error.message;
      });
  }

  function bindControls() {
    queryInput.addEventListener("input", function () { scheduleSearch(true); });
    [direction, priority, relevance, year].forEach(function (control) {
      control.addEventListener("change", function () { scheduleSearch(true); });
    });
    deepToggle.addEventListener("change", function () {
      if (deepToggle.checked) {
        if (!deepYear.value) {
          status.textContent = "请选择要加载的深搜年份。";
          return;
        }
        year.value = deepYear.value;
        loadDeepYear(deepYear.value);
      } else {
        scheduleSearch(true);
      }
    });
    deepYear.addEventListener("change", function () {
      if (!deepToggle.checked || !deepYear.value) return;
      year.value = deepYear.value;
      loadDeepYear(deepYear.value);
    });
    more.addEventListener("click", function () {
      limit += PAGE_SIZE;
      sendSearch();
    });
  }

  function restoreUrlState() {
    var params = new URLSearchParams(window.location.search);
    queryInput.value = params.get("q") || "";
    direction.value = params.get("direction") || "";
    priority.value = params.get("priority") || "";
    relevance.value = params.get("relevance") || "";
    year.value = params.get("year") || "";
    deepToggle.checked = params.get("deep") === "1";
    deepYear.value = params.get("deepYear") || "";
  }

  function loadMetadataYears(years, index) {
    if (index >= years.length) return;
    var selectedYear = years[index];
    fetch("search-index-" + selectedYear + ".json")
      .then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json();
      })
      .then(function (records) {
        worker.postMessage({ type: "append", records: records });
        loaded += records.length;
        scheduleSearch(false);
      })
      .catch(function () { failedYears.push(selectedYear); })
      .then(function () { loadMetadataYears(years, index + 1); });
  }

  fetch("search-index-manifest.json")
    .then(function (response) {
      if (!response.ok) throw new Error("HTTP " + response.status);
      return response.json();
    })
    .then(function (data) {
      manifest = data;
      data.years.forEach(function (value) {
        var option = element("option", "", value);
        option.value = value;
        year.appendChild(option);
        deepYear.appendChild(option.cloneNode(true));
      });
      restoreUrlState();
      bindControls();
      loadMetadataYears(data.years, 0);
      if (deepToggle.checked && deepYear.value) loadDeepYear(deepYear.value);
    })
    .catch(function (error) {
      status.textContent = "搜索 manifest 加载失败：" + error.message;
    });
})();
