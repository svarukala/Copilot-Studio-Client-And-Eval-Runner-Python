# Copilot Studio Python Client

Python client for chatting with Microsoft Copilot Studio agents using the M365 Agents SDK ("Direct-to-Engine" protocol). Includes an interactive chat mode and a CSV-driven prompt evaluation runner.

## Prerequisites

- Python 3.10+
- An Azure AD app registration with a client ID
- A published Copilot Studio agent (you'll need the environment ID, schema name, and tenant ID)

## Setup

1. **Install dependencies:**

   ```bash
   pip install -e .
   ```

   Or install directly:

   ```bash
   pip install microsoft-agents-copilotstudio-client msal python-dotenv aiohttp
   ```

2. **Configure your `.env` file** with your agent's connection details:

   ```env
   COPILOTSTUDIO_ENVIRONMENT_ID=your-environment-id
   COPILOTSTUDIO_SCHEMA_NAME=your-agent-schema-name
   COPILOTSTUDIO_TENANT_ID=your-tenant-id
   COPILOTSTUDIO_APP_CLIENT_ID=your-app-client-id
   COPILOTSTUDIO_APP_CLIENT_SECRET=
   AUTH_MODE=interactive
   ```

   You can find the environment ID, schema name, and tenant ID in the Copilot Studio portal. Open your agent, under **Settings > Advanced > Metadata**.

## Usage

### Interactive Chat

```bash
python chat.py
```

A browser window will open for authentication (interactive mode). After login, you can chat with the agent in the terminal. Type `exit` or `quit` to end the session.

### Prompt Evaluation

Run a batch of prompts against the agent and get a pass/fail report:

```bash
python evaluate.py sample_eval.csv                  # results auto-saved to results/eval_<timestamp>.csv
python evaluate.py input.csv results/output.csv     # explicit output path
```

## Evaluation CSV Format

Create a CSV with these columns (`conversation_id` and `attachment` are optional):

```csv
prompt,expected_response,match_method,conversation_id,attachment
"Hello","hello",contains,,
"What is 2+2?","4",exact,,
"Tell me about policies","policy|procedure",regex,,
"Reset my password","credit card",not_contains,,
"Describe our benefits","health insurance",fuzzy,,
"Explain the leave policy","parental leave|80",partial,,
"Hi","hello",contains,benefits_flow,
"Tell me about dental","80%",contains,benefits_flow,
"Summarize this","key points",contains,,report.pdf
"Describe image","chart",contains,,https://example.com/chart.png
```

### Conversation Isolation

Each row runs in a **fresh conversation** by default, so earlier answers don't influence later prompts. To test multi-turn flows, give rows the same `conversation_id` — they'll share one conversation and execute in CSV order.

### File Attachments

The optional `attachment` column lets you send a file alongside the prompt:

- **URL** (`https://...`) — sent as-is via `content_url` on the attachment
- **Local file path** (`report.pdf`) — the file is base64-encoded into a data URI and sent inline

Under the hood, prompts with attachments use `ask_question_with_activity()` to send a full `Activity` object with an `attachments` list, while text-only prompts use the simpler `ask_question()`.

### Match Methods

| Method | Description |
|--------|-------------|
| `exact` | Response must equal expected text (case-insensitive) |
| `contains` | Response must contain expected substring (case-insensitive) |
| `not_contains` | Response must NOT contain the expected substring (case-insensitive) |
| `regex` | Expected value is a regex pattern tested against the response |
| `fuzzy` | Full-text similarity ratio using `SequenceMatcher`. Default threshold: 70%. Use `expected_text\|80` to set a custom threshold (e.g., 80%) |
| `partial` | Best partial substring match + word overlap score. Default threshold: 70%. Use `expected_text\|80` for a custom threshold |

For `fuzzy` and `partial`, the threshold is appended to the expected response with a `|` separator. The score (0-100%) is printed during evaluation for visibility.

## Authentication

### Interactive Mode (default)

Uses `msal.PublicClientApplication` with a browser popup. MSAL caches the token after first login, so subsequent runs use `acquire_token_silent` without re-prompting (until the token expires).

```env
AUTH_MODE=interactive
```

### S2S Mode (future)

> **Note:** S2S (server-to-server / client credentials) authentication is **not yet officially supported** by the Copilot Studio Direct-to-Engine API. The backend requires a user-context token. S2S code is included for future readiness. See the [official SDK sample README](https://github.com/microsoft/Agents/blob/main/samples/python/copilotstudio-client/README.md) for status updates.

```env
AUTH_MODE=s2s
COPILOTSTUDIO_APP_CLIENT_SECRET=your-secret
```

## Project Structure

| File | Purpose |
|------|---------|
| `config.py` | `AgentSettings` dataclass loaded from `.env` |
| `chat.py` | Interactive console chat loop; exports `acquire_token()` and `create_copilot_client()` reused by `evaluate.py` |
| `evaluate.py` | CSV-driven prompt evaluation runner with pass/fail reporting |
| `sample_eval.csv` | Example evaluation input file |
| `pyproject.toml` | Project metadata and dependencies |

## Key SDK Details

- Package: [`microsoft-agents-copilotstudio-client`](https://pypi.org/project/microsoft-agents-copilotstudio-client/) (import path uses underscores: `microsoft_agents.copilotstudio.client`)
- `CopilotClient` takes `ConnectionSettings` + an access token string. Uses SSE streaming internally.
- `start_conversation()` returns an async generator of activities (agent greeting)
- `ask_question()` returns an async generator per question (text only)
- `ask_question_with_activity()` accepts a full `Activity` object (used for attachments)
- Activity types: `message`, `typing`, `event`, `end_of_conversation` (from `microsoft_agents.activity.ActivityTypes`)
- Auth scope: `https://api.powerplatform.com/.default`

## References

- [M365 Agents SDK - Python samples](https://github.com/microsoft/Agents/tree/main/samples/python/copilotstudio-client)
- [Integrate with web/native apps using M365 Agents SDK](https://learn.microsoft.com/en-us/microsoft-copilot-studio/publication-integrate-web-or-native-app-m365-agents-sdk)
- [microsoft-agents-copilotstudio-client on PyPI](https://pypi.org/project/microsoft-agents-copilotstudio-client/)
