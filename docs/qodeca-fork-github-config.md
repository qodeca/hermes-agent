# Qodeca fork – GitHub configuration

> **Audience:** Qodeca maintainers of this fork
> **Scope:** Qodeca-specific fork setup, not part of upstream Hermes docs
> **Last updated:** 2026-07-08

## Overview

This repository is a **public fork**: `qodeca/hermes-agent`, forked from the original
`NousResearch/hermes-agent`. As of the last update, Qodeca's copy is a **clean mirror** –
`main` carries zero Qodeca-specific commits, and the `.github/` configuration is
byte-identical to upstream. Nothing in the fork has been customized yet.

## State at a glance

| Aspect | State | Note |
|--------|-------|------|
| Fork relationship | `qodeca/hermes-agent` ← `NousResearch/hermes-agent` | Public, owned by the `qodeca` org |
| `main` divergence | 0 ahead / 19 behind upstream | Pure downstream mirror, no Qodeca changes |
| Git remotes | only `origin` = qodeca | **No `upstream` remote** configured for the Nous original |
| Issues | **disabled** on the fork | Bug reports belong on the upstream repo |
| Open PRs | none | — |
| Branches | ~100 | Inherited from upstream at fork time (Nous dev branches, e.g. `alice/…`, `atropos-…`) |
| GitHub Actions | enabled, but **0 runs ever** | Effectively dormant on the fork (see below) |
| Merge methods | merge / squash / rebase all allowed | Default GitHub settings |
| Auth (local) | `gh` via `GITHUB_TOKEN` | scopes: `repo`, `workflow`, `read:org`, `gist` |

## CI does not effectively run on the fork

Several workflows are hard-guarded to the upstream repo and no-op here:

- `deploy-site.yml`, `skills-index.yml`, `skills-index-freshness.yml`, `docker.yml`
  all gate on `if: github.repository == 'NousResearch/hermes-agent'`.

The core checks (`tests.yml`, `lint.yml`, `typecheck.yml`) are **not** guarded, but the
fork shows zero workflow runs – they have never been triggered. Practical consequence:
pushing to Qodeca does **not** provide the green-checkmark safety net the upstream repo
has. Rely on local verification instead:

```bash
scripts/run_tests.sh    # CI-parity test run
ruff check .            # lint (only PLW1514 enforced)
ty check                # typecheck
```

## Syncing from upstream (NousResearch)

There is no `upstream` remote configured, so pulling newer upstream commits requires
adding one (one-time):

```bash
git remote add upstream https://github.com/NousResearch/hermes-agent.git
git fetch upstream main
```

Then fast-forward `main` (the fork has no local divergence to conflict with):

```bash
git checkout main
git merge --ff-only upstream/main
git push origin main
```

## Working-copy note

The local checkout may sit in **detached HEAD** at a release tag (e.g. `v2026.7.7.2`)
rather than on a branch. Create a branch before starting new work:

```bash
git checkout -b feature/<name>
```
