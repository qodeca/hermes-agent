# Qodeca fork – GitHub configuration

> **Audience:** Qodeca maintainers of this fork
> **Scope:** Qodeca-specific fork setup, not part of upstream Hermes docs
> **Last updated:** 2026-07-09

## Overview

This repository is a **public fork**: `qodeca/hermes-agent`, forked from the original
`NousResearch/hermes-agent`. `main` is kept **in sync with upstream** and now also carries
**Qodeca-specific commits** – notably dashboard-auth security fixes and reverse-proxy
(HTTPS) support, plus the fork's own docs. It is no longer a pure mirror.

## State at a glance

| Aspect | State | Note |
|--------|-------|------|
| Fork relationship | `qodeca/hermes-agent` ← `NousResearch/hermes-agent` | Public, owned by the `qodeca` org |
| `main` vs upstream | 13 ahead / 0 behind `upstream/main` | 11 Qodeca commits + 2 merges (PR #1, upstream sync); re-synced with `upstream/main` on 2026-07-09 |
| Git remotes | `origin` = qodeca, `upstream` = NousResearch | `upstream` push URL is set to `DISABLE` (fetch-only) |
| Issues | **disabled** on the fork | Bug reports belong on the upstream repo |
| Branches | ~1300 | Inherited from upstream (Nous dev branches, e.g. `alice/…`, `atropos-…`); the fork mirrors them |
| GitHub Actions | **enabled, but dormant** | Actions are on, yet the fork has **0 workflow runs** — a push to `main` triggers nothing. Verify locally (see below) |
| Merge methods | merge / squash / rebase all allowed | Default GitHub settings |
| Auth (local) | `gh` via `GITHUB_TOKEN` | scopes: `repo`, `workflow`, `read:org`, `gist` |

## Qodeca commits on `main`

Security and feature work carried on top of upstream (all PR-quality, upstreamable):

- `fix(dashboard-auth)` – don't trust `X-Forwarded-For` unless behind a trusted proxy
  (login rate-limit bypass); centralized in `hermes_cli/dashboard_auth/client_ip.py`.
- `fix(dashboard-auth)` – don't route password providers through the OAuth flow (500 on login).
- `feat(dashboard)` – support a trusted reverse proxy (Tailscale Serve) on a loopback bind
  (`dashboard.trusted_proxy`, `dashboard.allowed_hosts`).
- `feat(dashboard)` – opt-in "Readable" theme (Inter, 17px, plain chrome), aligned with the
  erfana fonts (PR #1, merged into `main`).
- Fork docs: `CLAUDE.md`, this file, and `docs/hermes-server-setup.md`.

Plus a periodic **merge from `upstream/main`** to pull in new upstream work (most recently
2026-07-09, bringing 85 upstream commits in on top of the Qodeca work).

## CI on the fork

GitHub Actions are **enabled** on the fork (`allowed_actions: all`), but in practice **nothing
runs**: the fork has 0 workflow runs and a push to `main` triggers none (verified 2026-07-09).
Treat fork CI as dormant. The publish/deploy workflows are additionally hard-guarded to upstream
and would skip here even if triggered (reporting success without doing anything):

- `deploy-site.yml`, `skills-index.yml`, `skills-index-freshness.yml`, `docker.yml`
  gate on `if: github.repository == 'NousResearch/hermes-agent'`.

Because fork CI catches nothing, run the CI-parity checks locally before pushing:

```bash
scripts/run_tests.sh    # CI-parity test run (needs a .venv; symlink to ~/.hermes/venvs/hermes-dev)
ruff check .            # lint (only PLW1514 enforced)
ty check                # typecheck
```

## Syncing from upstream (NousResearch)

The `upstream` remote is already configured (fetch-only; its push URL is `DISABLE`). Because
`main` now carries Qodeca commits, a sync is a **merge or rebase**, not a fast-forward:

```bash
git fetch upstream main
git checkout main
git merge upstream/main       # resolve conflicts if any, then run scripts/run_tests.sh
git push origin main
```

If Hermes services are running from this checkout, stop them before merging – a conflicted
merge leaves `<<<<<<<` markers that break the next import (see `docs/hermes-server-setup.md`,
"deploy discipline").

## Working-copy note

The checkout normally sits on the `main` branch. Create a feature branch before new work:

```bash
git checkout -b feature/<name>
```
