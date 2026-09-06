"""
Multi-proxy support for reaching the Telegram Bot API when the server itself
"""

import logging
import os
import time
from typing import List, Optional

import httpx

logger = logging.getLogger(__name__)

PROXY_TEST_URL = "https://api.telegram.org"
PROXY_TEST_TIMEOUT = float(os.getenv("PROXY_TEST_TIMEOUT", "6"))
DIRECT_CONNECT_ATTEMPTS = int(os.getenv("DIRECT_CONNECT_ATTEMPTS", "3"))
DIRECT_CONNECT_RETRY_DELAY = float(os.getenv("DIRECT_CONNECT_RETRY_DELAY", "2"))


def safe(proxy_url: str) -> str:
    """Strip credentials before printing/logging a proxy URL."""
    return proxy_url.split("@")[-1] if "@" in proxy_url else proxy_url


def get_proxy_candidates() -> List[str]:
    multi = os.getenv("TELEGRAM_PROXY_URLS", "")
    urls = [u.strip() for u in multi.split(",") if u.strip()]
    if urls:
        return urls
    single = os.getenv("TELEGRAM_PROXY_URL")
    return [single] if single else []


def _test_direct() -> Optional[float]:
    """Tests reaching Telegram with no proxy. Returns latency, or None on failure."""
    try:
        start = time.monotonic()
        with httpx.Client(timeout=PROXY_TEST_TIMEOUT) as client:
            resp = client.get(PROXY_TEST_URL)
            if resp.status_code >= 500:
                raise RuntimeError(f"HTTP {resp.status_code}")
        return time.monotonic() - start
    except Exception as e:
        logger.info(f"✕ direct connection failed: {e}")
        return None


def _test_proxy(proxy_url: str) -> Optional[float]:
    """Returns latency in seconds if the proxy can reach Telegram, else None."""
    try:
        start = time.monotonic()
        with httpx.Client(proxy=proxy_url, timeout=PROXY_TEST_TIMEOUT) as client:
            resp = client.get(PROXY_TEST_URL)
            # api.telegram.org replies 404 on a bare GET - still proof it works.
            if resp.status_code >= 500:
                raise RuntimeError(f"HTTP {resp.status_code}")
        return time.monotonic() - start
    except Exception as e:
        logger.info(f"✕ proxy unreachable ({safe(proxy_url)}): {e}")
        return None


def pick_working_proxy(candidates: List[str]) -> Optional[str]:
    """Tests every candidate and returns the fastest one that actually works."""
    results = []
    for p in candidates:
        latency = _test_proxy(p)
        if latency is not None:
            logger.info(f"✓ proxy OK ({safe(p)}) - {latency:.2f}s")
            results.append((latency, p))
    if not results:
        return None
    results.sort(key=lambda t: t[0])
    return results[0][1]


def resolve_proxy() -> Optional[str]:
    print(f"⌕ Testing direct connectivity to Telegram ({DIRECT_CONNECT_ATTEMPTS} attempt(s))...")
    for attempt in range(1, DIRECT_CONNECT_ATTEMPTS + 1):
        latency = _test_direct()
        if latency is not None:
            print(f"✓ Direct connection OK ({latency:.2f}s) - no proxy needed.")
            return None
        print(f"✕ Direct attempt {attempt}/{DIRECT_CONNECT_ATTEMPTS} failed.")
        if attempt < DIRECT_CONNECT_ATTEMPTS:
            time.sleep(DIRECT_CONNECT_RETRY_DELAY)

    candidates = get_proxy_candidates()
    if not candidates:
        print(
            "⚠ Direct connection failed and no TELEGRAM_PROXY_URLS are configured - "
            "continuing without a proxy anyway (bot may not connect)."
        )
        return None

    print(f"⌕ Direct connection unreliable - testing {len(candidates)} proxy candidate(s)...")
    chosen = pick_working_proxy(candidates)
    if chosen is None:
        print("⚠ None of the configured proxies could reach Telegram either - continuing without a proxy.")
    return chosen
