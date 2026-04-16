"""Web Push helper — sends VAPID-signed Push messages to subscribed clients."""

from __future__ import annotations

import asyncio
import json
import logging

from src.config import WebConfig
from src.sync.state import SyncState

logger = logging.getLogger(__name__)


def send_push(
    config: WebConfig,
    state: SyncState,
    title: str,
    body: str,
    url: str = "/",
) -> int:
    """Fan-out a notification to every registered subscription.

    Returns the number of messages successfully sent. Silently does
    nothing if VAPID keys are not configured. This is a blocking call
    — use :func:`send_push_async` from async contexts.
    """
    if not config.vapid_public_key or not config.vapid_private_key:
        logger.debug("VAPID keys missing, skipping push")
        return 0

    try:
        from pywebpush import WebPushException, webpush
    except ImportError:
        logger.warning("pywebpush not installed, cannot send push")
        return 0

    payload = json.dumps({"title": title, "body": body, "url": url})
    vapid_claims = {"sub": config.vapid_subject}

    subs = state.list_webpush_subscriptions()
    sent = 0

    for sub in subs:
        subscription_info = {
            "endpoint": sub["endpoint"],
            "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
        }
        try:
            webpush(
                subscription_info=subscription_info,
                data=payload,
                vapid_private_key=config.vapid_private_key,
                vapid_claims=dict(vapid_claims),
            )
            sent += 1
        except WebPushException as e:
            status = getattr(e.response, "status_code", None)
            if status == 410:
                state.remove_webpush_subscription(sub["endpoint"])
                logger.info("Removed gone subscription: %s", sub["endpoint"][:40])
            else:
                logger.warning("WebPush failed (%s): %s", status, e)
        except Exception as e:
            logger.warning("WebPush unexpected error: %s", e)

    return sent


async def send_push_async(
    config: WebConfig,
    state: SyncState,
    title: str,
    body: str,
    url: str = "/",
) -> int:
    """Async wrapper that offloads ``send_push`` to a thread.

    ``pywebpush`` is synchronous and does one requests call per
    subscriber. Running it directly from an async route blocks the
    event loop for hundreds of milliseconds per subscription; wrapping
    it in ``asyncio.to_thread`` keeps other requests moving.
    """
    return await asyncio.to_thread(
        send_push,
        config,
        state,
        title,
        body,
        url,
    )


def generate_vapid_keys() -> tuple[str, str]:
    """Generate a new VAPID keypair.

    Returns (public_key, private_key) as URL-safe base64 strings.
    """
    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    private_key = ec.generate_private_key(ec.SECP256R1())
    priv_bytes = private_key.private_numbers().private_value.to_bytes(32, "big")
    priv_b64 = base64.urlsafe_b64encode(priv_bytes).rstrip(b"=").decode()

    pub_key = private_key.public_key()
    pub_bytes = pub_key.public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    pub_b64 = base64.urlsafe_b64encode(pub_bytes).rstrip(b"=").decode()

    return pub_b64, priv_b64
