#!/usr/bin/env node
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(require("node:path").join(__dirname, "../frontend/app.js"), "utf8");
const context = vm.createContext({_fmtMetricVal: String, _scoreCache: {}, getSubmissionDisplayName: () => "Demo"});
for (const name of ["_scorePayload", "_analysisComplete", "_leaderboardReferenceStatus", "_leaderboardSummaryLine", "escapeHtml", "_iccRowsHtml", "renderIccStatistics", "_cacheScoreStatus"]) {
  const start = source.indexOf(`function ${name}(`);
  assert.ok(start >= 0);
  const end = source.indexOf("\n}", start) + 2;
  vm.runInContext(source.slice(start, end), context);
}
const qc = {status: "not_configured", nifti_analysis: {maps: [{stats: {mean: 1}}], errors: []}};
assert.equal(context._analysisComplete(qc), true);
assert.equal(context._analysisComplete({score_result: qc}), true);
assert.equal(context._analysisComplete({status: "not_configured"}), false);
assert.equal(context._analysisComplete({nifti_analysis: {maps: [], errors: []}}), false);
assert.equal(context._analysisComplete({nifti_analysis: {maps: [{error: "Unreadable"}], errors: []}}), false);
assert.equal(context._analysisComplete({status: "scored"}), true);
assert.match(context._leaderboardSummaryLine([{analysis_complete: true, status: "not_configured"}]), /1 analysis complete/);
const rows = [{model_description: "ICC(2,1): agreement", value: .8},
  {model_description: "ICC(3,1): consistency", value: .9},
  {model_description: "ICC(2,1): agreement", value: null, unavailable_reason: "Not enough scans <check>"}];
const html = context._iccRowsHtml([{reference_scoring: {icc_statistics: rows}}]);
assert.match(html, /ICC\(2,1\)/);
assert.match(html, /ICC\(3,1\)/);
assert.match(html, /<td>0.8<\/td>/);
assert.match(html, /<td>0.9<\/td>/);
assert.match(html, /Not enough scans &lt;check&gt;/);
assert.match(html, /<td>Not available<\/td>/);
const card = {style: {}}; const body = {innerHTML: "stale results"};
context.el = (id) => id === "icc-results-card" ? card : body;
context.renderIccStatistics([]);
assert.equal(body.innerHTML, "");
assert.equal(card.style.display, "none");
const fresh = {reference_scoring: {icc_models: ["icc2_1", "icc3_1"]}};
context._cacheScoreStatus("demo", {nifti_analysis: fresh,
  score_result: {nifti_analysis: {reference_scoring: {icc_models: []}}}}, null);
assert.equal(context._scoreCache.demo.niftiAnalysis, fresh);
console.log("=== Results: 16 passed, 0 failed ===");
