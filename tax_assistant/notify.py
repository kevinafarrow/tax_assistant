"""Pushover notifications and healthchecks.io pings."""
from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)

PUSHOVER_URL = "https://api.pushover.net/1/messages.json"


def pushover(cfg, title: str, message: str, priority: int = 0) -> bool:
    if not cfg.pushover_user_key or not cfg.pushover_app_token:
        log.warning("Pushover not configured; skipping notification: %s", title)
        return False
    try:
        resp = httpx.post(
            PUSHOVER_URL,
            data={
                "token": cfg.pushover_app_token,
                "user": cfg.pushover_user_key,
                "title": title,
                "message": message[:1024],
                "priority": priority,
            },
            timeout=15,
        )
        resp.raise_for_status()
        return True
    except Exception:
        log.exception("Pushover notification failed: %s", title)
        return False


def hc_ping(cfg, ok: bool, message: str = "") -> None:
    if not cfg.healthchecks_url:
        log.debug("healthchecks.io not configured; skipping ping")
        return
    url = cfg.healthchecks_url if ok else cfg.healthchecks_url + "/fail"
    try:
        httpx.post(url, content=message[:2000], timeout=10)
    except Exception:
        # A failed ping must never take down the poller; healthchecks' own
        # grace period covers us if pings can't get out.
        log.exception("healthchecks.io ping failed")
