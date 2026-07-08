# Qodeca fork – GitHub configuration

> **Audience:** Qodeca maintainers of this fork
> **Scope:** Qodeca-specific fork setup, not part of upstream Hermes docs
> **Last updated:** 2026-07-08

## Overview

This repository is a **public fork**: `qodeca/hermes-agent`, forked from the original
`NousResearch/hermes-agent`. `main` is kept **in sync with upstream** and now also carries
**Qodeca-specific commits** – notably dashboard-auth security fixes and reverse-proxy
(HTTPS) support, plus the fork's own docs. It is no longer a pure mirror.

## State at a glance

| Aspect | State | Note |
|--------|-------|------|
| Fork relationship | `qodeca/hermes-agent` ← `NousResearch/hermes-agent` | Public, owned by the `qodeca` org |
| `main` vs upstream | 8 ahead / 0 behind `upstream/main` | Synced with upstream, plus Qodeca commits on top |
| Git remotes | `origin` = qodeca, `upstream` = NousResearch | `upstream` push URL is set to `DISABLE` (fetch-only) |
| Issues | **disabled** on the fork | Bug reports belong on the upstream repo |
| Branches | ~100 | Inherited from upstream at fork time (Nous dev branches, e.g. `alice/…`, `atropos-…`) |
| GitHub Actions | **active** | `CI` runs on `main` pushes and PRs; guarded publish/deploy workflows no-op (see below) |
| Merge methods | merge / squash / rebase all allowed | Default GitHub settings |
| Auth (local) | `gh` via `GITHUB_TOKEN` | scopes: `repo`, `workflow`, `read:org`, `gist` |

## Qodeca commits on `main`

Security and feature work carried on top of upstream (all PR-quality, upstreamable):

- `fix(dashboard-auth)` – don't trust `X-Forwarded-For` unless behind a trusted proxy
  (login rate-limit bypass); centralized in `hermes_cli/dashboard_auth/client_ip.py`.
- `fix(dashboard-auth)` – don't route password providers through the OAuth flow (500 on login).
- `feat(dashboard)` – support a trusted reverse proxy (Tailscale Serve) on a loopback bind
  (`dashboard.trusted_proxy`, `dashboard.allowed_hosts`).
- Fork docs: `CLAUDE.md`, this file, and `docs/hermes-server-setup.md`.

## CI on the fork

GitHub Actions are active. The unguarded `CI` workflow (tests, lint, typecheck) runs on
`main` pushes and pull requests, so pushes do get checked. The publish/deploy workflows are
hard-guarded to upstream and their jobs skip here (reporting success without doing anything):

- `deploy-site.yml`, `skills-index.yml`, `skills-index-freshness.yml`, `docker.yml`
  gate on `if: github.repository == 'NousResearch/hermes-agent'`.

Still run the CI-parity checks locally before pushing (faster feedback, and matches CI):

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
