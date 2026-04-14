"""Load agent connection settings from .env file."""

import os
from dataclasses import dataclass
from dotenv import load_dotenv


@dataclass
class AgentSettings:
    environment_id: str
    schema_name: str
    tenant_id: str
    app_client_id: str
    app_client_secret: str
    auth_mode: str  # "interactive" or "s2s"
    timeout: int  # seconds per SDK call (ask_question / start_conversation)

    @classmethod
    def from_env(cls, env_path: str = ".env") -> "AgentSettings":
        load_dotenv(env_path)
        return cls(
            environment_id=os.environ["COPILOTSTUDIO_ENVIRONMENT_ID"],
            schema_name=os.environ["COPILOTSTUDIO_SCHEMA_NAME"],
            tenant_id=os.environ["COPILOTSTUDIO_TENANT_ID"],
            app_client_id=os.environ["COPILOTSTUDIO_APP_CLIENT_ID"],
            app_client_secret=os.environ.get("COPILOTSTUDIO_APP_CLIENT_SECRET", ""),
            auth_mode=os.environ.get("AUTH_MODE", "interactive"),
            timeout=int(os.environ.get("TIMEOUT_SECONDS", "120")),
        )

    @property
    def use_s2s(self) -> bool:
        return self.auth_mode.lower() == "s2s" and bool(self.app_client_secret)
