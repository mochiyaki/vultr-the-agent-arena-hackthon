# Blast Radius Zero

**Safe agent execution on Vultr.** A web-based agent that does real work (writes and runs code)
where every action happens inside a throwaway, hardened sandbox on a Vultr VM. The control plane
plans the task with **Vultr Serverless Inference**, dispatches each action to an isolated
container, streams the evidence live to the browser, has an independent verifier judge the
result from that evidence only, and then destroys the sandbox.

> Built for the Vultr "The Agent Arena" hackathon, problem statement *Blast Radius Zero*.

```
 Browser ──HTTP/SSE──▶ Control plane (FastAPI, Vultr VM)
                          │  plan / act / verify
                          ├──────────────▶ Vultr Serverless Inference  (all LLM calls)
                          │  docker API (sibling containers, never in-process)
                          └──▶ Sandbox container per task
                                 gVisor runsc · network=none · read-only rootfs
                                 tmpfs /workspace · cap-drop ALL · no-new-privileges
                                 user nobody · mem/cpu/pid limits · per-command KILL timeout
                                 ──▶ destroyed when the task ends
```

## What it does

1. **Plan.** The user describes a task. The planner (Vultr inference) splits it into 1–6 concrete steps, each with success criteria.
2. **Provision.** A fresh container is created for the task with the containment policy below.
3. **Execute.** For each step, the executor model works through four mediated tools:
   `run_command`, `write_file`, `read_file`, `finish_step`. The model never touches anything
   directly. Every command is recorded with exit code, duration, stdout/stderr and a SHA-256 of stdout.
4. **Verify.** An independent verifier prompt receives only *evidence* (commands, outputs, hashed
   artifact manifest) and returns pass/fail with cited evidence. Deterministic counters (commands
   run/failed, files written) are attached regardless of what the model says.
5. **Destroy.** The container is force-removed. The workspace tarball and a JSON report remain
   downloadable from the control plane.

Everything streams to the UI in real time over Server-Sent Events.

## Containment policy (defaults)

| Control | Setting | Why |
|---|---|---|
| Runtime | gVisor `runsc` (falls back to `runc` with a visible warning) | user-space kernel; syscalls never hit the host kernel directly |
| Network | `none` | no egress, no lateral movement, no exfiltration |
| Root filesystem | read-only | code cannot persist or tamper with the image |
| Workspace | tmpfs, 64 MB, `noexec,nosuid`, owned by nobody | bounded, disappears with the container |
| Capabilities | all dropped, `no-new-privileges` | no privilege escalation path |
| User | `65534:65534` (nobody) | never root |
| Resources | 512 MB RAM (no swap), 1 CPU, 128 PIDs | no fork bombs, no memory exhaustion |
| Time | 60 s per command (`timeout -s KILL` inside the sandbox), 600 s per task (`sleep TTL` as PID 1) | runaway code is killed even if the control plane dies |
| Paths | every model-supplied path is normalised and must stay under `/workspace` | blocks `../` traversal |
| Output | stdout/stderr capped at 16 KB per command, export capped at 20 MB | bounded blast radius on the control plane too |
| Lifecycle | one container per task, force-removed in `finally` | nothing survives the task |

The policy is a frozen dataclass (`app/sandbox/base.py`) that is echoed verbatim into every task's
audit record and shown in the UI.

## Vultr is the system of control

* **Compute:** the control plane and all sandboxes run on a Vultr Cloud Compute VM (`deploy/setup-vultr.sh`).
* **Inference:** every LLM call (planner, executor, verifier) goes through `https://api.vultrinference.com/v1`
  (`app/llm.py`). The model is selectable; `/api/models` lists the tool-capable models live.
* **Orchestration:** the VM's Docker daemon is the sandbox scheduler; the control plane only speaks to it
  through the Docker API. The UI shows the Vultr instance id and region from the instance metadata service.

## Deploy on Vultr (about 5 minutes)

1. Create a **Cloud Compute** instance, Ubuntu 24.04, 2 vCPU / 4 GB is plenty.
   (Optional: paste `deploy/cloud-init.yaml` as user data after pointing it at your repo.)
2. Bootstrap Docker + gVisor and build the images:
   ```bash
   ssh root@<vultr-ip> 'REPO_URL=https://github.com/<you>/blast-radius-zero.git bash -s' < deploy/setup-vultr.sh
   ```
3. Configure and start:
   ```bash
   ssh root@<vultr-ip>
   cd /opt/blast-radius-zero
   nano .env            # set VULTR_INFERENCE_API_KEY (and optionally VULTR_INFERENCE_MODEL)
   docker compose up -d
   ```
4. Open `http://<vultr-ip>/`. The header badges should read **docker**, **gVisor runsc**, **net: none**
   and show your Vultr region.

### Verify containment on the VM

```bash
# while a task is running:
docker ps --filter label=brz.sandbox=true
docker inspect <id> --format '{{.HostConfig.Runtime}} {{.HostConfig.NetworkMode}} {{.HostConfig.ReadonlyRootfs}} {{.HostConfig.CapDrop}}'
# -> runsc none true [ALL]
```
Then run the built-in example task *"Try to reach the internet with curl and python…"*: the
commands fail inside the sandbox, the verifier reports the failure honestly, and the container is gone afterwards.

## Configuration

All settings are environment variables; see `.env.example`.

| Variable | Default | Meaning |
|---|---|---|
| `VULTR_INFERENCE_API_KEY` | – | **required** |
| `VULTR_INFERENCE_MODEL` | `deepseek-v4.1-flash` | any tool-capable model from `/api/models` (e.g. `glm-5.3`, `qwen3.8-27b`) |
| `SANDBOX_DRIVER` | `docker` | `docker` in production. `unsafe_local` exists only for tests and requires `ALLOW_UNSAFE_LOCAL_SANDBOX=1`; the UI shows a red banner when it is active. |
| `SANDBOX_RUNTIME` | `runsc` | gVisor; falls back to runc if not installed |
| `SANDBOX_NETWORK` | `none` | set to `bridge` only if a task genuinely needs egress |
| `SANDBOX_MEMORY` / `SANDBOX_CPUS` / `SANDBOX_PIDS` | `512m` / `1.0` / `128` | resource caps |
| `SANDBOX_CMD_TIMEOUT_S` / `SANDBOX_TASK_TTL_S` | `60` / `600` | time caps |
| `AGENT_MAX_STEPS` / `AGENT_MAX_ACTIONS_PER_STEP` | `6` / `8` | agent budget |
| `MAX_CONCURRENT_TASKS` | `3` | sandboxes running at once |

## API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/tasks` `{prompt}` | submit a task, returns `{id}` |
| `GET` | `/api/tasks/{id}/events` | SSE stream: `plan`, `sandbox`, `exec_start`, `exec_done`, `file_written`, `verification`, `sandbox_destroyed`, … |
| `GET` | `/api/tasks/{id}` | full JSON report (plan, executions with hashes, artifacts, verdict, token usage) |
| `GET` | `/api/tasks/{id}/workspace.tar` | the sandbox workspace as produced |
| `GET` | `/api/system` | driver, gVisor availability, policy, Vultr instance metadata |
| `GET` | `/api/models` | tool-capable models on Vultr Serverless Inference |

## Local development

```bash
pip install -r requirements.txt
make test                 # 14 tests: policy, path traversal, timeouts, full plan→execute→verify loop with a fake LLM
cp .env.example .env      # add your key; with Docker installed, SANDBOX_DRIVER=docker works locally too
make sandbox-image && make dev
```

## Project layout

```
app/main.py              FastAPI control plane, SSE, downloads
app/agent.py             planner / executor (tool loop) / verifier
app/llm.py               Vultr Serverless Inference client (OpenAI-compatible, tool calling)
app/sandbox/base.py      Sandbox contract, SandboxPolicy, path safety
app/sandbox/docker_sandbox.py   hardened per-task container driver (gVisor)
app/sandbox/local_sandbox.py    dev-only driver, no containment
app/store.py             task registry + event fan-out
app/static/index.html    single-page UI
sandbox/Dockerfile       sandbox image (python 3.12, numpy/pandas/matplotlib, node)
deploy/                  Vultr VM bootstrap (Docker + gVisor) and cloud-init
tests/                   pytest suite
```

## Known limits / next steps

* Task state is in memory; restarting the control plane forgets past tasks (reports can be persisted to Vultr Object Storage next).
* One VM hosts both control plane and sandboxes. The `Sandbox` interface is driver-based, so a next step is a driver that provisions a throwaway Vultr instance per task via the Vultr API for hard VM-level isolation.
* Browser automation is not included in this version; a Playwright-equipped sandbox image would plug into the same interface.
