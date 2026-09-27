"""DEVELOPMENT-ONLY driver: runs commands as subprocesses in a temp directory.

This provides *no* containment and exists only so the agent loop can be exercised
on a laptop without Docker. It refuses to start unless ALLOW_UNSAFE_LOCAL_SANDBOX=1
and the UI shows a red banner when it is active.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import os
import shutil
import tarfile
import tempfile
import time
import uuid
from typing import Any

from .base import Artifact, ExecResult, Sandbox, SandboxPolicy, safe_workspace_path


class LocalSandbox(Sandbox):
    def __init__(self, policy: SandboxPolicy, output_cap_bytes: int = 16_000):
        self.policy = policy
        self.sandbox_id = f"local-{uuid.uuid4().hex[:12]}"
        self._root = ""
        self._cap = output_cap_bytes

    async def start(self) -> dict[str, Any]:
        self._root = tempfile.mkdtemp(prefix="brz-")
        return {"driver": "unsafe_local", "root": self._root, "runtime": "host", "gvisor": False,
                "policy": self.policy.as_dict(), "warning": "NO CONTAINMENT - development only"}

    def _host(self, path: str) -> str:
        virtual = safe_workspace_path(path, "/workspace")
        return os.path.join(self._root, virtual[len("/workspace/"):]) if virtual != "/workspace" else self._root

    async def run(self, command: str, timeout_s: int | None = None) -> ExecResult:
        timeout = timeout_s or self.policy.cmd_timeout_s
        start = time.monotonic()
        proc = await asyncio.create_subprocess_shell(
            command, cwd=self._root, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "HOME": self._root},
        )
        timed_out = False
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            out, err = await proc.communicate()
            timed_out = True
        dur = int((time.monotonic() - start) * 1000)
        so, t1 = self._trim(out)
        se, t2 = self._trim(err)
        if timed_out:
            se += f"\n[brz] command killed after {timeout}s timeout"
        return ExecResult(command, proc.returncode if not timed_out else 137, so, se, dur, timed_out, t1 or t2)

    def _trim(self, data: bytes) -> tuple[str, bool]:
        truncated = len(data) > self._cap
        text = data[: self._cap].decode("utf-8", "replace")
        if truncated:
            text += f"\n... [truncated {len(data) - self._cap} bytes]"
        return text, truncated

    async def write_file(self, path: str, content: str) -> None:
        target = self._host(path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(content)

    async def read_file(self, path: str, max_bytes: int = 64_000) -> str:
        with open(self._host(path), "rb") as f:
            data = f.read(max_bytes + 1)
        text = data[:max_bytes].decode("utf-8", "replace")
        return text + ("\n... [truncated]" if len(data) > max_bytes else "")

    async def list_artifacts(self) -> list[Artifact]:
        out: list[Artifact] = []
        for dirpath, _, files in os.walk(self._root):
            for name in files:
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, self._root)
                with open(full, "rb") as f:
                    data = f.read()
                try:
                    preview = data[:400].decode("utf-8")
                except UnicodeDecodeError:
                    preview = "<binary>"
                out.append(Artifact(rel, len(data), hashlib.sha256(data).hexdigest(), preview))
        return sorted(out, key=lambda a: a.path)

    async def export_workspace(self) -> bytes:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            tar.add(self._root, arcname="workspace")
        return buf.getvalue()

    async def destroy(self) -> None:
        if self._root and os.path.isdir(self._root):
            shutil.rmtree(self._root, ignore_errors=True)
        self._root = ""
