# Project Review: copilotstudio-client-py

## Issues & Suggestions

- [ ] **`.env` in git history** — `.env` was committed in the initial commit. `.gitignore` excludes it, but it's already in history. If it contains real credentials, clean history with `git filter-repo`.

- [ ] **Duplicate client setup in `evaluate.py`** — `evaluate.py:167-173` creates `ConnectionSettings` and `CopilotClient` inline instead of reusing `create_copilot_client()` from `chat.py`. Could drift over time.

- [ ] **`partial` match is O(n*m)** — `evaluate.py:69-71` slides character-by-character over the response running `SequenceMatcher` at each offset. Fine at eval-runner scale but could be slow for long responses.

- [x] **No timeout/retry on SDK calls** — Added `asyncio.wait_for()` with a configurable `TIMEOUT_SECONDS` (default 120s) on both `start_conversation` and `ask_question` calls. Timeouts are reported as `[TIMEOUT]` in the eval report.

- [x] **Single conversation for all evals** — Each prompt now gets a fresh conversation by default. Multi-turn flows are supported via the optional `conversation_id` CSV column.

- [ ] **Inconsistent activity type checks** — `chat.py:69` uses `"typing"` and `"event"` as string literals while other checks use `ActivityTypes.message` / `ActivityTypes.end_of_conversation`. Align if the SDK exposes those constants.

- [ ] **No type-checking config** — Code uses type hints throughout but there's no mypy/pyright config. Low priority for a small project.
