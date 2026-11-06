# Validation — v0.5.0 (2026-11-06)

What was actually run before this release:

- Unit tests (`python -m pytest -q`): **2014 passed**. Integration and browser tests are excluded by default.
- Browser tests (`python -m pytest -m e2e tests/e2e`, Playwright against an isolated backend, frontend, and temporary database): **468 passed, 3 skipped, 2 failed** in one full run. Both failures were budget-page assertions where the UI had not refreshed within 5 s; the whole budget file (29 tests) passes when run alone. A few timing-sensitive tests can fail in long full runs, and which ones varies.
- `python -m ruff check .`: passed.
- Frontend: `npm run build` passes TypeScript checking and the Vite production build (Vite warns about a chunk over 500 kB).
- Pre-release checks: no API keys, cookies, or personal paths in tracked files; `.env`, trip databases, attachments, generated city caches, tile caches, and local assistant configuration are not tracked.

Not covered by the automated runs: output quality of a live local model (Ollama), and real calls to Amap, Wikimedia, and other external services.
