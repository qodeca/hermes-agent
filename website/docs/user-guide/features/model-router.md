---
title: "Model Router"
description: "Route tasks to a light, standard, or heavy model tier by task complexity — off by default, never overrides an explicit model choice"
---

# Model Router

The model router picks a model **tier** — light, standard, or heavy — per task at conversation start, so a one-line reminder doesn't run on the same heavyweight model as a multi-hour research job. It is **off by default** (`routing.enabled: false`).

:::info Not the same thing as Provider Routing
This page is about choosing *which model* handles a task based on its complexity. [Provider Routing](/user-guide/features/provider-routing) is a different feature: it controls which OpenRouter *upstream provider* serves a request for a model you already chose. The two are independent and can be combined.
:::

## The contract: fills only the global-default gap

The router **never overrides an explicit model choice**. Resolution precedence, highest first:

1. Explicit per-job / per-call model (e.g. a cron job's pinned `provider`/`model`, a `delegate_task` override)
2. `HERMES_INFERENCE_MODEL` env override
3. Session `/model` switch or channel override
4. **The router** (only when enabled and the task's origin is in `apply_to`)
5. Global default from `hermes model`

In other words: the router only fills the gap where the global default would have applied anyway. Anything you pinned stays pinned.

The router is also **fail-open**: it never raises, and any error in classification or tier resolution results in a no-op (the task runs on whatever model it would have used without routing). A tier whose `provider`, `model`, and `base_url` are all empty is a no-op for that tier — the expected steady state is to configure only `light` (and optionally `heavy`), leave `standard` empty, and let the router be a pure delta on today's behaviour.

## Where it applies

`routing.apply_to` selects which conversation origins are routed. Anything not listed keeps its existing model resolution untouched. Four surfaces are supported:

| Surface | Meaning |
|---------|---------|
| `cron` | Scheduled job runs (unpinned jobs only — a job with an explicit `provider`/`model` always wins) |
| `delegate` | Subagent tasks spawned via `delegate_task` |
| `gateway` | New messaging-gateway conversations |
| `oneshot` | One-shot CLI runs (`hermes -z` / `hermes chat`) |

The default is `[cron, delegate]` — the unattended surfaces where nobody is around to `/model`-switch by hand.

## Configuration

```yaml
routing:
  enabled: false                # off by default
  apply_to: [cron, delegate]    # cron | delegate | gateway | oneshot
  classifier: heuristic         # heuristic | llm | off
  default_tier: standard        # tier used when no heuristic is decisive (or the classifier fails)
  tiers:
    light:    {provider: "", model: "", base_url: ""}
    standard: {provider: "", model: "", base_url: ""}
    heavy:    {provider: "", model: "", base_url: ""}
  heuristics:
    light_max_chars: 280        # task text ≤ this many chars reads as light
    heavy_min_chars: 4000       # task text ≥ this many chars reads as heavy
    light_keywords: [remind, greet, ping, notify, send a message]
    heavy_keywords: [research, investigate, comprehensive, deep dive, migrate, refactor, crawl]
    heavy_toolsets: [browser, delegation]
```

### Classifier modes

- `heuristic` (default) — deterministic rules only: task-text length thresholds, keyword lists, and attached toolsets. No LLM call, no added latency or cost.
- `llm` — heuristics run first; when they aren't decisive, a short auxiliary LLM call breaks the tie. Configure the backend under `auxiliary.routing` (same shape as `auxiliary.compression`: `provider`, `model`, `base_url`, `api_key`, `timeout`, `extra_body`).
- `off` — no classification; every routed task gets `default_tier`.

```yaml
auxiliary:
  routing:
    provider: openrouter
    model: google/gemini-3-flash-preview
```

### Heuristic tuning

`heavy_toolsets` marks a task heavy outright when one of the listed toolsets is attached. Note that gateway platforms commonly attach `browser`, so the default list will floor most gateway sessions at heavy if you enable `gateway` routing — tune it down for gateway use if that's not the trade-off you want.

## Minimal example

Route short cron reminders to a cheap model, leave everything else exactly as it is today:

```yaml
routing:
  enabled: true
  apply_to: [cron]
  tiers:
    light:
      provider: openrouter
      model: google/gemini-3-flash-preview
    # standard and heavy left empty → no-op, existing resolution kept
```

## See also

- [Scheduled Tasks (Cron)](/user-guide/features/cron) — the main routed surface
- [Subagent Delegation](/user-guide/features/delegation) — per-call `delegation.*` model overrides (which always win over the router)
- [Provider Routing](/user-guide/features/provider-routing) — OpenRouter upstream-provider selection (a different feature)
- [Auxiliary Models](/user-guide/configuration#auxiliary-models) — the `auxiliary.*` slot system the LLM classifier plugs into
