"""Entrypoint: `python -m app` or `uv run python -m app`."""
from __future__ import annotations

import uvicorn

from .config import get_settings
from .main import assert_safe_bind


def main() -> None:
    settings = get_settings()
    # Refuse to bind beyond loopback without an auth token (fail closed).
    assert_safe_bind(settings)

    # Direct TLS terminates HTTPS in Uvicorn. Serve mode terminates it in the
    # loopback-only trusted proxy validated above.
    https_kwargs: dict[str, str] = {}
    if settings.direct_tls:
        https_kwargs = {
            "ssl_certfile": settings.ssl_certfile,
            "ssl_keyfile": settings.ssl_keyfile,
        }
    scheme = settings.external_scheme

    print(f"Hermes Voice backend listening on {scheme}://{settings.host}:{settings.port}")
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        proxy_headers=settings.serve_mode,
        forwarded_allow_ips=settings.forwarded_allow_ips,
        log_level="info",
        **https_kwargs,
    )


if __name__ == "__main__":
    main()
