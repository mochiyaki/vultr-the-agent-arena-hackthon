"""End-to-end agent loop with a scripted fake LLM: plan -> execute -> verify -> destroy."""
import json
import os

import pytest

from app.agent import Agent
from app.llm import LLMReply, ToolCall
from app.sandbox import create_sandbox
from app.store import TaskStore


def _tc(i, name, **args):
    return ToolCall(f"c{i}", name, args), {"id": f"c{i}", "type": "function",
                                          "function": {"name": name, "arguments": json.dumps(args)}}


class FakeLLM:
    """Deterministic stand-in for Vultr inference that follows the tool protocol."""
    model = "fake-model"

    def __init__(self):
        self.calls = 0
        self.seen_tool_results = []

    async def chat(self, messages, tools=None, temperature=0.2, max_tokens=2048):
        self.calls += 1
        for m in messages:
            if m.get("role") == "tool":
                self.seen_tool_results.append(m["content"])
        system = messages[0]["content"]
        if "planner" in system:
            return LLMReply(content=json.dumps([
                {"title": "Write script", "goal": "create hello.py", "success_criteria": "file exists"},
                {"title": "Run it", "goal": "run hello.py", "success_criteria": "prints hello"},
            ]))
        if "independent verifier" in system:
            evidence = json.loads(messages[1]["content"])
            ok = any(e.get("exit") == 0 and "hello" in e.get("stdout", "") for e in evidence["executions"])
            return LLMReply(content=json.dumps({"passed": ok, "confidence": 0.9, "summary": "ran", "evidence": ["x"]}))
        # executor
        n_tools = sum(1 for m in messages if m.get("role") == "tool")
        if "Current step 1" in system:
            if n_tools == 0:
                tc, raw = _tc(1, "write_file", path="hello.py", content="print('hello from sandbox')\n")
            else:
                tc, raw = _tc(2, "finish_step", summary="wrote hello.py", success=True)
        else:
            if n_tools == 0:
                tc, raw = _tc(3, "run_command", command="python3 hello.py && ls -la && echo ../escape > /dev/null")
            elif n_tools == 1:
                tc, raw = _tc(4, "read_file", path="../../etc/hostname")  # must be refused
            else:
                tc, raw = _tc(5, "finish_step", summary="ran it", success=True)
        return LLMReply(content="", tool_calls=[tc], raw_tool_calls=[raw], usage={"prompt_tokens": 10, "completion_tokens": 5})


@pytest.mark.asyncio
async def test_full_task_lifecycle():
    llm = FakeLLM()
    agent = Agent(llm, sandbox_factory=create_sandbox)
    task = TaskStore().create("say hello")
    await agent.run_task(task)

    assert task.status == "done", task.error
    assert [s["title"] for s in task.plan] == ["Write script", "Run it"]
    kinds = [e["kind"] for e in task.executions]
    assert kinds == ["write_file", "run_command"]
    run = task.executions[1]
    assert run["exit_code"] == 0 and "hello from sandbox" in run["stdout"] and len(run["stdout_sha256"]) == 64
    assert any(a["path"] == "hello.py" for a in task.artifacts)
    assert task.verification["passed"] is True
    assert task.verification["commands_run"] == 1 and task.verification["files_written"] == 1
    assert task.usage["llm_calls"] == llm.calls and task.usage["prompt_tokens"] > 0
    # path traversal from the model was refused, not executed
    assert any("escapes workspace" in r for r in llm.seen_tool_results)
    types = [e["type"] for e in task.events]
    assert "sandbox" in types and types.index("sandbox_destroyed") > types.index("verification")
    assert not os.path.isdir(task.sandbox["root"])  # sandbox torn down
    assert task.workspace_tar and task.workspace_tar[:1]


@pytest.mark.asyncio
async def test_command_timeout_is_enforced(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "sandbox_cmd_timeout_s", 1)
    sb = create_sandbox()
    await sb.start()
    try:
        res = await sb.run("sleep 5; echo never")
        assert res.timed_out and res.exit_code == 137 and "never" not in res.stdout
    finally:
        await sb.destroy()


@pytest.mark.asyncio
async def test_planner_failure_marks_task_failed_and_no_sandbox_leaks():
    class BadLLM:
        model = "bad"
        async def chat(self, *a, **k):
            return LLMReply(content="I cannot plan this.")
    task = TaskStore().create("anything")
    await Agent(BadLLM()).run_task(task)
    assert task.status == "failed" and "ValueError" in task.error
    assert task.sandbox == {}  # never provisioned
