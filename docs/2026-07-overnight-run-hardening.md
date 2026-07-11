# 2026-07 overnight-run hardening

**Date:** July 2026
**Merged:** `1161dc765..08f694a8c` on `main` — 63 commits, fast-forward of 11 stacked slices (PR0–PR12).

One failed overnight cron run exposed silent-failure gaps across scheduling, the agent loop, observability, security, and model selection; a five-lens review turned it into 31 findings, all of which are addressed (or explicitly deferred) by this batch. This is the narrative record for future maintainers asking "why does cron have a wall-clock cap", "what is `running_marker`", or "when did the model router land".

## The incident

An unattended overnight run failed on every axis at once — and nothing surfaced any of it:

- **Wrong schedule.** A "tonight at 2am" request was stored as a *yearly* cron expression (`0 2 10 7 *` shape): a fully date-pinned 5-field cron whose semantics are "every year on that date", not "once".
- **Runaway research job.** The 2am research job ran ~2.4 hours against a memory-starved local model. The backend repeatedly failed with `prefill_memory_exceeded`, which the loop treated as a generic retryable error — including one API call that hung for 46 minutes, and a delegation that fell back to a synchronous inline run and blocked the parent for 58 minutes.
- **Zero trace.** The job died when the gateway went down. No status was recorded, no output survived, no per-run stats existed — from the job record it looked like the run never happened.
- **Late, degenerate greeting.** A 06:00 greeting job fired 3h45m late (the scheduler fires once past grace rather than skipping) and then burned 261 KB of stored output in repetition/degeneration loops before anything stopped it.
- **Silence.** No alert reached the operator for any of the above. Every failure mode was discoverable only by manually reading logs the next day.

## The review

The incident triggered a researched, five-lens review of the codebase — scheduling reliability, agent-loop resilience, observability, security, and model operations. It produced **31 findings**: 7 must-fix, 14 should-fix, 9 nice-to-fix, 1 cosmetic. All must-fix and should-fix items landed in this batch; the remainder landed where cheap or moved to the deferred list at the end of this document.

## Findings → fixes

Grouped by area. Each row states the problem in plain terms and the mechanism that now handles it (module and/or config knob). The rows below cover the 31 findings; the row count is lower than 31 because several findings share one fix — for example, operator alerting closes multiple "nothing told anyone" findings across areas, and the model router closes the full model-operations cluster.

### Cron reliability

| Problem | Fix |
|---|---|
| "Tonight at 2am" stored as a yearly cron — fires next July, not tonight | Date-pinned cron → one-shot conversion: a fully date-pinned 5-field expression whose next occurrence is ≤31 days away is stored as `kind="once"` (DST-safe naive-base anchoring); a far-out one stays recurring (legitimate yearly job) but the create path returns a notice steering one-time runs to an ISO timestamp |
| A run could exceed any sane duration as long as it kept producing activity | Wall-clock cap per run: `cron.max_runtime_seconds` (default 3600, `0` = unlimited; env `HERMES_CRON_MAX_RUNTIME` wins) — sibling of the pre-existing inactivity timeout `HERMES_CRON_TIMEOUT` (600 s) |
| A run killed mid-flight (gateway death, restart) left zero trace | `running_marker` (`{"at": iso, "by": machine_id}`) stamped durably at fire time for recurring jobs, plus `last_status="running"`; at boot, startup reconciliation marks orphaned runs (TTL-expired or own-host marker/stale one-shot claim) as `error` "interrupted: scheduler restarted mid-run" and emits one operator alert per reconciled job. `HERMES_MACHINE_ID` scopes reaping so co-hosted instances don't reap each other's live runs |
| A morning job firing hours late is worse than not firing | Opt-in per-job `misfire_deadline_seconds`: a recurring fire more than N seconds late is skipped (fast-forward kept, `last_status="skipped_stale"`, one-line notice delivered). Unset preserves the deliberate fire-once-past-grace behaviour; `skipped_stale` renders distinctly in `hermes cron list` |
| No per-run record of what a job actually did | `last_run_stats` persisted per run (`started_at`, `ended_at`, `duration_s`, `exec_duration_s`, `api_calls`, `output_tokens`, `exit_reason`) plus a `cron.run_summary` INFO line emitted independent of delivery |
| Stored run output was unbounded (the 261 KB greeting) | `cron.output_max_bytes` (default 262144) caps the stored output file — head 60% + tail 30% kept, middle elided with a marker; delivered chat text is separate |
| No cron-specific loop bound | `cron.max_iterations` (default 40; resolution: per-job (future) > `cron.max_iterations` > `agent.max_turns` > 40; non-positive means *unset*, not unlimited) and `cron.session_output_token_budget` (default 200000, lower than interactive because cron runs are single-shot) |

### Agent-loop resilience

| Problem | Fix |
|---|---|
| `prefill_memory_exceeded` treated as retryable → endless compress-and-resubmit against a memory-starved backend | Backend-capacity error class: `FailoverReason.backend_capacity` (`prefill_memory_exceeded`, memory-guard, kv-cache-exceeded) classified `retryable=False, should_compress=False`, and classified *before* the generic-400 heuristic — a capacity 400 aborts cleanly |
| One API call hung for 46 minutes | Request-timeout floor: model clients are never built without a timeout; Anthropic client-build sites resolve through `_resolved_api_call_timeout()` (config → `HERMES_API_TIMEOUT` → 1800 s), and non-stream stale timeouts have a finite ceiling even for reasoning models |
| Repeated backend failures kept the loop alive indefinitely | Conversation circuit breaker: `agent.max_consecutive_api_failures` (default 10, `0` = disabled) aborts the loop after N consecutive backend failures, counting across provider-fallback resets, resetting on any success |
| Degeneration loops ran unchecked (the 261 KB of repetition) | `agent/degeneration_detector.py` — pure `looks_degenerate()`: 8-gram overlap >60% vs the previous 3 assistant turns, or the same normalized line ≥5× in the newest text. Two-strike integration: first strike steers via the next tool result (role-safe), second consecutive strike exits `degeneration_detected`. On by default for cron (`cron.degeneration_detection: true`), off for interactive (`agent.degeneration_detection: false` — interactive users see and stop loops themselves) |
| No cumulative output bound for a session | `agent.session_output_token_budget` (default `0` = off; cron sets 200000) ends the loop cleanly via a final grace call when exceeded; `agent.default_max_tokens` (default `0` = provider default) bounds per-call output when `agent.max_tokens` is unset |
| Context compression itself called the failing backend, compounding the failure | Compression skips a failing backend: if the summary call fails with a capacity/overloaded error (or the trigger was a backend fault), compression aborts and *preserves* messages instead of a lossy static drop; `auxiliary.compression.provider/model` is the independent-summarizer escape hatch |
| Async-pool-full delegation fell back to an unbounded synchronous inline run (58 minutes) | Bounded capacity fallback: `delegation.sync_fallback_timeout_seconds` (default 600); on expiry the children are interrupted, completed-sibling results are salvaged, and a late-returning orphan skips all parent side effects |

### Observability & alerting

| Problem | Fix |
|---|---|
| Nothing alerted anyone about any of it | `hermes_cli/operator_alerts.py` — `send_operator_alert()`, fire-and-forget over the existing cron delivery machinery, never raises, 15-minute identical-title rate limit, no-op when `alerts.deliver` (a `platform:chat_id` target, default `""` = off) is unset. Consumed by cron startup reconciliation, the security-audit high-severity path, and the curator denial breaker — one mechanism closing the "silent failure" findings in three areas |
| MCP keepalive reconnect failures spammed the log every cycle | Reconnect dwell + log rollup: 300 s (`_PARKED_RETRY_INTERVAL`) dwell between failed cycles (explicit `/mcp` refresh bypasses); repeated identical failures log the first at WARNING, then DEBUG, then a periodic rollup and one recovery line |
| Tool-availability `check_fn` results logged every turn | Transition-only logging: WARNING only on a True→False transition, INFO on recovery |

### Security

| Problem | Fix |
|---|---|
| Curator background reviews could persist injected content into memory/skills | `tools/curator_write_guard.py` scans every curator-originated memory/skill write (add/replace/batch, skill create/edit/patch/write_file) for injection patterns before persistence, gated on `is_background_review()`; a hit drops the write and logs a WARNING. Interactive writes unaffected |
| A compromised background review could hammer privileged operations | Curator denial breaker: a background review aborts after N (default 5) denied privileged attempts — counting both whitelist blocks and in-tool guard refusals — and emits one operator alert. (Per-run scope; cross-run persistence is deferred, see below) |
| One global `GATEWAY_ALLOW_ALL_USERS` flag opened *every* platform | Per-platform `<PLATFORM>_ALLOW_ALL_USERS` (e.g. `TELEGRAM_ALLOW_ALL_USERS`); any active allow-all triggers a startup operator alert listing the open platforms; the global flag still works but is deprecated with a startup WARNING naming the per-platform alternative |
| Startup security audit findings were all equally quiet log lines | Audit checks now return `(severity, message)`; SSH-password-auth and network-listener-without-auth are `high`, and high findings emit one combined operator alert. Log lines unchanged; no fail-closed gate |
| The agent could `hermes gateway restart`/`stop` its own host process | Restart-guard process ancestry: the commands refuse if `_HERMES_GATEWAY` is set or the caller's process ancestry contains the recorded gateway pid (`~/.hermes/gateway.pid`); any error in the walk falls back to the env check |
| No way to see the effective authorization surface | `/allowlist show` — admin-gated slash command reporting effective authorization sources (env allowlists per platform, group allowlists, bot policy, paired users, allow-all flags); the local interactive CLI path is ungated |

### Model operations

| Problem | Fix |
|---|---|
| A heavy research task was sent to a memory-starved local model — the root of the 2.4 h death spiral | `agent/model_router.py` — pure task-complexity router: `route_model()` picks a light/standard/heavy tier per task using heuristics (length thresholds, keywords, toolsets) with an optional LLM tie-break classifier (`auxiliary.routing`). Fail-open (never raises, no-op on any error), read-only catalog check, applies to cron and delegate origins by default (`routing.apply_to`). **Off by default** (`routing.enabled: false`; tiers empty = no-op). One fix closing the model-operations finding cluster |

## Config-knob quick reference

Defaults verified against `hermes_cli/config.py` DEFAULT_CONFIG at `08f694a8c`. `alerts` and `routing` are new registered top-level sections.

| Key | Default | Meaning |
|---|---|---|
| `agent.max_consecutive_api_failures` | `10` | Abort the loop after N consecutive backend API failures (`0` = disabled) |
| `agent.session_output_token_budget` | `0` (off) | Cumulative output-token budget per session; clean grace-call exit on exceed |
| `agent.degeneration_detection` | `false` | Repetition detector for interactive sessions — off by default |
| `agent.default_max_tokens` | `0` (provider default) | Per-call `max_tokens` when `agent.max_tokens` is unset |
| `cron.max_runtime_seconds` | `3600` | Wall-clock cap per cron run (`0` = unlimited); env `HERMES_CRON_MAX_RUNTIME` wins |
| `cron.max_iterations` | `40` | Cron loop iteration cap; non-positive = unset (falls back), not unlimited |
| `cron.session_output_token_budget` | `200000` | Output-token budget for cron sessions |
| `cron.output_max_bytes` | `262144` | Byte cap on a run's stored output file (head/tail elision) |
| `cron.degeneration_detection` | `true` | Detector on for cron (the unattended surface); explicit `agent.degeneration_detection` wins |
| `alerts.deliver` | `""` (off) | `platform:chat_id` target for operator alerts |
| `routing.*` | `enabled: false` | Task-complexity model router (`apply_to`, `classifier`, `default_tier`, `tiers.*`, `heuristics.*`) — off by default |
| `auxiliary.routing` | (block) | Optional LLM tie-break classifier backend for the router (same shape as `auxiliary.compression`) |
| `delegation.sync_fallback_timeout_seconds` | `600` | Bound on the pool-full inline synchronous delegation fallback |

New/notable env vars:

| Env var | Meaning |
|---|---|
| `HERMES_CRON_MAX_RUNTIME` | Wall-clock cap; wins over `cron.max_runtime_seconds`; `0` = unlimited. Sibling of the pre-existing `HERMES_CRON_TIMEOUT` (inactivity, 600 s) |
| `HERMES_MACHINE_ID` | Scopes cron running-marker reaping (default `host:pid`); set distinct values when running more than one Hermes instance on a host |
| `<PLATFORM>_ALLOW_ALL_USERS` | Per-platform allow-all (e.g. `TELEGRAM_ALLOW_ALL_USERS`); replaces the deprecated global `GATEWAY_ALLOW_ALL_USERS` |

## Deferred follow-ups

Known gaps deliberately left out of this batch:

- **Interactive-CLI model routing** — the router applies to cron and delegate origins; the interactive CLI keeps its explicitly chosen model.
- **OpenTelemetry GenAI spans** — `last_run_stats` + `cron.run_summary` are the lightweight substitute for now.
- **A real delegation queue** — the bounded synchronous fallback is a stopgap; pool-full work should queue, not run inline.
- **A generic log-rate-limit filter** — the MCP rollup and 15-minute alert dedup are point solutions.
- **Curator denial breaker cross-run persistence** — the denial counter is per-run; a persistent counter across background reviews is a follow-up.
