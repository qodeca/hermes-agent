# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Authoritative guide

`AGENTS.md` is the full, canonical development guide (~70KB). It covers the contribution rubric, the "footprint ladder" for adding capability, plugin/skill authoring, profiles, cron, and dozens of hard-won gotchas. **Read the relevant section of `AGENTS.md` before non-trivial work** — this file is only the fast index.

## Fork-specific docs (Qodeca)

This is the `qodeca/hermes-agent` fork. Two docs cover fork-only setup — read them for anything touching the live server or the fork's GitHub state:

- `docs/hermes-server-setup.md` — running this checkout as an always-on server (launchd dashboard + gateway, Tailscale Serve HTTPS, deploy/restart discipline).
- `docs/qodeca-fork-github-config.md` — fork relationship, the upstream-sync procedure, and fork CI state (enabled but dormant — verify locally).

## Two invariants that govern almost every change

1. **Per-conversation prompt caching is sacred.** A long-lived conversation reuses a cached prefix every turn. Anything that mutates past context, swaps toolsets, or rebuilds the system prompt mid-conversation invalidates that cache and multiplies user cost. The system prompt must be byte-stable for the life of a conversation. The one exception is context compression.
2. **The core is a narrow waist; capability lives at the edges.** Every model tool ships on every API call, so new *core tools* are the expensive last resort. Prefer, in order: extend existing code → CLI command + skill → service-gated tool (`check_fn`) → plugin → MCP server in the catalog → new core tool. The product is expansive at the edges (platforms, providers, TUI/desktop) and conservative at the waist.

Also preserve strict message role alternation (never two same-role messages in a row; no synthetic user message injected mid-loop).

## Commands

This is a hybrid Python + TypeScript monorepo (Python core; npm workspaces for `apps/*`, `ui-tui`, `web`).

**Tests — always use the wrapper, never bare `pytest`:**
```bash
scripts/run_tests.sh                                  # full suite, CI-parity
scripts/run_tests.sh tests/gateway/                   # one directory
scripts/run_tests.sh tests/agent/test_foo.py::test_x  # one test
scripts/run_tests.sh -v --tb=long                     # pass-through pytest flags
```
The wrapper spawns each test file in its own subprocess (no module-level leakage), unsets credential env vars, and forces `TZ=UTC` / `LANG=C.UTF-8`. Bare `pytest` on a machine with API keys set diverges from CI. Tests must run against a temp `HERMES_HOME` and must never write to the real `~/.hermes/`.

**Python lint / typecheck** (tools pinned in the `dev` extra):
```bash
ruff check .    # blocking; only PLW1514 (unspecified-encoding) is enforced
ty check        # typechecker (Astral `ty`)
```
`ruff` is intentionally near-fully-disabled except `PLW1514` — always pass `encoding=...` to `open()`/`read_text()`/`write_text()` (silent corruption on Windows otherwise).

**Dev install** (work from the full checkout at `$HERMES_HOME/hermes-agent`, usually `~/.hermes/hermes-agent`; create the venv *outside* the source tree):
```bash
uv pip install -e ".[all,dev]"
```

**TUI dev** (`ui-tui/`, Ink/React):
```bash
cd ui-tui && npm run dev   # watch mode
npm run build              # full build
npm run typecheck          # tsc --noEmit
npm run lint               # eslint
```

## Architecture

Python core runs the same agent loop across a CLI, a messaging gateway, a TUI, and an Electron desktop app.

**Load-bearing entry points** (large god-files — refactoring clusters out of them is welcomed work):
| File | Role |
|------|------|
| `run_agent.py` | `AIAgent` class — synchronous core conversation loop (`run_conversation()`), ~12k LOC |
| `model_tools.py` | Tool orchestration: `discover_builtin_tools()`, `handle_function_call()` |
| `toolsets.py` | Toolset definitions, `_HERMES_CORE_TOOLS` list |
| `cli.py` | `HermesCLI` — interactive CLI orchestrator, `process_command()`, ~11k LOC |
| `hermes_state.py` | `SessionDB` — SQLite session store with FTS5 search |
| `hermes_constants.py` | `get_hermes_home()` — profile-aware paths |

**Tool import chain** (order matters):
```
tools/registry.py  (no deps)
   ↑ tools/*.py  (each calls registry.register() at import time)
   ↑ model_tools.py  (imports registry + triggers discovery)
   ↑ run_agent.py, cli.py, batch_runner.py
```
Tools are auto-discovered — add a file under `tools/` that calls `registry.register()`. Terminal backends (local, docker, ssh, modal, daytona, singularity) live in `tools/environments/`.

**Agent loop:** entirely synchronous, inside `run_conversation()`. Loops while `api_call_count < max_iterations` and budget remains, checking for interrupts each turn. Messages are OpenAI format (`system`/`user`/`assistant`/`tool`); reasoning goes in `assistant_msg["reasoning"]`. Guarded by the conversation circuit breaker (`agent.max_consecutive_api_failures`, aborts after consecutive backend failures) and `agent.session_output_token_budget` (cumulative output-token cap per session); `agent/degeneration_detector.py` (`agent.degeneration_detection`) catches repetition loops.

**Model router** (`agent/model_router.py`, off by default via `routing.enabled`): pure task-complexity classifier that picks a model tier for cron/delegate calls.

**Operator alerts** (`hermes_cli/operator_alerts.py`): fire-and-forget notifications over the cron delivery machinery, gated on `alerts.deliver`.

**Slash commands** are defined once in `hermes_cli/commands.py` (`COMMAND_REGISTRY` of `CommandDef`). CLI dispatch, gateway dispatch, `/help`, Telegram menu, Slack routing, and autocomplete all derive from it automatically. To add one: add a `CommandDef`, then a handler branch in `HermesCLI.process_command()` (`cli.py`) and, if gateway-available, in `gateway/run.py`.

**Gateway** (`gateway/`): one process serves ~20 platforms via `gateway/platforms/<name>/` adapters (`run.py` + `session.py`). See `gateway/platforms/ADDING_A_PLATFORM.md`.

**TUI** (`ui-tui/` Ink/React + `tui_gateway/` Python): TypeScript owns the screen; Python owns sessions/tools/model calls. They talk over newline-delimited JSON-RPC on stdio (`tui_gateway/server.py` is the method/event catalog).

**Plugins** (`plugins/`): discovered at runtime, live in their own directories, and must work within provided ABCs/hooks — plugins must not touch core files. Categories include `memory/`, `model-providers/`, `context_engine/`, `image_gen/`. Third-party/vendor-product plugins do **not** land in this tree — they ship as standalone repos users install into `~/.hermes/plugins/`.

**Skills**: `skills/` (bundled, active by default) vs `optional-skills/` (heavy/niche, installed via `hermes skills install`). SKILL.md `description` must be ≤ 60 chars, one sentence, ending with a period.

**Cron** (`cron/jobs.py` + `cron/scheduler.py`): natural-language scheduled jobs with multi-platform delivery; agents schedule via the `cronjob` tool, users via `hermes cron`. Cron sessions are bounded by an inactivity timeout (`HERMES_CRON_TIMEOUT`, default 600s, resets on activity) plus a wall-clock runtime cap (`HERMES_CRON_MAX_RUNTIME` / `cron.max_runtime_seconds`, default 3600s, bounds total runtime regardless of activity); also covers startup reconciliation of orphaned runs, per-job `misfire_deadline_seconds` (skip a late fire instead of running it stale), and per-run `last_run_stats` persisted on `mark_job_run()`.

**Config vs secrets:** behavioral settings live in `~/.hermes/config.yaml`, including the top-level `alerts` and `routing` sections. `~/.hermes/.env` is for credentials only — do **not** add `HERMES_*` env vars for non-secret config; the one exception is `HERMES_MACHINE_ID`, which scopes cron reap/reconciliation when running >1 instance on a host. Logs: `~/.hermes/logs/` (`agent.log`, `errors.log`, `gateway.log`); browse with `hermes logs`.

## Testing conventions

- **No change-detector tests.** Never assert a snapshot of data expected to change (model catalog contents, `_config_version` literals, enumeration counts). Assert invariants/relationships instead (e.g. "every catalog model has a context-length entry"), not specific names. Reviewers reject snapshot tests.
- **E2E over green mocks** for anything touching resolution chains, config propagation, security boundaries, remote backends, or file/network I/O — exercise the real path against a temp `HERMES_HOME`.

## Profiles

The runtime supports multiple isolated instances (profiles). Never hardcode `~/.hermes/` — use `get_hermes_home()` / profile-aware helpers so code stays profile-safe. Profiles are independent islands *by design*; don't add cross-profile config inheritance.
