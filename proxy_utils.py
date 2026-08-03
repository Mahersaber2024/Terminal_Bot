"""
Multi-proxy support for reaching the Telegram Bot API when the server itself
has packet loss / is throttled by Telegram.

Env vars (either works, TELEGRAM_PROXY_URLS wins if both are set):

  TELEGRAM_PROXY_URLS = "http://user:pass@host1:8080,socks5://host2:1080,http://host3:3128"
  TELEGRAM_PROXY_URL  = "http://user:pass@host:port"   (single proxy, old behavior)

At startup the bot first tries a DIRECT connection (no proxy) up to
DIRECT_CONNECT_ATTEMPTS times. Only if all of those fail does it test every
proxy candidate against api.telegram.org and use the fastest one that works.
A background watchdog job then periodically re-checks connectivity; if it
stays broken for a while it exits the process on purpose so your process
manager (systemd/pm2/Docker restart policy) restarts the bot - on restart the
whole strategy (direct first, then proxies) runs again from scratch.

SECURITY NOTE: every request to the Bot API includes your bot token in the
URL. Any proxy you route through can see (and steal) that token. Only use
proxies you control, or trusted paid providers - public "free proxy list"
services are a common way bots get hijacked.
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
    """Tests reaching Telegram with no proxy at all. Returns latency, or None on failure."""
    try:
        start = time.monotonic()
        with httpx.Client(timeout=PROXY_TEST_TIMEOUT) as client:
            resp = client.get(PROXY_TEST_URL)
            if resp.status_code >= 500:
                raise RuntimeError(f"HTTP {resp.status_code}")
        return time.monotonic() - start
    except Exception as e:
        logger.info(f"❌ direct connection failed: {e}")
        return None


def _test_proxy(proxy_url: str) -> Optional[float]:
    """Returns latency in seconds if the proxy can reach Telegram, else None."""
    try:
        start = time.monotonic()
        with httpx.Client(proxy=proxy_url, timeout=PROXY_TEST_TIMEOUT) as client:
            resp = client.get(PROXY_TEST_URL)
            # api.telegram.org replies 404 on a bare GET - that's still proof
            # the proxy reached it. Only treat connection-level failures as bad.
            if resp.status_code >= 500:
                raise RuntimeError(f"HTTP {resp.status_code}")
        return time.monotonic() - start
    except Exception as e:
        logger.info(f"❌ proxy unreachable ({safe(proxy_url)}): {e}")
        return None


def pick_working_proxy(candidates: List[str]) -> Optional[str]:
    """Tests every candidate and returns the fastest one that actually works."""
    results = []
    for p in candidates:
        latency = _test_proxy(p)
        if latency is not None:
            logger.info(f"✅ proxy OK ({safe(p)}) - {latency:.2f}s")
            results.append((latency, p))
    if not results:
        return None
    results.sort(key=lambda t: t[0])
    return results[0][1]


def resolve_proxy() -> Optional[str]:
    """
    Connection strategy:
      1. Try reaching Telegram directly (no proxy) up to DIRECT_CONNECT_ATTEMPTS
         times, a few seconds apart. If any attempt succeeds, no proxy is used
         at all - this is the normal/fast path when the server's own network
         is fine.
      2. Only if every direct attempt fails does it fall back to the
         TELEGRAM_PROXY_URLS candidates, testing each and using the fastest
         one that actually works.

    Returns the chosen proxy URL, or None (meaning: connect directly).
    """
    print(f"🔎 Testing direct connectivity to Telegram ({DIRECT_CONNECT_ATTEMPTS} attempt(s))...")
    for attempt in range(1, DIRECT_CONNECT_ATTEMPTS + 1):
        latency = _test_direct()
        if latency is not None:
            print(f"✅ Direct connection OK ({latency:.2f}s) - no proxy needed.")
            return None
        print(f"❌ Direct attempt {attempt}/{DIRECT_CONNECT_ATTEMPTS} failed.")
        if attempt < DIRECT_CONNECT_ATTEMPTS:
            time.sleep(DIRECT_CONNECT_RETRY_DELAY)

    candidates = get_proxy_candidates()
    if not candidates:
        print(
            "⚠️ Direct connection failed and no TELEGRAM_PROXY_URLS are configured - "
            "continuing without a proxy anyway (bot may not connect)."
        )
        return None

    print(f"🔎 Direct connection unreliable - testing {len(candidates)} proxy candidate(s)...")
    chosen = pick_working_proxy(candidates)
    if chosen is None:
        print("⚠️ None of the configured proxies could reach Telegram either - continuing without a proxy.")
    return chosen
