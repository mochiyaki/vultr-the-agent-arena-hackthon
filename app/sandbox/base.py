"""Sandbox contract shared by every driver.

A Sandbox is a throwaway execution environment that lives for exactly one task.
The agent never touches the host: every command, file write and file read goes
through this interface, and the control plane destroys the sandbox when the task ends.
"""
from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass(frozen=True)
class SandboxPolicy:
    """Containment policy applied to a sandbox. Immutable so it can be audited verbatim."""

    image: str = "brz-sandbox:latest"
    runtime: str = "runsc"          # gVisor; falls back to runc if unavailable
    network: str = "none"           # no egress by default
    memory: str = "512m"
    cpus: float = 1.0
    pids_limit: int = 128
    workspace_mb: int = 64
    cmd_timeout_s: int = 60
    task_ttl_s: int = 600
    read_only_rootfs: bool = True
    drop_all_capabilities: bool = True
    no_new_privileges: bool = True
    user: str = "65534:65534"       # nobody

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ExecResult:
    """Verifiable record of one command executed inside the sandbox."""

    command: str
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False
    truncated: bool = False
    stdout_sha256: str = field(default="")

    def __post_init__(self) -> None:
        if not self.stdout_sha256:
            self.stdout_sha256 = hashlib.sha256(self.stdout.encode("utf-8", "replace")).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Artifact:
    path: str
    size: int
    sha256: str
    preview: str  # first bytes, text only


class Sandbox(ABC):
    """One isolated environment per task."""

    policy: SandboxPolicy
    sandbox_id: str

    @abstractmethod
    async def start(self) -> dict[str, Any]:
        """Provision the environment. Returns driver metadata for the audit trail."""

    @abstractmethod
    async def run(self, command: str, timeout_s: int | None = None) -> ExecResult:
        """Run a shell command inside the sandbox with a hard timeout."""

    @abstractmethod
    async def write_file(self, path: str, content: str) -> None:
        """Write a UTF-8 text file into the sandbox workspace."""

    @abstractmethod
    async def read_file(self, path: str, max_bytes: int = 64_000) -> str:
        """Read a text file from the sandbox workspace."""

    @abstractmethod
    async def list_artifacts(self) -> list[Artifact]:
        """List files in the workspace with hashes for verification."""

    @abstractmethod
    async def export_workspace(self) -> bytes:
        """Return a tar archive of the workspace (size-capped)."""

    @abstractmethod
    async def destroy(self) -> None:
        """Tear the environment down. Must be idempotent."""


def safe_workspace_path(path: str, root: str = "/workspace") -> str:
    """Normalise a user/LLM-provided path and refuse anything escaping the workspace."""
    import posixpath

    if not path or path.startswith("~"):
        raise ValueError("invalid path")
    candidate = path if path.startswith("/") else posixpath.join(root, path)
    normalized = posixpath.normpath(candidate)
    if normalized != root and not normalized.startswith(root + "/"):
        raise ValueError(f"path escapes workspace: {path}")
    return normalized
