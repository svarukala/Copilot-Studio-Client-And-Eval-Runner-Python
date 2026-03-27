"""Interactive console chat with a Copilot Studio agent via Direct-to-Engine."""

import asyncio
import sys

import msal
from microsoft_agents.activity import ActivityTypes
from microsoft_agents.copilotstudio.client import ConnectionSettings, CopilotClient

from config import AgentSettings


def acquire_token(settings: AgentSettings) -> str:
    """Acquire an access token using MSAL (interactive or S2S)."""
    if settings.use_s2s:
        app = msal.ConfidentialClientApplication(
            client_id=settings.app_client_id,
            client_credential=settings.app_client_secret,
            authority=f"https://login.microsoftonline.com/{settings.tenant_id}",
        )
        result = app.acquire_token_for_client(
            scopes=["https://api.powerplatform.com/.default"]
        )
    else:
        app = msal.PublicClientApplication(
            client_id=settings.app_client_id,
            authority=f"https://login.microsoftonline.com/{settings.tenant_id}",
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
            if activity.type == ActivityTypes.end_of_conversation:
                return


def main():
    asyncio.run(run_chat())


if __name__ == "__main__":
    main()
