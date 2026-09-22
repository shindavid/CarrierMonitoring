"""Web Push (VAPID) notifications for the home-screen app.

The browser subscribes (see control.html) and the subscription is stored in the
control database. The controller loop — and the web app's test button — send through
here; pywebpush signs each message with the VAPID private key.

Sending is best-effort by design: a subscription the push service reports as gone
(404/410) is pruned, and any other failure is logged and swallowed, so a push problem
can never disturb the control loop. Nothing is sent (and pywebpush is not even
imported) unless a VAPID key pair is configured.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .controldb import ControlStore
    from .settings import Settings

log = logging.getLogger(__name__)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def generate_keys() -> tuple[str, str]:
    """A fresh VAPID key pair as (public, private), both base64url.

    The public key is the application server key the browser needs to subscribe; the
    private key is the 32-byte EC scalar pywebpush signs with. Run once (``carriermon
    vapid-keys``) and keep the pair in the environment."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    private = key.private_numbers().private_value.to_bytes(32, "big")
    public = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return _b64(public), _b64(private)


def configured(settings: "Settings") -> bool:
    return bool(settings.vapid_public_key and settings.vapid_private_key)


def send(control: "ControlStore", settings: "Settings", *, title: str, body: str,
         tag: str = "carrier-control", url: str = "/control") -> int:
    """Push a notification to every stored subscription. Returns how many were sent.
    Prunes subscriptions the push service says are gone; swallows everything else."""
    if not configured(settings):
        return 0
    subs = control.list_subscriptions()
    if not subs:
        return 0
    from pywebpush import WebPushException, webpush

    payload = json.dumps({"title": title, "body": body, "tag": tag, "url": url})
    claims = {"sub": settings.vapid_subject}
    sent = 0
    for sub in subs:
        try:
            webpush(subscription_info=sub, data=payload,
                    vapid_private_key=settings.vapid_private_key, vapid_claims=dict(claims))
            sent += 1
        except WebPushException as exc:
            status = getattr(exc.response, "status_code", None)
            if status in (404, 410):  # subscription is gone: drop it
                control.remove_subscription(sub["endpoint"])
                log.info("pruned expired push subscription")
            else:
                log.warning("push send failed (%s): %s", status, exc)
        except Exception as exc:  # noqa: BLE001 - never let a push problem escape
            log.warning("push send error: %s", exc)
    return sent
