# Publication validation — 2026-11-06

Checked on the isolated publication copy:

- Core existing test suite: **220 passed** (`tests/test_schemas.py`, `tests/test_scheduler.py`, `tests/test_validator.py`, `tests/test_library.py`).
- `python -m ruff check .`: passed with the project's existing configuration.
- Frontend installation used the supplied lock file (`npm ci --ignore-scripts --no-audit --no-fund`). `npm run build` passed TypeScript checking and the Vite production build. Vite reported a bundle-size warning for a minified chunk over 500 kB.

The full default test suite was started but not completed during this upload; browser and live-model integration tests were not rerun. Historical counts in the Chinese development notes describe earlier development runs, not this publication check.

This copy excludes `.env`, local credentials/cookies, trip databases, attachments, generated city caches, tile caches, local assistant configuration, build output, and Python/Node environments. Source application files were copied without functional edits. The original development directory and its existing Git history were not changed.
