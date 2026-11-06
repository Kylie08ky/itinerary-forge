# Validation — v0.6.1 (2026-11-06)

v0.6.1 fixes a timing race in the budget-page browser tests and corrects the wording about how many cities are fully curated in two specs; the application code is the same as v0.5.0. The Docker image was not rebuilt (see docs/self-hosting.md).

What was actually run before this release:

- Unit tests (`python -m pytest -q`): **2015 passed**. Integration and browser tests are excluded by default.
- Browser tests (`python -m pytest -m e2e tests/e2e`, Playwright against an isolated backend, frontend, and temporary database): one full run before the fix gave **469 passed, 3 skipped, 1 failed**. The failure was a budget-page test that filled the form before the card had finished its second data load, which resets the form. The tests now wait for the card and for the network to settle; the budget file (29 tests) then passed in two consecutive runs. The full suite was not re-run after the fix. Other timing-sensitive tests can still fail in long full runs, and which ones varies.
- `python -m ruff check .`: passed.
- Frontend: `npm run build` passes TypeScript checking and the Vite production build (Vite warns about a chunk over 500 kB).
- Pre-release checks: no API keys, cookies, or personal paths in tracked files; `.env`, trip databases, attachments, generated city caches, tile caches, and local assistant configuration are not tracked.

Not covered by the automated runs: output quality of a live local model (Ollama), and real calls to Amap, Wikimedia, and other external services.
