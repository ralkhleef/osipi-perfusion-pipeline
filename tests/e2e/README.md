# Live browser acceptance (Playwright)

`acceptance.spec.js` drives the real UI in a real browser. It must run on a
machine with Docker and a display/headless Chromium — not in the CI sandbox.

## Run

```bash
# 1. bring the stack up (from repo root)
docker compose up --build            # serves http://localhost:8000

# 2. in a second shell, install Playwright once
npm i -D @playwright/test
npx playwright install chromium

# 3. run the acceptance spec
BASE_URL=http://localhost:8000 npx playwright test tests/e2e/acceptance.spec.js --reporter=list
```

Screenshots for each step are written to `tests/e2e/screenshots/`.

## What it covers

- App startup + `/api/config`
- Lena single: upload → detect (ASL) → validate → QC & Preview → export
- Result-only shows "execution not required"
- QC title/subtitle + "not official OSIPI scores" disclaimer
- Missing required ATT is a blocking validation error (Continue stays disabled)
- Batch of three stays isolated
- Start New clears the session
- Narrow (390px) viewport keeps the primary action visible
- Refresh after validate restores state

## Notes

Selectors are text/role based so they survive DOM tweaks. If a locator misses
against your build, adjust the locator to match the running app — do not loosen
the assertion, or the check stops being meaningful.

The Lena ZIPs used here (`lena_01`..`lena_05`) live in `submissions/incoming/`.
`lena_02`–`lena_05` were generated from `lena_01`'s float NIfTIs for the
no-wrapper / missing-ATT / batch / baseline cases.
