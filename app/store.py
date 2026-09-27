"""In-memory task registry with a per-task async event log (fan-out to SSE subscribers)."""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Task:
    id: str
    prompt: str
    status: str = "queued"  # queued | planning | running | verifying | done | failed
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    plan: list[dict[str, Any]] = field(default_factory=list)
    executions: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    verification: dict[str, Any] | None = None
    sandbox: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, int] = field(default_factory=lambda: {"prompt_tokens": 0, "completion_tokens": 0, "llm_calls": 0})
    workspace_tar: bytes | None = None
    error: str | None = None
    _subscribers: list[asyncio.Queue] = field(default_factory=list, repr=False)

    def emit(self, kind: str, **data: Any) -> dict[str, Any]:
        event = {"seq": len(self.events), "t": round(time.time() - self.created_at, 3), "type": kind, **data}
        self.events.append(event)
        for q in list(self._subscribers):
            q.put_nowait(event)
        return event

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        for ev in self.events:  # replay history for late joiners
            q.put_nowait(ev)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id, "prompt": self.prompt, "status": self.status, "created_at": self.created_at,
            "finished_at": self.finished_at, "steps": len(self.plan), "executions": len(self.executions),
            "verified": (self.verification or {}).get("passed"), "error": self.error,
        }

    def report(self) -> dict[str, Any]:
        return {
            **self.summary(), "plan": self.plan, "sandbox": self.sandbox, "executions": self.executions,
            "artifacts": self.artifacts, "verification": self.verification, "usage": self.usage,
        }


class TaskStore:
    def __init__(self, max_tasks: int = 200):
        self._tasks: dict[str, Task] = {}
        self._max = max_tasks

    def create(self, prompt: str) -> Task:
        task = Task(id=uuid.uuid4().hex[:10], prompt=prompt)
        self._tasks[task.id] = task
        if len(self._tasks) > self._max:  # drop oldest finished
            for tid, t in list(self._tasks.items()):
                if t.status in ("done", "failed"):
                    del self._tasks[tid]
                    break
        return task

    def get(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    def all(self) -> list[Task]:
        return sorted(self._tasks.values(), key=lambda t: t.created_at, reverse=True)


store = TaskStore()
