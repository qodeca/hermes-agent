---
title: "Operator Alerts"
description: "Route out-of-band operational warnings — interrupted cron runs, high-severity audit findings, curator aborts — to a chat you watch"
---

# Operator Alerts

Operator alerts are Hermes' out-of-band channel for "the operator should know about this" events — things that would otherwise only appear as a log line on a headless server. Instead of grepping `gateway.log` after the fact, you get a message in a chat you actually watch.

**Off by default.** Alerts are enabled by setting a delivery target:

```yaml
# ~/.hermes/config.yaml
alerts:
  deliver: "telegram:123456789"   # platform:chat_id — empty = disabled (default)
```

`alerts.deliver` accepts the same target format as a cron job's `deliver` field (see the [cron delivery options](/user-guide/features/cron#delivery-options)) — `platform:chat_id`, e.g. `telegram:123456789`, `discord:#ops`, `slack:C0123ABCD45`. When it is empty, alerting is a no-op.

## What emits an alert

| Source | When |
|--------|------|
| **Cron startup reconciliation** | The scheduler restarted mid-run and orphaned runs were marked `error` at boot — one alert per reconciled job. See [restart recovery](/user-guide/features/cron#restart-recovery-orphaned-runs). |
| **Security audit (high severity)** | The startup security audit found high-severity issues (SSH password auth enabled, network listener without auth) — one combined alert for all high findings. An active allow-all flag also triggers a startup alert listing the open platforms. See [Security](/user-guide/security#security-audit-severity-and-operator-alerting). |
| **Curator denial breaker** | A background curator review was aborted after repeated denied privileged attempts. See [Curator guardrails](/user-guide/features/curator#guardrails-on-background-runs). |

## Delivery semantics

- **Fire-and-forget.** Alerts ride the existing cron delivery machinery and never block or fail the operation that emitted them — a broken alert target can't take down a cron run or gateway startup.
- **Never raises.** Any error in alert delivery is swallowed (and logged); alerting is strictly best-effort.
- **Rate-limited.** Identical alert titles are limited to one delivery per 15 minutes, so a flapping condition can't flood your chat.
- **Severity-tagged.** Each alert carries a severity (default `warning`); the emitting subsystem decides the level.

## Relationship to cron delivery

Operator alerts and cron job deliveries share plumbing but are independent: `alerts.deliver` is a single global target for operational events, while each cron job's `deliver` field controls where that job's *output* goes. You can point them at the same chat or keep an `#ops`-style channel for alerts only.

## See also

- [Scheduled Tasks (Cron)](/user-guide/features/cron) — run limits, restart recovery, and delivery targets
- [Security](/user-guide/security) — the startup audit and authorization model
- [Curator](/user-guide/features/curator) — the background review the denial breaker protects
