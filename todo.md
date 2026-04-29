# Project Review: copilotstudio-client-py

## Issues & Suggestions

- [ ] **`.env` in git history** — `.env` was committed in the initial commit. `.gitignore` excludes it, but it's already in history. If it contains real credentials, clean history with `git filter-repo`.

- [ ] **Duplicate client setup in `evaluate.py`** — `evaluate.py:167-173` creates `ConnectionSettings` and `CopilotClient` inline instead of reusing `create_copilot_client()` from `chat.py`. Could drift over time.

- [ ] **`partial` match is O(n*m)** — `evaluate.py:69-71` slides character-by-character over the response running `SequenceMatcher` at each offset. Fine at eval-runner scale but could be slow for long responses.

- [x] **No timeout/retry on SDK calls** — Added `asyncio.wait_for()` with a configurable `TIMEOUT_SECONDS` (default 120s) on both `start_conversation` and `ask_question` calls. Timeouts are reported as `[TIMEOUT]` in the eval report.

- [x] **Single conversation for all evals** — Each prompt now gets a fresh conversation by default. Multi-turn flows are supported via the optional `conversation_id` CSV column.

- [ ] **Inconsistent activity type checks** — `chat.py:69` uses `"typing"` and `"event"` as string literals while other checks use `ActivityTypes.message` / `ActivityTypes.end_of_conversation`. Align if the SDK exposes those constants.

- [ ] **No type-checking config** — Code uses type hints throughout but there's no mypy/pyright config. Low priority for a small project.

## Backlog (inspired by @microsoft/m365-copilot-eval)

- [x] **HTML report generator** — Self-contained HTML report with sortable rows, status badges, search/filter, and aggregate stats. Auto-opens in browser after eval completes; suppress with `--no-open`. Written alongside the CSV for every run.
- [x] **Parallel execution** — Conversation groups run concurrently via `asyncio.gather()` with `asyncio.Semaphore` for concurrency control. New `--concurrency N` (`-c N`) CLI flag, default 1. Multi-turn cases within a group remain sequential to preserve ordering.
- [ ] **Interactive / inline prompt mode** — Add `--prompt "..."` and `--interactive` flags to `evaluate.py` for one-off testing without a CSV file.
- [ ] **Auto-discover prompts file** — When run with no args, look for `sample_eval.csv`, `evals.csv`, `tests.csv` in CWD before erroring out.
- [ ] **Optional JSON input format** — Allow JSON input as an alternative to CSV for power users (per-prompt evaluator overrides, default_evaluators block, schema validation).
- [ ] **Evaluator name aliases** — Accept `relevance`, `coherence`, `groundedness` as aliases for our LLM judge methods, matching Microsoft's naming convention.
