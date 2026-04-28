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

    # LLM Judge settings (optional — only used by general_quality / text_similarity / compare_meaning match methods)
    judge_provider: str  # "azure_openai", "openai", or "openai_compatible" (covers Ollama, LM Studio, vLLM)
    judge_base_url: str
    judge_api_key: str
    judge_model: str
    judge_api_version: str  # Azure-only

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
            judge_provider=os.environ.get("JUDGE_PROVIDER", "").lower(),
            judge_base_url=os.environ.get("JUDGE_BASE_URL", ""),
            judge_api_key=os.environ.get("JUDGE_API_KEY", ""),
            judge_model=os.environ.get("JUDGE_MODEL", ""),
            judge_api_version=os.environ.get("JUDGE_API_VERSION", "2024-08-01-preview"),
        )

    @property
    def use_s2s(self) -> bool:
        return self.auth_mode.lower() == "s2s" and bool(self.app_client_secret)

    @property
    def has_judge_config(self) -> bool:
        return bool(self.judge_provider and self.judge_model)
