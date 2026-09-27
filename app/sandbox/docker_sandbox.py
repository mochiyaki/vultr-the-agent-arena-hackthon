"""Hardened Docker sandbox driver.

Each task gets its own container on the Vultr VM:
  * gVisor (runsc) runtime when available - syscalls never reach the host kernel directly
  * network disabled (no egress, no lateral movement)
  * read-only root filesystem, tmpfs workspace with a size cap
  * all Linux capabilities dropped, no-new-privileges, runs as nobody
  * memory / CPU / PID limits, per-command kill timeout, whole-task TTL
The container is force-removed when the task completes, fails, or times out.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import posixpath
import tarfile
import time
import uuid
from typing import Any

from .base import Artifact, ExecResult, Sandbox, SandboxPolicy, safe_workspace_path

log = logging.getLogger("brz.sandbox.docker")

WORKSPACE = "/workspace"
EXPORT_CAP_BYTES = 20 * 1024 * 1024


def _runtime_available(client, runtime: str) -> bool:
    try:
        return runtime in (client.info().get("Runtimes") or {})
    except Exception:  # pragma: no cover - defensive
        return False


class DockerSandbox(Sandbox):
    def __init__(self, policy: SandboxPolicy, output_cap_bytes: int = 16_000):
        import docker  # local import so the dev driver works without the SDK

        self.policy = policy
        self.sandbox_id = f"brz-{uuid.uuid4().hex[:12]}"
        self._client = docker.from_env()
        self._container = None
        self._output_cap = output_cap_bytes
        self._created_at = 0.0
        self.effective_runtime: str = "runc"

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._start_sync)

    def _start_sync(self) -> dict[str, Any]:
        p = self.policy
        runtime = p.runtime if _runtime_available(self._client, p.runtime) else None
        if runtime is None and p.runtime not in ("", "runc"):
            log.warning("runtime %s not available on host, falling back to runc", p.runtime)
        self.effective_runtime = runtime or "runc"

        kwargs: dict[str, Any] = dict(
            image=p.image,
            command=["sleep", str(p.task_ttl_s)],  # container self-destructs after TTL
            name=self.sandbox_id,
            detach=True,
            network_mode=p.network,
            mem_limit=p.memory,
            memswap_limit=p.memory,  # no swap
            nano_cpus=int(p.cpus * 1e9),
            pids_limit=p.pids_limit,
            read_only=p.read_only_rootfs,
            tmpfs={
                WORKSPACE: f"rw,nosuid,size={p.workspace_mb}m,uid=65534,gid=65534,mode=0700",
                "/tmp": "rw,nosuid,size=32m,uid=65534,gid=65534,mode=0700",
            },
            cap_drop=["ALL"] if p.drop_all_capabilities else [],
            security_opt=["no-new-privileges:true"] if p.no_new_privileges else [],
            user=p.user,
            working_dir=WORKSPACE,
            environment={"HOME": "/tmp", "PYTHONDONTWRITEBYTECODE": "1", "MPLCONFIGDIR": "/tmp"},
            labels={"brz.sandbox": "true", "brz.created": str(int(time.time()))},
            init=True,
        )
        if runtime:
            kwargs["runtime"] = runtime
        self._container = self._client.containers.run(**kwargs)
        self._created_at = time.time()
        return {
            "driver": "docker",
            "container_id": self._container.short_id,
            "runtime": self.effective_runtime,
            "gvisor": self.effective_runtime == "runsc",
            "policy": p.as_dict(),
        }

    async def destroy(self) -> None:
        await asyncio.to_thread(self._destroy_sync)

    def _destroy_sync(self) -> None:
        if self._container is None:
            return
        try:
            self._container.remove(force=True, v=True)
        except Exception as exc:  # already gone
            log.debug("remove failed for %s: %s", self.sandbox_id, exc)
        self._container = None

    # ------------------------------------------------------------------ execution
    async def run(self, command: str, timeout_s: int | None = None) -> ExecResult:
        return await asyncio.to_thread(self._run_sync, command, timeout_s or self.policy.cmd_timeout_s)

    def _run_sync(self, command: str, timeout_s: int) -> ExecResult:
        assert self._container is not None, "sandbox not started"
        start = time.monotonic()
        # `timeout -s KILL` enforces the per-command budget *inside* the sandbox;
        # the outer thread also guards against a hung docker daemon.
        argv = ["timeout", "-s", "KILL", str(timeout_s), "sh", "-c", command]
        code, (out, err) = self._container.exec_run(
            argv, demux=True, workdir=WORKSPACE, user=self.policy.user, environment={"HOME": "/tmp"}
        )
        duration = int((time.monotonic() - start) * 1000)
        stdout, t1 = self._cap(out)
        stderr, t2 = self._cap(err)
        timed_out = code == 137 and duration >= timeout_s * 1000 - 200
        if timed_out:
            stderr = (stderr + f"\n[brz] command killed after {timeout_s}s timeout").strip()
        return ExecResult(command, code, stdout, stderr, duration, timed_out, t1 or t2)

    def _cap(self, data: bytes | None) -> tuple[str, bool]:
        if not data:
            return "", False
        truncated = len(data) > self._output_cap
        text = data[: self._output_cap].decode("utf-8", "replace")
        if truncated:
            text += f"\n... [truncated {len(data) - self._output_cap} bytes]"
        return text, truncated

    # ------------------------------------------------------------------ files
    async def write_file(self, path: str, content: str) -> None:
        full = safe_workspace_path(path, WORKSPACE)
        await asyncio.to_thread(self._write_sync, full, content.encode("utf-8"))

    def _write_sync(self, full: str, data: bytes) -> None:
        assert self._container is not None
        parent, name = posixpath.split(full)
        if parent != WORKSPACE:
            self._container.exec_run(["mkdir", "-p", parent], user=self.policy.user)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mode = 0o644
            info.uid = info.gid = 65534
            info.mtime = int(time.time())
            tar.addfile(info, io.BytesIO(data))
        buf.seek(0)
        self._container.put_archive(parent, buf.getvalue())

    async def read_file(self, path: str, max_bytes: int = 64_000) -> str:
        full = safe_workspace_path(path, WORKSPACE)
        return await asyncio.to_thread(self._read_sync, full, max_bytes)

    def _read_sync(self, full: str, max_bytes: int) -> str:
        assert self._container is not None
        stream, _stat = self._container.get_archive(full)
        raw = b"".join(stream)
        with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
            member = next(m for m in tar.getmembers() if m.isfile())
            f = tar.extractfile(member)
            data = f.read(max_bytes + 1) if f else b""
        text = data[:max_bytes].decode("utf-8", "replace")
        if len(data) > max_bytes:
            text += "\n... [truncated]"
        return text

    async def list_artifacts(self) -> list[Artifact]:
        raw = await self.export_workspace()
        return artifacts_from_tar(raw)

    async def export_workspace(self) -> bytes:
        return await asyncio.to_thread(self._export_sync)

    def _export_sync(self) -> bytes:
        assert self._container is not None
        stream, _ = self._container.get_archive(WORKSPACE)
        chunks, total = [], 0
        for chunk in stream:
            total += len(chunk)
            if total > EXPORT_CAP_BYTES:
                raise RuntimeError("workspace export exceeds size cap")
            chunks.append(chunk)
        return b"".join(chunks)


def artifacts_from_tar(raw: bytes, strip_prefix: str = "workspace/") -> list[Artifact]:
    """Turn a workspace tarball into a hashed artifact manifest."""
    out: list[Artifact] = []
    with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
        for m in tar.getmembers():
            if not m.isfile():
                continue
            name = m.name
            if name.startswith(strip_prefix):
                name = name[len(strip_prefix):]
            if name.startswith("./"):
                name = name[2:]
            f = tar.extractfile(m)
            data = f.read() if f else b""
            try:
                preview = data[:400].decode("utf-8")
            except UnicodeDecodeError:
                preview = "<binary>"
            out.append(Artifact(name, len(data), hashlib.sha256(data).hexdigest(), preview))
    out.sort(key=lambda a: a.path)
    return out
