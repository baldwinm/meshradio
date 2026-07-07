"""Shared outbound-HTTP setup.

Every httpx client in meshradio identifies itself with the same descriptive
User-Agent — Cloudflare 403s the library default coming from datacenter IPs,
which took down ingestion on Render once.
"""

from __future__ import annotations

import httpx

from . import __version__

USER_AGENT = f"meshradio/{__version__} (+https://github.com/baldwinm/meshradio)"


def http_client(**kwargs) -> httpx.AsyncClient:
    """AsyncClient with the meshradio User-Agent; extra headers merge in."""
    headers = {"User-Agent": USER_AGENT, **kwargs.pop("headers", {})}
    kwargs.setdefault("timeout", 30)
    return httpx.AsyncClient(headers=headers, **kwargs)
