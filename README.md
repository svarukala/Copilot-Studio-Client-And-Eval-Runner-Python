# Copilot Studio Python Client

Python client for chatting with Microsoft Copilot Studio agents using the M365 Agents SDK ("Direct-to-Engine" protocol). Includes an interactive chat mode and a CSV-driven prompt evaluation runner.

## Prerequisites

- Python 3.10+
- A published Copilot Studio agent (you'll need the environment ID, schema name, and tenant ID — found in Copilot Studio under **Settings > Advanced > Metadata**)
- An Azure AD app registration (see below)

## Create an App Registration

1. Go to [Azure Portal > App registrations](https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade) and click **New registration**
2. Name it (e.g., `copilot-studio-client`), set **Supported account types** to *Single tenant*, and click **Register**
3. On the app's **Overview** page, copy the **Application (client) ID** — this is your `COPILOTSTUDIO_APP_CLIENT_ID`
4. Go to **Authentication > Add a platform > Mobile and desktop applications** and add `http://localhost` as a redirect URI
5. Under **API permissions > Add a permission > APIs my organization uses**, search for `Power Platform API` (`https://api.powerplatform.com`), select **Delegated permissions**, and add `user_impersonation`
6. Click **Grant admin consent** (or ask your admin)

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

## Usage

### Interactive Chat

```bash
python chat.py
```

A browser window will open for authentication on first run. The token is cached locally (`.token_cache.bin`), so subsequent runs skip the login prompt until the refresh token expires. Type `exit` or `quit` to end the session.

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

- **URL** (`https://...`) — downloaded and base64-encoded into a data URI
- **Local file path** (`report.pdf`, `C:\docs\test.pdf`) — read from disk and base64-encoded

Both are sent inline as `data:` URIs because the Direct-to-Engine API does not fetch external URLs on behalf of the agent. Under the hood, prompts with attachments use `ask_question_with_activity()` to send a full `Activity` object, while text-only prompts use the simpler `ask_question()`.

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

Uses `msal.PublicClientApplication` with a browser popup. The token cache is persisted to `.token_cache.bin`, so subsequent runs use `acquire_token_silent` without re-prompting. MSAL automatically refreshes expired access tokens using the cached refresh token. You'll only see a browser login again when the refresh token itself expires (~90 days).

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

## Roadmap

- **LLM-as-a-Judge evaluation** — Use a large language model to evaluate agent responses instead of (or alongside) deterministic match methods. This would support open-ended quality checks like "Is the response helpful and accurate?" without requiring exact expected text. If you're interested in this feature, please open an issue or upvote an existing one.

## References

- [M365 Agents SDK - Python samples](https://github.com/microsoft/Agents/tree/main/samples/python/copilotstudio-client)
- [Integrate with web/native apps using M365 Agents SDK](https://learn.microsoft.com/en-us/microsoft-copilot-studio/publication-integrate-web-or-native-app-m365-agents-sdk)
- [microsoft-agents-copilotstudio-client on PyPI](https://pypi.org/project/microsoft-agents-copilotstudio-client/)
