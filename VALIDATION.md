# Validation — v0.7.0 (2026-11-06)

v0.7.0 restyles the public share page and the PDF / long-image / HTML exports to match the plan page (city palette, dark title band, stat cards; the PDF prints on plain white). The shared route now carries the ticket total. Everything below was run on this version. The Docker image was not rebuilt (see docs/self-hosting.md).

What was actually run before this release:

- Unit tests (`python -m pytest -q`): **2022 passed**. Integration and browser tests are excluded by default.
- Browser tests (`python -m pytest -m e2e tests/e2e`, Playwright against an isolated backend, frontend, and temporary database): **474 passed, 3 skipped, 0 failed** in one full run (about 23 minutes). The 3 skipped tests skip themselves; their reasons were not reviewed for this release. A few timing-sensitive tests have failed in earlier long runs, and which ones varies.
- `python -m ruff check .`: passed.
- Frontend: `npm run build` passes TypeScript checking and the Vite production build (Vite warns about a chunk over 500 kB).
- Pre-release checks: no API keys, cookies, or personal paths in tracked files; `.env`, trip databases, attachments, generated city caches, tile caches, and local assistant configuration are not tracked.

Not covered by the automated runs: output quality of a live local model (Ollama), and real calls to Amap, Wikimedia, and other external services.
