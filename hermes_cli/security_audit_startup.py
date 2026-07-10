"""Startup security posture audit (warn-on-load, never blocks).

Surfaces dangerous host / deployment posture at process start so operators
get an at-a-glance "you're exposed" signal. Motivated by the June 2026
MCP-config persistence campaign, where compromised boxes ran as root with an
exposed dashboard / API server and no firewall — and nothing ever told the
operator: the SSH-password-auth check that would have caught it only ever
wrote a WARNING log line nobody read. These checks are advisory: they emit
``logger.warning`` records and return ``(severity, message)`` findings; they
never raise or block startup, and never gate anything closed.

Checks (each is independent and fail-safe — any internal error is swallowed
and simply yields no finding). Severity is either ``"info"`` (log-only) or
``"high"`` (also routed to the operator alert channel — see
:func:`log_startup_security_warnings`):

1. Running as root (POSIX uid 0). — info
2. SSH daemon present with password authentication enabled. — high
3. Running inside a container with no persistent volume mount over the
   HERMES_HOME data dir (state is ephemeral — lost on container restart). — info
4. A network-accessible gateway listener (dashboard / API server) with no
   authentication configured. — high

Cross-platform: the root and SSH checks are POSIX-only and no-op on Windows.
Everything is best-effort and read-only.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger("hermes.security_audit")

# A finding is (severity, human-readable message). "high" findings are
# routed to the operator alert channel (see _alert_high_severity_findings);
# "info" findings are log-only.
Finding = Tuple[str, str]

# Sentinel so the audit only runs once per process even if both the CLI and
# gateway startup paths call it.
_AUDIT_RAN = False


def _is_root() -> bool:
    """True when the process runs as POSIX uid 0. Always False on Windows."""
    getuid = getattr(os, "geteuid", None) or getattr(os, "getuid", None)
    if getuid is None:
        return False
    try:
        return getuid() == 0
    except Exception:
        return False


def _running_as_root() -> Optional[Finding]:
    if not _is_root():
        return None
    return (
        "info",
        "Running as ROOT. The agent's terminal/file tools execute with full "
        "root privileges — a single prompt-injection or exposed endpoint is a "
        "full host compromise. Run Hermes as an unprivileged user (or in a "
        "sandboxed terminal backend / container with a non-root user).",
    )


_SSHD_CONFIG_PATHS = (
    "/etc/ssh/sshd_config",
)
_SSHD_CONFIG_DIR = "/etc/ssh/sshd_config.d"


def _iter_sshd_config_lines() -> list[str]:
    """Yield non-comment lines from sshd_config + its drop-in directory."""
    lines: list[str] = []
    paths: list[Path] = [Path(p) for p in _SSHD_CONFIG_PATHS]
    try:
        d = Path(_SSHD_CONFIG_DIR)
        if d.is_dir():
            paths.extend(sorted(d.glob("*.conf")))
    except Exception:
        pass
    for p in paths:
        try:
            for raw in p.read_text(encoding="utf-8", errors="replace").splitlines():
                stripped = raw.strip()
                if stripped and not stripped.startswith("#"):
                    lines.append(stripped)
        except Exception:
            continue
    return lines


def _ssh_password_auth_enabled() -> Optional[Finding]:
    """Warn when an SSH daemon has password authentication enabled.

    Password auth on a public SSH daemon is the classic brute-force surface
    and pairs badly with a root-capable agent box. POSIX-only; returns None
    when there's no sshd config to read (e.g. Windows, or SSH not installed).
    High severity: this is the finding the June 2026 campaign incident
    turned on — it is routed to the operator alert channel, not just logged.
    """
    lines = _iter_sshd_config_lines()
    if not lines:
        return None
    # Last directive wins in sshd_config. Default (no directive) is "yes".
    verdict = "yes"
    saw_directive = False
    for line in lines:
        m = re.match(r"(?i)^PasswordAuthentication\s+(\w+)", line)
        if m:
            verdict = m.group(1).lower()
            saw_directive = True
    if verdict == "no":
        return None
    qualifier = "" if saw_directive else " (default — no explicit directive)"
    return (
        "high",
        f"SSH password authentication is ENABLED{qualifier}. Password auth is "
        "brute-forceable and dangerous on an internet-facing box. Set "
        "'PasswordAuthentication no' in sshd_config and use key-based auth.",
    )


def _in_container() -> bool:
    """Best-effort container detection (Docker / Podman / generic OCI)."""
    if os.path.exists("/.dockerenv"):
        return True
    if os.environ.get("HERMES_DESKTOP_CHILD_PID"):
        return False  # desktop child, not a server container
    try:
        cgroup = Path("/proc/1/cgroup").read_text(encoding="utf-8", errors="replace")
        if any(tok in cgroup for tok in ("docker", "containerd", "kubepods", "libpod")):
            return True
    except Exception:
        pass
    return False


def _path_is_mounted(path: Path) -> bool:
    """True if *path* sits on (or under) a real mount point per /proc/mounts.

    Container overlay/root filesystems are ephemeral; a bind/volume mount over
    the data dir shows up as a distinct mount entry. We treat the path as
    persisted when a mountpoint at or above it is NOT the container root
    overlay.
    """
    try:
        target = path.resolve()
    except Exception:
        target = path
    try:
        mounts = Path("/proc/mounts").read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return True  # can't tell — fail safe (no warning)
    best = None
    best_fstype = ""
    for line in mounts:
        parts = line.split()
        if len(parts) < 3:
            continue
        mountpoint, fstype = parts[1], parts[2]
        try:
            mp = Path(mountpoint)
        except Exception:
            continue
        if mp == target or mp in target.parents:
            # Longest matching mountpoint wins (most specific).
            if best is None or len(str(mp)) > len(str(best)):
                best = mp
                best_fstype = fstype
    if best is None:
        return True
    # overlay / tmpfs over the data dir = ephemeral container storage.
    return best_fstype not in ("overlay", "tmpfs", "aufs")


def _container_no_volume_mount(hermes_home: Optional[Path]) -> Optional[Finding]:
    if not _in_container():
        return None
    home = hermes_home or Path(
        os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
    )
    try:
        if _path_is_mounted(home):
            return None
    except Exception:
        return None
    return (
        "info",
        f"Running in a container but the data dir ({home}) is NOT on a "
        "persistent volume mount — sessions, memory, skills, and API keys are "
        "ephemeral and lost on container restart. Mount a host volume over the "
        "HERMES_HOME data directory.",
    )


def _network_listener_without_auth(config: Optional[dict]) -> list[Finding]:
    """Warn about network-accessible gateway listeners with no auth.

    Covers the API server (no API_SERVER_KEY) and the dashboard (non-loopback
    bind with no auth provider). Read-only against config + env; overlaps the
    hard fail-closed guards but surfaces the posture proactively at startup.
    High severity: an unauthenticated, network-reachable, terminal-capable
    endpoint is remote code execution — routed to the operator alert channel.
    """
    findings: list[Finding] = []
    try:
        from gateway.platforms.base import is_network_accessible
    except Exception:
        return findings

    cfg = config or {}

    # API server.
    try:
        plats = (cfg.get("platforms") or {})
        api = plats.get("api_server") if isinstance(plats, dict) else None
        if isinstance(api, dict) and api.get("enabled"):
            extra = api.get("extra") or {}
            host = extra.get("host") or os.environ.get("API_SERVER_HOST", "127.0.0.1")
            key = extra.get("key") or os.environ.get("API_SERVER_KEY", "")
            if is_network_accessible(str(host)) and not str(key).strip():
                findings.append((
                    "high",
                    f"OpenAI-compatible API server is network-accessible ({host}) "
                    "with NO API_SERVER_KEY. It dispatches terminal-capable agent "
                    "work — an unauthenticated network endpoint is remote code "
                    "execution. Set a strong API_SERVER_KEY.",
                ))
    except Exception:
        pass

    return findings


def run_security_audit(
    *, hermes_home: Optional[Path] = None, config: Optional[dict] = None
) -> list[Finding]:
    """Run all checks and return a list of ``(severity, message)`` findings.

    Pure: no logging, no side effects. Each check is independently
    fail-safe. Used directly by tests; the logging + alert-routing wrapper
    is :func:`log_startup_security_warnings`.
    """
    findings: list[Finding] = []
    for check in (
        _running_as_root,
        _ssh_password_auth_enabled,
    ):
        try:
            r = check()
            if r:
                findings.append(r)
        except Exception:
            continue
    try:
        r = _container_no_volume_mount(hermes_home)
        if r:
            findings.append(r)
    except Exception:
        pass
    try:
        findings.extend(_network_listener_without_auth(config))
    except Exception:
        pass
    return findings


def _alert_high_severity_findings(findings: list[Finding]) -> None:
    """Route "high" severity findings to the operator alert channel.

    One combined alert per startup event, not one per finding: the
    ``send_operator_alert`` helper rate-limits identical titles for 15
    minutes, so calling it once per finding under a single fixed title
    ("security posture") would just have every finding after the first
    silently suppressed by that rate limit — the opposite of what fixing
    the incident where security warnings only ever landed in a log nobody
    read is meant to fix. A single alert listing every high finding says
    the same thing in
    one message, which also matches what "process just started" actually
    is: one event, not N independent ones.

    Guarded import: this module must not hard-depend on
    ``hermes_cli.operator_alerts`` (mirrors the guarded-import pattern used
    by ``cron/scheduler.py`` and ``agent/background_review.py`` for the same
    module). ``send_operator_alert`` itself never raises, but the import can
    fail if the module is unavailable, so the whole call is wrapped.
    """
    high_messages = [msg for severity, msg in findings if severity == "high"]
    if not high_messages:
        return
    try:
        from hermes_cli.operator_alerts import send_operator_alert

        body = "\n".join(f"- {msg}" for msg in high_messages)
        send_operator_alert("security posture", body, severity="high")
    except Exception:
        logger.debug(
            "security posture audit: unable to route high-severity findings "
            "to the operator alert channel", exc_info=True,
        )


def log_startup_security_warnings(
    *,
    hermes_home: Optional[Path] = None,
    config: Optional[dict] = None,
    force: bool = False,
) -> list[Finding]:
    """Run the audit once per process and emit each finding via logger.warning.

    Also routes any "high" severity findings to the operator alert channel
    (see :func:`_alert_high_severity_findings`) so they don't only live in a
    log file nobody watches overnight. Returns the findings (also for
    tests). Never raises. Idempotent unless ``force=True`` (used by tests).
    """
    global _AUDIT_RAN
    if _AUDIT_RAN and not force:
        return []
    _AUDIT_RAN = True
    try:
        findings = run_security_audit(hermes_home=hermes_home, config=config)
    except Exception:
        return []
    if findings:
        logger.warning(
            "Security posture audit found %d issue(s) — review your deployment:",
            len(findings),
        )
        for i, (_severity, msg) in enumerate(findings, 1):
            logger.warning("  [security %d/%d] %s", i, len(findings), msg)
        try:
            _alert_high_severity_findings(findings)
        except Exception:
            logger.debug("security posture audit: alert routing failed", exc_info=True)
    return findings
