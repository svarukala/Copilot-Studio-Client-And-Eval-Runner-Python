"""Interactive console chat with a Copilot Studio agent via Direct-to-Engine."""

import asyncio
import atexit
import sys
from pathlib import Path

import msal
from microsoft_agents.activity import Activity, ActivityTypes
from microsoft_agents.copilotstudio.client import ConnectionSettings, CopilotClient

from config import AgentSettings

CACHE_PATH = Path(__file__).parent / ".token_cache.bin"


def _load_cache() -> msal.SerializableTokenCache:
    """Load a persistent MSAL token cache from disk."""
    cache = msal.SerializableTokenCache()
    if CACHE_PATH.exists():
        cache.deserialize(CACHE_PATH.read_text(encoding="utf-8"))
    atexit.register(_save_cache, cache)
    return cache


def _save_cache(cache: msal.SerializableTokenCache) -> None:
    """Write the token cache to disk if it changed."""
    if cache.has_state_changed:
        CACHE_PATH.write_text(cache.serialize(), encoding="utf-8")


def acquire_token(settings: AgentSettings) -> str:
    """Acquire an access token using MSAL (interactive or S2S)."""
    cache = _load_cache()

    if settings.use_s2s:
        app = msal.ConfidentialClientApplication(
            client_id=settings.app_client_id,
            client_credential=settings.app_client_secret,
            authority=f"https://login.microsoftonline.com/{settings.tenant_id}",
            token_cache=cache,
        )
        result = app.acquire_token_for_client(
            scopes=["https://api.powerplatform.com/.default"]
        )
    else:
        app = msal.PublicClientApplication(
            client_id=settings.app_client_id,
            authority=f"https://login.microsoftonline.com/{settings.tenant_id}",
            token_cache=cache,
        )
        accounts = app.get_accounts()
        if accounts:
            result = app.acquire_token_silent(
                scopes=["https://api.powerplatform.com/.default"],
                account=accounts[0],
            )
        else:
            result = None

        if not result:
            result = app.acquire_token_interactive(
                scopes=["https://api.powerplatform.com/.default"]
            )

    token = result.get("access_token")
    if not token:
        print(f"Authentication failed: {result.get('error_description', result)}")
        sys.exit(1)
    return token


def create_copilot_client(settings: AgentSettings) -> CopilotClient:
    """Build a CopilotClient from settings."""
    conn = ConnectionSettings(
        environment_id=settings.environment_id,
        agent_identifier=settings.schema_name,
    )
    token = acquire_token(settings)
    return CopilotClient(conn, token)


def print_activity(activity) -> None:
    """Print an activity to the console, mirroring the C# sample output."""
    if activity.type == ActivityTypes.message:
        if activity.text:
            print(activity.text)
        if getattr(activity, "attachments", None):
            for att in activity.attachments:
                ct = getattr(att, "content_type", "") or ""
                content = getattr(att, "content", None)
                if content and "application/vnd.microsoft.card" in ct:
                    _print_card(ct, content)
                elif content:
                    print(f"  [Attachment: {ct}]")
        if getattr(activity, "suggested_actions", None):
            for action in activity.suggested_actions.actions:
                title = getattr(action, "title", None) or getattr(action, "text", "")
                print(f"  - {title}")
    elif activity.type == "typing":
        print(".", end="", flush=True)
    elif activity.type == "event":
        print("+", end="", flush=True)
    elif activity.type == ActivityTypes.end_of_conversation:
        print("\n[End of conversation]")


def _print_card(content_type: str, content) -> None:
    """Pretty-print a Bot Framework card attachment."""
    card_type = content_type.rsplit(".", 1)[-1] if "." in content_type else content_type
    body = content if isinstance(content, dict) else {}

    # Adaptive Card / Consent Card
    if card_type == "card.adaptive":
        card_body = body.get("body", [])
        for block in card_body:
            text = block.get("text", "")
            if text:
                print(f"  [Card] {text}")
        for action in body.get("actions", []):
            title = action.get("title", "")
            url = action.get("url", "")
            if title:
                print(f"  [Card Action] {title}" + (f" -> {url}" if url else ""))
        if not card_body and not body.get("actions"):
            print(f"  [Adaptive Card] {body}")
    # Sign-in card
    elif card_type == "card.signin":
        text = body.get("text", "Sign in required")
        print(f"  [Sign-in Card] {text}")
        for btn in body.get("buttons", []):
            print(f"  [Sign-in] {btn.get('title', 'Sign in')} -> {btn.get('value', '')}")
    # OAuth card
    elif card_type == "card.oauth":
        text = body.get("text", "Authentication required")
        print(f"  [OAuth Card] {text}")
        for btn in body.get("buttons", []):
            print(f"  [OAuth] {btn.get('title', 'Sign in')} -> {btn.get('value', '')}")
    else:
        print(f"  [{card_type}] {body.get('title', body.get('text', ''))}")


# ---------------------------------------------------------------------------
# Consent card handling
# ---------------------------------------------------------------------------

CONSENT_CARD_NAME = "aiPrompt/consentCard"


def is_consent_card(activity) -> bool:
    """Check if an activity is a consent card that needs auto-approval."""
    if getattr(activity, "type", None) != "message":
        return False
    name = getattr(activity, "name", None) or ""
    return name.lower() == CONSENT_CARD_NAME.lower()


def extract_consent_actions(activity) -> dict[str, object]:
    """Extract Action.Submit title->data mappings from consent card attachments."""
    actions: dict[str, object] = {}
    attachments = getattr(activity, "attachments", None) or []
    for att in attachments:
        ct = (getattr(att, "content_type", "") or "").lower()
        if ct != "application/vnd.microsoft.card.adaptive":
            continue
        content = getattr(att, "content", None)
        if not isinstance(content, dict):
            continue
        _collect_submit_actions(content, actions)
    return actions


def _collect_submit_actions(element, actions: dict[str, object]) -> None:
    """Recursively find Action.Submit entries in an adaptive card payload."""
    if isinstance(element, dict):
        for action in element.get("actions", []):
            if not isinstance(action, dict):
                continue
            if action.get("type", "").lower() != "action.submit":
                continue
            title = action.get("title", "")
            data = action.get("data")
            if title and data is not None:
                actions[title] = data
        for value in element.values():
            _collect_submit_actions(value, actions)
    elif isinstance(element, list):
        for child in element:
            _collect_submit_actions(child, actions)


async def handle_consent_card(client: CopilotClient, activity, choice: str = "Allow") -> list:
    """Auto-approve a consent card and return follow-up activities.

    Sends an Activity with type='message', value=<action data>,
    name='aiPrompt/consentCard' via client.execute().
    """
    conv_id = ""
    if getattr(activity, "conversation", None):
        conv_id = getattr(activity.conversation, "id", "") or ""
    if not conv_id:
        conv_id = client._current_conversation_id

    actions = extract_consent_actions(activity)
    if not actions:
        print("  [consent] No submit actions found in consent card.")
        return []

    action_titles = list(actions.keys())
    print(f"  [consent] Available actions: {', '.join(action_titles)}")

    # Pick the requested choice, fall back to first available
    if choice in actions:
        data = actions[choice]
    else:
        print(f"  [consent] '{choice}' not found, using '{action_titles[0]}'")
        data = actions[action_titles[0]]
        choice = action_titles[0]

    print(f"  [consent] Auto-approving: {choice}")

    submit = Activity(type="message", value=data, name=CONSENT_CARD_NAME)

    follow_ups = []
    async for follow_up in client.execute(conv_id, submit):
        follow_ups.append(follow_up)
    return follow_ups


async def run_chat() -> None:
    settings = AgentSettings.from_env()
    client = create_copilot_client(settings)

    # Start conversation and print greeting
    print("\nagent> ", end="", flush=True)
    async for activity in client.start_conversation(emit_start_conversation_event=True):
        print_activity(activity)

    # Message loop
    while True:
        try:
            question = input("\nuser> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            break

        if not question or question.lower() in ("exit", "quit"):
            print("Goodbye!")
            break

        print("\nagent> ", end="", flush=True)
        async for activity in client.ask_question(question):
            print_activity(activity)
            if is_consent_card(activity):
                follow_ups = await handle_consent_card(client, activity)
                for fu in follow_ups:
                    print_activity(fu)
                    if fu.type == ActivityTypes.end_of_conversation:
                        return
            if activity.type == ActivityTypes.end_of_conversation:
                return


def main():
    asyncio.run(run_chat())


if __name__ == "__main__":
    main()
