#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const appJs = fs.readFileSync(path.resolve(__dirname, "../frontend/app.js"), "utf8");

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

const pattern = appJs.match(/const _ORGANISER_FOLDER = .*;/)[0];
const context = {};
vm.createContext(context);
vm.runInContext(`${pattern}\n${extractFunction("_organiserFolderProblem")}\nthis.check = _organiserFolderProblem;`, context);
const problem = (paths) => context.check(paths.map((p) => ({ webkitRelativePath: p })));

console.log("\nOrganiser folders are not uploaded as a submission");
check("the whole test-data folder is refused",
  /hidden_ground_truth, shared_masks/.test(problem([
    "DCE_test_data/submission/P01/site_1/scan_1/Ktrans.nii.gz",
    "DCE_test_data/hidden_ground_truth/P01/site_1/scan_1/Ktrans.nii.gz",
    "DCE_test_data/shared_masks/site_1/GM_mask.nii.gz",
  ])));
check("the ground-truth folder itself is refused",
  /organiser reference data/.test(problem(["hidden_ground_truth/P01/site_1/scan_1/Ktrans.nii.gz"])));
check("the masks folder itself is refused",
  /organiser reference data/.test(problem(["shared_masks/site_1/GM_mask.nii.gz"])));
check("a submission folder is accepted",
  problem(["submission/P01/site_1/scan_1/Ktrans.nii.gz", "submission/methods.txt"]) === "");
check("a team folder with its own masks folder is accepted",
  problem(["Team_A/masks/brain.nii.gz", "Team_A/P01/Ktrans.nii.gz"]) === "");

console.log(`\n=== Results: ${passed} passed, ${failed} failed ===\n`);
process.exit(failed ? 1 : 0);
