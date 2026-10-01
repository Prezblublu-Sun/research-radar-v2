/* Workbench: hydrate each run section from data/run/<run_id>.json.
 *
 * The shell lists the last runs; the cards arrive here so the root page
 * stays a few kilobytes however many papers a run routed. Filtering and
 * the all-hidden notice are owned by radar-ui.js: this file only listens.
 */
(function () {
  "use strict";

  var sections = document.querySelectorAll("section.run-section[data-run-id]");
  if (!sections.length) return;
  if (!window.RadarCard || !window.RadarCard.buildCard) {
    sections.forEach(function (section) {
      var status = section.querySelector("[data-run-status]");
      if (status) status.textContent = "卡片组件加载失败，请刷新页面重试。";
    });
    return;
  }

  function element(tag, className, value) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (value != null) node.textContent = String(value);
    return node;
  }

  function grid(records) {
    var box = element("div", "paper-grid");
    records.forEach(function (record) {
      box.appendChild(window.RadarCard.buildCard(record));
    });
    return box;
  }

  function fold(label, records, open) {
    var details = element("details", "workbench-lower");
    if (open) details.open = true;
    details.appendChild(element("summary", "", label + "（" + records.length + " 篇）"));
    details.appendChild(grid(records));
    return details;
  }

  function hydrate(section) {
    if (window.RadarUI && window.RadarUI.hydrate) {
      window.RadarUI.hydrate(section);
    } else {
      document.dispatchEvent(new CustomEvent("radar:content-ready", {
        detail: { root: section }
      }));
    }
  }

  function load(section) {
    var runId = String(section.dataset.runId || "");
    var host = section.querySelector("[data-run-cards]");
    var status = section.querySelector("[data-run-status]");
    if (!runId || !host || !status) return;
    status.textContent = "正在加载本次运行的论文…";
    fetch("data/run/" + encodeURIComponent(runId) + ".json", { cache: "no-store" })
      .then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json();
      })
      .then(function (payload) {
        var records = (payload && Array.isArray(payload.records)) ? payload.records : [];
        var actionable = [], unscored = [], lower = [];
        records.forEach(function (record) {
          var priority = String(record.priority || "");
          if (priority === "High" || priority === "Medium") actionable.push(record);
          else if (priority === "Unscored") unscored.push(record);
          else lower.push(record);
        });
        var fragment = document.createDocumentFragment();
        if (actionable.length) fragment.appendChild(grid(actionable));
        else fragment.appendChild(element("p", "empty-state", "本次运行没有 High 或 Medium 论文。"));
        if (unscored.length) fragment.appendChild(fold("待评分", unscored, true));
        if (lower.length) fragment.appendChild(fold("查看 Low / Exclude", lower, false));
        host.replaceChildren(fragment);
        status.textContent = "";
        status.hidden = true;
        hydrate(section);
      })
      .catch(function (error) {
        status.hidden = false;
        status.textContent = "本次运行的论文加载失败：" + error.message;
        var retry = element("button", "queue-page-button", "重试");
        retry.type = "button";
        retry.addEventListener("click", function () { load(section); });
        status.appendChild(document.createTextNode(" "));
        status.appendChild(retry);
      });
  }

  sections.forEach(load);
})();
