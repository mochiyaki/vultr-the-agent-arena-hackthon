"""Runtime configuration. Everything is driven by environment variables (see .env.example)."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Vultr Serverless Inference (OpenAI-compatible)
    vultr_inference_api_key: str = ""
    vultr_inference_base_url: str = "https://api.vultrinference.com/v1"
    vultr_inference_model: str = "deepseek-v4.1-flash"

    # Sandbox
    sandbox_driver: str = "docker"  # docker | unsafe_local (dev only)
    sandbox_image: str = "brz-sandbox:latest"
    sandbox_runtime: str = "runsc"
    sandbox_memory: str = "512m"
    sandbox_cpus: float = 1.0
    sandbox_pids: int = 128
    sandbox_workspace_mb: int = 64
    sandbox_cmd_timeout_s: int = 60
    sandbox_task_ttl_s: int = 600
    sandbox_network: str = "none"
    allow_unsafe_local_sandbox: bool = False

    # Agent
    agent_max_steps: int = 6
    agent_max_actions_per_step: int = 8
    agent_output_cap_bytes: int = 16_000

    # Web
    port: int = 8080


settings = Settings()
