# Browser acceptance test

`acceptance.spec.js` runs the main workflow in Chromium. It needs Docker and is
not part of the sandboxed CI tests.

```bash
docker compose up --build
npm i -D @playwright/test
npx playwright install chromium
BASE_URL=http://localhost:8000 npx playwright test tests/e2e/acceptance.spec.js --reporter=list
```

It checks startup, upload, validation, result-map handling, QC and export,
blocking validation errors, batch isolation, session reset, mobile layout, and
state restoration after refresh.

Screenshots are written to `tests/e2e/screenshots/`. The test ZIP files are in
`submissions/incoming/`.
