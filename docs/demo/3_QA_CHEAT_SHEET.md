# Q&A cheat sheet — mentor meeting

Short, correct answers you can lean on. The theme in every answer: **correct and
safe now; official scoring plugs in when you define it.**

**Q: Did you average CBF and ATT together?**
No. They have different units (mL/100g/min vs seconds), so they're kept in
separate rows and separate summaries everywhere — tables, PDF, HTML, and CSV.
There is no combined RMSE/MAE/Bias across map types.

**Q: Is that coefficient of variation a repeatability measure?**
No. What's shown is an **Error CoV** (spread of voxelwise errors ÷ reference mean)
and, separately, a **Spatial CoV** (map variability). Repeatability CoV needs
repeated noise-varied datasets, which we don't have yet — so the report explicitly
says repeatability CoV and ICC are **unavailable** until you provide those.

**Q: Where's the ICC / overall score / ranking?**
Deliberately not built yet. Those are scientific decisions for your team — the
ICC model, the weighting, whether reproducibility outranks accuracy. Nothing is
invented. When you define them, they drop into the same structure. Today it shows
per-metric results and comparisons, no pass/fail, no leaderboard.

**Q: What happens if the submitted map and reference aren't aligned?**
It checks shape, affine, and voxel size first. If they don't match it returns
`spatial_grid_mismatch` and skips voxelwise scoring rather than producing
misleading numbers — no silent resampling. (I verified two same-shape maps 100 mm
apart are refused, not scored as if identical.)

**Q: How do you handle the 4-D ASL / model file?**
Parameter maps (Perfmap, ATTmap) must be exactly 3-D. A 4-D file is treated as
"4D ASL data" — kept available for download but not scored as a parameter map, and
not shown as a preview card. Scoring it against a 4-D ground-truth series is ready
to add once you provide that reference.

**Q: Does missing data show up as zero?**
Never. Missing metrics are blank / "Not available", and a missing reference is not
treated as a score of zero. No pass/fail threshold is applied to results.

**Q: Does it handle DCE and DSC, or just ASL?**
It's config-driven for all three, and a mixed batch is kept **completely separate
by challenge** — no cross-challenge totals or averages. ASL is the most complete
because that's the data we have; DCE/DSC just need their reference files, masks,
and metric definitions from your teams.

**Q: Can you upload more than one submission at once?**
Yes — a batch ZIP, or several ZIPs at once (including different challenges). Each
submission is detected, tagged, and validated under its own challenge.

**Q: How will we add our official metric/score later?**
There's a written guide (`docs/ADDING_SCORING_METRICS.md`): add the metric
function, register it in config, choose which maps/ROIs use it, expose it in the
reports, and add a test. Roughly a day's work per metric, no core rewrite.

**Q: How do I know it actually works?**
213 automated Python tests and 964 frontend checks, all green, plus a full
end-to-end run across six submission types (valid ASL, missing ATT, batch, mixed
ASL/DCE, corrupt NIfTI, reproducible). Corrupt files block progression; valid ones
flow through to export.

**Q: What do you need from us next?**
Priority order: (1) official ASL scoring definition + masks, (2) repeated
noise-varied datasets for repeatability/ICC, (3) DCE/DSC files and metrics from
Olivia, (4) the ground-truth 4-D ASL series.

---

### If you get stuck / don't know an answer
Say: *"Good question — that's one where I'd want your definition rather than
guessing, so I've left it as a configurable placeholder."* That's the honest,
correct answer for anything scoring-formula related.
