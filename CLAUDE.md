# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Python client for chatting with Microsoft Copilot Studio agents using the M365 Agents SDK ("Direct-to-Engine" protocol). Includes an interactive chat mode and a CSV-driven prompt evaluation runner.

## Commands

```bash
# Install dependencies
pip install -e .

# Interactive chat with the agent
python chat.py

# Run prompt evaluations from CSV
python evaluate.py sample_eval.csv              # results saved to results/eval_<timestamp>.csv
python evaluate.py input.csv output.csv         # explicit output path
```

## Architecture

- **config.py** - `AgentSettings` dataclass loaded from `.env`; supports both interactive (browser) and S2S (client credentials) auth modes via `AUTH_MODE`
- **chat.py** - Interactive console chat loop. Authenticates via MSAL, creates a `CopilotClient` (from `microsoft-agents-copilotstudio-client`), calls `start_conversation()` then `ask_question()` in a loop. The `acquire_token()` and `create_copilot_client()` functions are reused by `evaluate.py`.
- **evaluate.py** - Reads a CSV of `(prompt, expected_response, match_method)`, runs each prompt against the agent in a single conversation, checks the response, and outputs a pass/fail report + results CSV.

## Key SDK Details

- The Python SDK package is `microsoft-agents-copilotstudio-client`. Import path uses underscores: `microsoft_agents.copilotstudio.client`.
- `CopilotClient` takes `ConnectionSettings` + an access token string. It uses SSE streaming internally.
- `start_conversation()` returns an async generator of activities (greeting). `ask_question()` returns an async generator per question.
- Activity types come from `microsoft_agents.activity.ActivityTypes` (`message`, `typing`, `end_of_conversation`).
- Auth scope for Power Platform: `https://api.powerplatform.com/.default`

## Configuration

All settings in `.env`:
- `COPILOTSTUDIO_ENVIRONMENT_ID`, `COPILOTSTUDIO_SCHEMA_NAME`, `COPILOTSTUDIO_TENANT_ID` - agent identity
- `COPILOTSTUDIO_APP_CLIENT_ID`, `COPILOTSTUDIO_APP_CLIENT_SECRET` - Azure AD app registration
- `AUTH_MODE` - `interactive` (browser popup) or `s2s` (client credentials, requires secret). **Note:** S2S is not yet officially supported by the Copilot Studio Direct-to-Engine API — the backend requires a user-context token. S2S code is included for future readiness. Use `interactive` for now.

## Evaluation CSV Format

```csv
prompt,expected_response,match_method,conversation_id,attachment,skip
"Hello","hi",contains,,,
"What is 2+2?","4",exact,,,
"Tell me about X","topic|subject",regex,,,
"Reset my password","credit card",not_contains,,,
"Describe benefits","health insurance",fuzzy,,,
"Explain leave policy","parental leave|80",partial,,,
"Hi","hello",contains,benefits_flow,,
"Tell me about dental","80%",contains,benefits_flow,,
"Summarize this","key points",contains,,report.pdf,
"Describe image","chart",contains,,https://example.com/chart.png,true
```

Each row runs in a **fresh conversation** by default. To test multi-turn flows, give rows the same `conversation_id` — they'll share one conversation and run in CSV order.

The optional `attachment` column accepts a URL (`https://...`) or a local file path. Local files are base64-encoded into data URIs for inline transport. The SDK sends attachments via `ask_question_with_activity()`.

Set the optional `skip` column to `true`, `yes`, or `1` to skip a row without removing it from the CSV.

Match methods: `exact`, `contains`, `not_contains`, `regex`, `fuzzy`, `partial`

For `fuzzy` and `partial`, append `|threshold` to set a custom pass threshold (default 70%). Uses `difflib.SequenceMatcher` — no extra dependencies.
