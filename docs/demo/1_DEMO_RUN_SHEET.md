# Mentor demo — run sheet (live screen-share)

Total time: ~8 min core + ~4 min bonus. Lead with **one ASL submission**. Keep it
calm; the pipeline does the talking.

## 0. Before the call (5 min, do this once)

```bash
cd /Users/ralkhleef/Desktop/osipi-perfusion-pipeline
docker compose up --build -d        # wait for the container to be healthy
docker compose ps                   # STATUS should show "healthy"/"running"
curl http://localhost:8000/api/health   # -> {"status":"ok"}
```

Then in Chrome open **http://localhost:8000** and open DevTools → Console (to show
"no red errors"). Have these files ready in a Finder window as a fallback in case
Docker acts up:
`docs/demo/`, and the pre-generated report samples if you kept them.

Have this ZIP ready to drag in: a **Lena ASL ZIP** (Perfmap + ATTmap), e.g.
`submissions/incoming/lena_01_exact_single_submission.zip`.

One-line framing to open with:
> "This is a QC and reference-comparison pipeline for the OSIPI challenges. It's
> deliberately not producing an official score yet — it does the safe, correct
> groundwork so your team can drop in the official scoring when it's defined."

---

## 1. Upload & Detect  (~1 min)

- Drag the Lena ASL ZIP onto the upload area → **Upload and Continue**.
- **Point out:** it detected **one ASL submission**, and that **Perfmap → CBF**
  and **ATTmap → ATT** were recognized automatically.

Say: *"Detection is filename + config driven — no hardcoding."*

## 2. Review  (~30 sec)

- Show the detected submission row: challenge = ASL, maps = CBF + ATT, result-only.
- Click **Validate Submission**.

## 3. Validate  (~1 min)

- **Point out:** it **passed** with a warning, not an error — the warning is
  "result maps only, add a Dockerfile to enable reproducible execution."
- Say: *"Warnings never block; only real errors do."*
- Click **Continue to Run**.

## 4. Run  (~30 sec)

- **Point out:** it says **"Execution not required"** — this is a result-only
  submission, so there's nothing to run. Click **Continue to Score**.

## 5. QC & Preview  — the heart of the demo  (~3 min)

This is where the scientific-correctness work shows.

- **Title reads "QC & Preview"**, subtitle "Quality checks and generic reference
  comparisons." Point at the tooltip: *"these are not official OSIPI scores."*
- **Parameter Map Previews:** exactly **two cards — CBF and ATT**. Note there is
  **no "Unknown" card**; the 4-D ASL input is tucked under "Submitted files (not
  scored as parameter maps)" labeled **"4D ASL data."**
- Open a preview (click a card) to show the axial/coronal/sagittal slices.

> **Reference data is now installed** (Lena's Perfmap/ATTmap live in
> `data/reference_data/maps/`; her 4-D ASL ground truth is set aside in
> `data/reference_data/asl_ground_truth_4d/` for the future fitted-model
> comparison). So the reference metrics table **will appear**. Two things to know:
>
> - **Which file to upload:** use `demo_participant_asl_submission.zip` — it's
>   Lena's maps with ~8% noise added (a stand-in participant), so you get
>   **realistic non-zero RMSE/bias** per CBF and ATT. If you instead upload
>   `lena_01_...zip`, its maps *are* Lena's originals, so every error is exactly
>   **0.0** — still a clean story ("submitted equals reference → zero error,
>   perfect correlation → proves the comparison is real"), but flatter.
> - **It takes ~30–40s** to compute at full resolution (8.6M voxels, per map).
>   That's expected. Click into QC & Preview and **narrate the CBF/ATT/CoV
>   safeguards below while it computes** — it fills the pause perfectly. Do the
>   dry run first so you've seen the timing.
> - **Want it snappy instead?** Temporarily move `data/reference_data/maps` aside
>   to fall back to the instant QC-only demo (reference shows "not available").
> - **Masks:** none yet — Lena will send grey/white/tumour/ROI masks, so today is
>   whole-image only. ROI rows appear once those masks are in
>   `data/reference_data/masks/`.

- **Reference metrics table** — the key slide:
  - CBF and ATT are in **separate rows** (say: *"different units — mL/100g/min vs
    seconds — so we never average them"*).
  - Columns: RMSE, MAE, Bias, **Error CoV**, Correlation — per map and per ROI.
  - Point at the note: **"Repeatability CoV and ICC are unavailable until repeated
    noise-varied datasets are provided."**
- If masks are present, show whole-image vs ROI rows (gray matter, etc.).
- Show the **difference map** is generated (submitted − reference), and mention it
  preserves the original affine so it lands correctly in FSLeyes/ITK-SNAP.

Say: *"Missing values show as 'Not available', never zero — a missing reference is
not a score of zero."*

## 6. Export  (~1 min)

- Click **Continue to Export**. Download and open, quickly:
  - **PDF** — per-map CBF/ATT sections, repeatability-unavailable note, no
    official-score claim.
  - **HTML** — same content, opens offline, no internet needed.
  - **CSV (blinded)** — open it: **long format**, one row per map × ROI × metric;
    no team/name/email/paths. Say: *"tidy format — easy in Python/R/Excel."*
- Click **Start New** to show the session clears cleanly.

---

## Bonus (only if time / they ask)  (~4 min)

- **Missing ATT:** upload the missing-ATT ZIP → validate → show it's flagged as a
  **warning** ("ATT expected map missing"), not a fake zero, and Continue stays enabled.
- **Mixed ASL + DCE:** select two ZIPs at once (multi-select in the file picker) →
  show each is detected as its own challenge, the Review list is **grouped by
  challenge**, and the report says **"no cross-challenge totals are computed."**
- **Extensibility:** open `docs/ADDING_SCORING_METRICS.md` — *"here's exactly how
  your team adds an official metric later."*

## If Docker misbehaves (fallback)

- Don't debug live. Say: *"Let me show the generated outputs directly"* and open a
  pre-saved `report.html` / `report.pdf` / long CSV from Finder.
- Everything below the browser is covered by the automated tests (213 Python, 964
  frontend) — you can show a green `pytest -q` run if needed.

## Close

> "Next, once you give me the official scoring definitions, masks, and any repeated
> datasets, I plug them into the same structure — the pipeline is already shaped
> for it."
