/* Random reading: hydrate each day's journal grids from data/random/<run_id>.json.
 *
 * The page carries the mark filter bar, so radar-ui.js filters these cards
 * and draws the all-hidden notice itself; this file only builds the cards.
 */
(function () {
  "use strict";

  var days = document.querySelectorAll("section.random-day[data-run-id]");
  if (!days.length) return;
  if (!window.RadarCard || !window.RadarCard.buildCard) {
    days.forEach(function (day) {
      var status = day.querySelector("[data-random-status]");
      if (status) status.textContent = "卡片组件加载失败，请刷新页面重试。";
    });
    return;
  }

  function hydrate(root) {
    if (window.RadarUI && window.RadarUI.hydrate) {
      window.RadarUI.hydrate(root);
    } else {
      document.dispatchEvent(new CustomEvent("radar:content-ready", {
        detail: { root: root }
      }));
    }
  }

  function load(day) {
    var runId = String(day.dataset.runId || "");
    var status = day.querySelector("[data-random-status]");
    if (!runId) return;
    fetch("data/random/" + encodeURIComponent(runId) + ".json", { cache: "no-store" })
      .then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json();
      })
      .then(function (payload) {
        var records = (payload && Array.isArray(payload.records)) ? payload.records : [];
        var grids = {};
        day.querySelectorAll("section.random-journal[data-journal-id]").forEach(function (journal) {
          var host = journal.querySelector("[data-journal-cards]");
          if (host) grids[String(journal.dataset.journalId || "")] = host;
        });
        var placed = {};
        records.forEach(function (record) {
          var venue = String((record.random_reading || {}).venue_id || "");
          var host = grids[venue];
          if (!host) return;
          if (!placed[venue]) { host.replaceChildren(); placed[venue] = true; }
          host.appendChild(window.RadarCard.buildCard(record));
        });
        if (status) status.textContent = "";
        hydrate(day);
      })
      .catch(function (error) {
        if (status) status.textContent = "随机阅读数据加载失败：" + error.message;
      });
  }

  days.forEach(load);
})();
