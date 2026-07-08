"""Trusted client-IP resolution for dashboard auth.

Security-sensitive. The login rate limiter (``routes.py``) keys its per-IP
throttle on this value, so honouring a client-supplied ``X-Forwarded-For``
header on a direct bind lets a caller vary the header per request and slip
each guess into a fresh bucket — defeating the throttle entirely (unbounded
online password guessing). The audit log also records this IP.

We therefore trust ``X-Forwarded-For`` ONLY when the operator declares they
run behind a trusted reverse proxy via ``dashboard.trusted_proxy: true`` in
``config.yaml``. Default off → use the real transport peer address. This is
the single source of truth; ``routes.py``, ``middleware.py`` and
``token_auth.py`` all delegate here so the behaviour cannot diverge.
"""

from __future__ import annotations

from fastapi import Request


def trust_forwarded_for() -> bool:
    """True when config opts into trusting ``X-Forwarded-For`` (reverse proxy)."""
    # Local import avoids any import cycle at module load; load_config_readonly
    # is mtime-cached so this is cheap enough for a per-request login path.
    from hermes_cli.config import cfg_get, load_config_readonly

    return bool(
        cfg_get(load_config_readonly(), "dashboard", "trusted_proxy", default=False)
    )


def client_ip(request: Request) -> str:
    """Resolve the caller's IP.

    Honours the first ``X-Forwarded-For`` hop only when
    ``dashboard.trusted_proxy`` is set; otherwise returns the real transport
    peer address so a spoofed header cannot be used to evade the rate limiter.
    """
    if trust_forwarded_for():
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""
