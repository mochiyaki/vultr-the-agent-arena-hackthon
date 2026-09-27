"""The control-plane agent: PLAN -> EXECUTE (inside a throwaway sandbox) -> VERIFY.

The LLM never has direct access to anything. It can only request four tools, each of
which is mediated by the control plane and executed inside the task's sandbox:
    run_command(command)        shell command, hard timeout, no network
    write_file(path, content)   text file inside /workspace
    read_file(path)             text file inside /workspace
    finish_step(summary)        declare the current step done

Every tool call is recorded as a verifiable execution (exit code, hashes, duration)
and streamed to the browser in real time.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import asdict
from typing import Any

from .config import settings
from .llm import LLM, extract_json
from .sandbox import Sandbox, create_sandbox
from .store import Task

log = logging.getLogger("brz.agent")

TOOLS = [
    {"type": "function", "function": {
        "name": "run_command",
        "description": "Run a shell command inside the isolated sandbox (cwd=/workspace, no network, hard timeout). Returns exit code, stdout, stderr.",
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "write_file",
        "description": "Create or overwrite a UTF-8 text file inside /workspace.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                       "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read a UTF-8 text file from /workspace.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "finish_step",
        "description": "Call when the current step's success criteria are met (or cannot be met). Provide a short factual summary of what was executed and observed.",
        "parameters": {"type": "object", "properties": {"summary": {"type": "string"}, "success": {"type": "boolean"}},
                       "required": ["summary", "success"]}}},
]

PLANNER_SYSTEM = """You are the planner of a sandboxed coding agent. The executor can only run shell
commands and read/write files inside an isolated Linux container (Python 3.12 with numpy, pandas,
matplotlib, requests-less; Node.js available; NO network access; no sudo; 60s per command).

Break the user's task into 1-{max_steps} concrete, sequential steps. Each step must produce
observable evidence (command output or a file). The last step should verify the result
(e.g. run tests, print checks, show file contents).

Respond with ONLY a JSON array, e.g.
[{{"title": "Write script", "goal": "Create solve.py that ...", "success_criteria": "solve.py exists and runs without error"}}]"""

EXECUTOR_SYSTEM = """You are the executor of a sandboxed coding agent working on ONE step of a plan.
You work inside an isolated container: cwd is /workspace, there is NO network access, python3 and
node are available, commands time out after {timeout}s. Use the tools to do real work. Do not just
describe what you would do - execute it. Prefer small, checkable actions. When the success criteria
are met (or clearly cannot be met), call finish_step with a factual summary. You have at most
{max_actions} tool calls for this step.

Overall task: {task}
Full plan: {plan}
Current step {n}: {title}
Goal: {goal}
Success criteria: {criteria}
Files currently in /workspace: {files}"""

VERIFIER_SYSTEM = """You are an independent verifier. You receive the user's task, the plan, the exact
commands that were executed with their exit codes and outputs, and a hashed manifest of the files
that exist in the sandbox workspace. Decide whether the task was genuinely accomplished based on
the EVIDENCE only (never on claims). Respond with ONLY JSON:
{"passed": true|false, "confidence": 0.0-1.0, "summary": "...", "evidence": ["short bullet citing a command/output/artifact", ...], "issues": ["..."]}"""


class Agent:
    def __init__(self, llm: LLM, sandbox_factory=create_sandbox):
        self.llm = llm
        self.sandbox_factory = sandbox_factory

    # ------------------------------------------------------------------ entry
    async def run_task(self, task: Task) -> None:
        sandbox: Sandbox | None = None
        try:
            task.status = "planning"
            task.emit("status", status="planning", message="Planning task with Vultr Serverless Inference")
            plan = await self.plan(task)
            task.plan = plan
            task.emit("plan", steps=plan)

            sandbox = self.sandbox_factory()
            task.emit("status", status="provisioning", message="Provisioning isolated sandbox")
            meta = await sandbox.start()
            task.sandbox = {"id": sandbox.sandbox_id, **meta}
            task.emit("sandbox", **task.sandbox)

            task.status = "running"
            deadline = time.monotonic() + settings.sandbox_task_ttl_s
            for i, step in enumerate(plan, 1):
                if time.monotonic() > deadline:
                    raise TimeoutError("task TTL exceeded")
                task.emit("step_start", index=i, **step)
                result = await self.execute_step(task, sandbox, plan, i, step)
                step["result"] = result
                task.emit("step_done", index=i, **result)

            task.status = "verifying"
            task.emit("status", status="verifying", message="Verifying results against execution evidence")
            artifacts = await sandbox.list_artifacts()
            task.artifacts = [asdict(a) for a in artifacts]
            try:
                task.workspace_tar = await sandbox.export_workspace()
            except Exception as exc:  # export is best-effort
                task.emit("warning", message=f"workspace export skipped: {exc}")
            task.verification = await self.verify(task)
            task.emit("verification", **task.verification)
            task.status = "done"
        except Exception as exc:
            log.exception("task %s failed", task.id)
            task.status = "failed"
            task.error = f"{type(exc).__name__}: {exc}"
            task.emit("error", message=task.error)
        finally:
            if sandbox is not None:
                await sandbox.destroy()
                task.emit("sandbox_destroyed", id=sandbox.sandbox_id)
            task.finished_at = time.time()
            task.emit("status", status=task.status, message="finished")

    # ------------------------------------------------------------------ plan
    async def plan(self, task: Task) -> list[dict[str, Any]]:
        reply = await self._chat(task, [
            {"role": "system", "content": PLANNER_SYSTEM.format(max_steps=settings.agent_max_steps)},
            {"role": "user", "content": task.prompt},
        ])
        steps = extract_json(reply.content)
        if isinstance(steps, dict):
            steps = steps.get("steps") or steps.get("plan") or [steps]
        clean = []
        for s in steps[: settings.agent_max_steps]:
            if not isinstance(s, dict):
                continue
            clean.append({"title": str(s.get("title") or s.get("goal") or "step")[:120],
                          "goal": str(s.get("goal") or s.get("title") or "")[:600],
                          "success_criteria": str(s.get("success_criteria") or "")[:400]})
        if not clean:
            raise ValueError("planner returned an empty plan")
        return clean

    # ------------------------------------------------------------------ execute
    async def execute_step(self, task: Task, sandbox: Sandbox, plan: list[dict], n: int, step: dict) -> dict:
        files = [a.path for a in await sandbox.list_artifacts()]
        messages = [
            {"role": "system", "content": EXECUTOR_SYSTEM.format(
                timeout=settings.sandbox_cmd_timeout_s, max_actions=settings.agent_max_actions_per_step,
                task=task.prompt, plan=json.dumps([{"title": p["title"]} for p in plan]),
                n=n, title=step["title"], goal=step["goal"], criteria=step["success_criteria"],
                files=", ".join(files) or "(empty)")},
            {"role": "user", "content": "Begin. Use tools; do not answer in prose."},
        ]
        actions = 0
        summary, success = "", False
        while actions < settings.agent_max_actions_per_step:
            reply = await self._chat(task, messages, tools=TOOLS)
            if not reply.tool_calls:
                # Model answered in prose: nudge once, then accept as summary.
                if actions == 0 and reply.content:
                    messages.append({"role": "assistant", "content": reply.content})
                    messages.append({"role": "user", "content": "You must call a tool. Execute, don't describe."})
                    actions += 1
                    continue
                summary = reply.content or "step ended without explicit finish"
                break
            messages.append({"role": "assistant", "content": reply.content or None, "tool_calls": reply.raw_tool_calls})
            finished = False
            for call in reply.tool_calls:
                actions += 1
                result = await self.dispatch(task, sandbox, n, call.name, call.arguments)
                messages.append({"role": "tool", "tool_call_id": call.id, "name": call.name, "content": result})
                if call.name == "finish_step":
                    finished = True
                    summary = str(call.arguments.get("summary", ""))
                    success = bool(call.arguments.get("success", True))
            if finished:
                break
        else:
            summary = summary or f"action budget ({settings.agent_max_actions_per_step}) exhausted"
        return {"summary": summary[:1500], "success": success, "actions": actions}

    async def dispatch(self, task: Task, sandbox: Sandbox, step_index: int, name: str, args: dict) -> str:
        """Execute one tool call inside the sandbox and return the tool message content."""
        try:
            if name == "run_command":
                cmd = str(args.get("command", "")).strip()
                if not cmd:
                    return "error: empty command"
                task.emit("exec_start", step=step_index, command=cmd)
                res = await sandbox.run(cmd)
                record = {"step": step_index, "kind": "run_command", **res.as_dict()}
                task.executions.append(record)
                task.emit("exec_done", **record)
                return json.dumps({"exit_code": res.exit_code, "timed_out": res.timed_out,
                                   "stdout": res.stdout, "stderr": res.stderr})
            if name == "write_file":
                path, content = str(args.get("path", "")), str(args.get("content", ""))
                await sandbox.write_file(path, content)
                record = {"step": step_index, "kind": "write_file", "path": path, "bytes": len(content.encode())}
                task.executions.append(record)
                task.emit("file_written", **record, preview=content[:600])
                return f"wrote {record['bytes']} bytes to {path}"
            if name == "read_file":
                path = str(args.get("path", ""))
                content = await sandbox.read_file(path)
                task.emit("file_read", step=step_index, path=path, bytes=len(content))
                return content
            if name == "finish_step":
                task.emit("step_summary", step=step_index, summary=str(args.get("summary", "")),
                          success=bool(args.get("success", True)))
                return "acknowledged"
            return f"error: unknown tool {name}"
        except Exception as exc:
            task.emit("tool_error", step=step_index, tool=name, message=str(exc))
            return f"error: {exc}"

    # ------------------------------------------------------------------ verify
    async def verify(self, task: Task) -> dict[str, Any]:
        evidence = {
            "task": task.prompt,
            "plan": [{k: v for k, v in s.items()} for s in task.plan],
            "executions": [self._compact_exec(e) for e in task.executions][-40:],
            "artifacts": [{"path": a["path"], "size": a["size"], "sha256": a["sha256"][:16], "preview": a["preview"][:200]}
                          for a in task.artifacts][:40],
        }
        try:
            reply = await self._chat(task, [
                {"role": "system", "content": VERIFIER_SYSTEM},
                {"role": "user", "content": json.dumps(evidence, ensure_ascii=False)[:60_000]},
            ], temperature=0.0)
            verdict = extract_json(reply.content)
        except Exception as exc:
            verdict = {"passed": False, "confidence": 0.0, "summary": f"verifier unavailable: {exc}",
                       "evidence": [], "issues": ["verifier error"]}
        failed_cmds = sum(1 for e in task.executions if e.get("kind") == "run_command" and e.get("exit_code") != 0)
        verdict.update({
            "passed": bool(verdict.get("passed")),
            "confidence": float(verdict.get("confidence") or 0),
            "commands_run": sum(1 for e in task.executions if e.get("kind") == "run_command"),
            "commands_failed": failed_cmds,
            "files_written": sum(1 for e in task.executions if e.get("kind") == "write_file"),
            "artifact_count": len(task.artifacts),
        })
        return verdict

    @staticmethod
    def _compact_exec(e: dict) -> dict:
        if e.get("kind") == "run_command":
            return {"cmd": e["command"][:300], "exit": e["exit_code"], "ms": e["duration_ms"],
                    "stdout": e["stdout"][-1200:], "stderr": e["stderr"][-600:]}
        return {k: e[k] for k in ("kind", "path", "bytes") if k in e}

    # ------------------------------------------------------------------ llm
    async def _chat(self, task: Task, messages: list[dict], tools=None, temperature: float = 0.2):
        reply = await self.llm.chat(messages, tools=tools, temperature=temperature)
        task.usage["llm_calls"] += 1
        task.usage["prompt_tokens"] += int(reply.usage.get("prompt_tokens") or 0)
        task.usage["completion_tokens"] += int(reply.usage.get("completion_tokens") or 0)
        task.emit("llm", model=getattr(self.llm, "model", "?"), tool_calls=[c.name for c in reply.tool_calls],
                  content=(reply.content or "")[:400])
        return reply
