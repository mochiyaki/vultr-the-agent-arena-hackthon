from __future__ import annotations

from ..config import settings
from .base import Sandbox, SandboxPolicy


def policy_from_settings() -> SandboxPolicy:
    return SandboxPolicy(
        image=settings.sandbox_image,
        runtime=settings.sandbox_runtime,
        network=settings.sandbox_network,
        memory=settings.sandbox_memory,
        cpus=settings.sandbox_cpus,
        pids_limit=settings.sandbox_pids,
        workspace_mb=settings.sandbox_workspace_mb,
        cmd_timeout_s=settings.sandbox_cmd_timeout_s,
        task_ttl_s=settings.sandbox_task_ttl_s,
    )


def create_sandbox() -> Sandbox:
    policy = policy_from_settings()
    driver = settings.sandbox_driver.lower()
    if driver == "docker":
        from .docker_sandbox import DockerSandbox
        return DockerSandbox(policy, settings.agent_output_cap_bytes)
    if driver == "unsafe_local":
        if not settings.allow_unsafe_local_sandbox:
            raise RuntimeError("SANDBOX_DRIVER=unsafe_local requires ALLOW_UNSAFE_LOCAL_SANDBOX=1 (dev only)")
        from .local_sandbox import LocalSandbox
        return LocalSandbox(policy, settings.agent_output_cap_bytes)
    raise RuntimeError(f"unknown SANDBOX_DRIVER: {settings.sandbox_driver}")


def describe_driver() -> dict:
    """Health/summary info for the UI and /api/system."""
    info = {"driver": settings.sandbox_driver, "policy": policy_from_settings().as_dict(), "gvisor": False,
            "docker": False, "contained": settings.sandbox_driver == "docker"}
    if settings.sandbox_driver == "docker":
        try:
            import docker
            client = docker.from_env()
            runtimes = client.info().get("Runtimes") or {}
            info["docker"] = True
            info["gvisor"] = settings.sandbox_runtime in runtimes
            info["runtimes"] = sorted(runtimes.keys())
            info["image_present"] = bool(client.images.list(name=settings.sandbox_image))
        except Exception as exc:
            info["error"] = str(exc)
    return info
