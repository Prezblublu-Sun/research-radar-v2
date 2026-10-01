/* Filtering/sorting worker for the 100k-record metadata-first search. */
"use strict";

var INDEX = [];
var DEEP = new Map();
var PRIORITY_RANK = { High: 3, Medium: 2, Low: 1, Exclude: 0, "": 0 };

self.onmessage = function (event) {
  var message = event.data || {};
  if (message.type === "append") {
    var records = message.records || [];
    records.forEach(function (paper) {
      paper._search_blob = [
        paper.title, paper.authors, paper.venue, paper.direction_name, paper.term,
        (paper.tags || []).join(" ")
      ].filter(Boolean).join(" ").toLowerCase();
    });
    INDEX = INDEX.concat(records);
    return;
  }
  if (message.type === "appendDeep") {
    (message.records || []).forEach(function (record) {
      DEEP.set(record.identity_key, record.deep_blob || "");
    });
    return;
  }
  if (message.type !== "search") return;

  var query = String(message.query || "").trim().toLowerCase();
  var filters = message.filters || {};
  var useDeep = Boolean(message.deep);
  var matches = INDEX.filter(function (paper) {
    if (filters.direction && paper.direction !== filters.direction) return false;
    if (filters.priority && paper.priority !== filters.priority) return false;
    if (filters.relevance && paper.relevance_level !== filters.relevance) return false;
    if (filters.year && paper.date.slice(0, 4) !== filters.year) return false;
    if (!query) return true;
    var text = paper._search_blob || "";
    if (useDeep) text += " " + (DEEP.get(paper.identity_key) || "");
    return text.indexOf(query) >= 0;
  });

  matches.sort(function (first, second) {
    return second.date.localeCompare(first.date) ||
      (PRIORITY_RANK[second.priority] - PRIORITY_RANK[first.priority]) ||
      first.title.localeCompare(second.title);
  });
  self.postMessage({
    type: "results",
    requestId: message.requestId,
    total: matches.length,
    results: matches.slice(0, message.limit || 50)
  });
};
