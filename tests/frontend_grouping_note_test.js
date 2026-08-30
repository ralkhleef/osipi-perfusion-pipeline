#!/usr/bin/env node
/**
 * Showing, and correcting, how an upload was grouped into submissions.
 *
 * A ZIP containing P01 through P10 is either one submission covering ten
 * participants or ten separate submissions. Nothing in the files distinguishes
 * them, so detection guesses, and both ways of being wrong are bad quietly:
 * split a single submission and every file loses the participant level that
 * identified it; merge a real batch and several teams are scored as one.
 *
 * `_groupingModel` decides what the review step offers. It is pure, so the
 * wording and the offered direction are pinned here directly. The case that
 * matters most is the one where nothing should be offered at all: a submission
 * with no inner folders cannot be split, and showing a button that would fail
 * is worse than showing nothing.
 *
 * Run: node tests/frontend_grouping_note_test.js
 */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const appJs = fs.readFileSync(path.resolve(__dirname, "../frontend/app.js"), "utf8");
const indexHtml = fs.readFileSync(path.resolve(__dirname, "../frontend/index.html"), "utf8");

let passed = 0;
let failed = 0;

function check(desc, cond, extra = "") {
  if (cond) { console.log(`  OK  ${desc}`); passed++; }
  else { console.error(`  FAIL  ${desc}${extra ? ` — ${extra}` : ""}`); failed++; }
}

function extractFunction(name) {
  const start = appJs.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`function not found: ${name}`);
  let depth = 0, seen = false;
  for (let i = start; i < appJs.length; i += 1) {
    if (appJs[i] === "{") { depth += 1; seen = true; }
    else if (appJs[i] === "}") { depth -= 1; if (seen && depth === 0) return appJs.slice(start, i + 1); }
  }
  throw new Error(`unbalanced braces in ${name}`);
}

const sandbox = { console };
vm.createContext(sandbox);
vm.runInContext(
  appJs.match(/const GROUPING_FOLDER_LIMIT = \d+;/)[0], sandbox);
vm.runInContext(extractFunction("_groupingModel"), sandbox);
vm.runInContext(extractFunction("_groupingFolderText"), sandbox);

const one = (folders) => [{ submission_id: "upload", inner_folders: folders }];
const many = (n) => Array.from({ length: n }, (_, i) => ({
  submission_id: `upload_Team_${i}`, source_folder: `Team_${i}`,
}));

console.log("\nOne submission with inner folders offers a split");
{
  const m = sandbox._groupingModel(one(["P01", "P02", "P03"]));
  check("a model is offered", !!m);
  check("the direction is split", m.mode === "split", m.mode);
  check("it says how many it would become",
    /3 separate submissions/.test(m.action), m.action);
  check("it says what was read", /1 submission/.test(m.text), m.text);
  check("it carries the id to act on",
    JSON.stringify(m.ids) === JSON.stringify(["upload"]), JSON.stringify(m.ids));
  check("it shows the folder names so a person can judge",
    m.folders.join(",") === "P01,P02,P03", m.folders.join(","));
}

console.log("\nSeveral submissions offer a merge");
{
  const m = sandbox._groupingModel(many(4));
  check("the direction is merge", m.mode === "merge", m.mode);
  check("it offers one submission", m.action === "Treat as 1 submission", m.action);
  check("it says how many were read", /4 separate submissions/.test(m.text), m.text);
  check("every id is carried", m.ids.length === 4, String(m.ids.length));
  check("it names the source folders rather than the ids",
    m.folders[0] === "Team_0", m.folders[0]);
}

console.log("\nNothing is offered when there is no decision to make");
/* A button that would fail is worse than no button. Splitting needs at least
   two inner folders; the backend refuses otherwise, so the interface must not
   invite it. */
check("a submission with no inner folders offers nothing",
  sandbox._groupingModel(one([])) === null);
check("a submission with one inner folder offers nothing",
  sandbox._groupingModel(one(["results"])) === null);
check("a missing inner_folders field offers nothing",
  sandbox._groupingModel([{ submission_id: "x" }]) === null);
check("an empty upload offers nothing",
  sandbox._groupingModel([]) === null);
check("a missing list offers nothing",
  sandbox._groupingModel(undefined) === null);

console.log("\nThe folder list stays readable");
{
  const ten = Array.from({ length: 10 }, (_, i) => `P${String(i + 1).padStart(2, "0")}`);
  const text = sandbox._groupingFolderText(ten);
  check("a long list is truncated with a count",
    /and 2 more$/.test(text), text);
  check("the first names are still shown", text.startsWith("P01, P02"), text);
  check("a short list is shown in full",
    sandbox._groupingFolderText(["P01", "P02"]) === "P01, P02");
  check("an empty list produces nothing",
    sandbox._groupingFolderText([]) === "");
  check("blank entries are dropped rather than shown as commas",
    sandbox._groupingFolderText(["P01", "", null, "P02"]) === "P01, P02",
    sandbox._groupingFolderText(["P01", "", null, "P02"]));
}

console.log("\nWiring");
check("the note is declared in the page, not built on demand",
  /id="grouping-note"/.test(indexHtml));
check("it starts hidden", /id="grouping-note"[^>]*\shidden/.test(indexHtml),
  "it would flash on load");
check("it is rendered whenever the review list is",
  /_renderGroupingNote\(submissions\);/.test(appJs));
check("the button posts to the regroup endpoint",
  /\/api\/regroup-submissions/.test(appJs));
check("the review list is rebuilt from the new grouping, not patched",
  /renderBatchTable\(submissions\);/.test(appJs),
  "stale submission ids would be left on screen");
check("a failure re-enables the button instead of leaving it stuck",
  /catch \(error\)[\s\S]{0,300}action\.disabled = false/.test(appJs));
check("the button is disabled while the request is in flight",
  /action\.disabled = true/.test(appJs), "double clicking would regroup twice");

console.log(`\n=== Results: ${passed} passed, ${failed} failed ===\n`);
process.exit(failed ? 1 : 0);
