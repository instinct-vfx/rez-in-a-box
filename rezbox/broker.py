# SPDX-License-Identifier: Apache-2.0
"""Broker provisioning through the HTTP management API.

Works against RabbitMQ's management plugin and LavinMQ (which implements the
same API). All operations are idempotent, so provisioning can be re-run at
any time.

rez itself never declares the exchange (rez/utils/amqp.py only publishes),
so it has to exist before rez starts tracking contexts or releasing.
"""
from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import quote

from .settings import Settings


class BrokerError(RuntimeError):
    pass


class ManagementAPI:
    def __init__(self, base_url: str, user: str, password: str, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        self._auth = f"Basic {token}"
        self.timeout = timeout

    def request(self, method: str, path: str, body: Any = None) -> Any:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base_url + path, data=data, method=method)
        req.add_header("Authorization", self._auth)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            raise BrokerError(f"{method} {path} -> HTTP {e.code}: {detail}") from e
        except (urllib.error.URLError, OSError) as e:
            raise BrokerError(f"{method} {path} -> {e}") from e
        if not payload:
            return None
        try:
            return json.loads(payload)
        except ValueError:
            return payload.decode("utf-8", "replace")

    def get(self, path: str) -> Any:
        return self.request("GET", path)

    def put(self, path: str, body: Any) -> Any:
        return self.request("PUT", path, body)

    def post(self, path: str, body: Any) -> Any:
        return self.request("POST", path, body)


def _q(s: str) -> str:
    return quote(s, safe="")


def api_for(settings: Settings) -> ManagementAPI:
    return ManagementAPI(settings.management_url, settings.admin_user, settings.admin_password)


def wait_until_ready(api: ManagementAPI, timeout: float = 120.0) -> dict:
    """Block until the management API answers, return /api/overview."""
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return api.get("/api/overview")
        except BrokerError as e:
            last = e
            time.sleep(1.0)
    raise BrokerError(f"management API not ready after {timeout:.0f}s: {last}")


def provision(api: ManagementAPI, s: Settings) -> list[str]:
    """Create user, permissions, exchange, queues and bindings. Idempotent."""
    v = _q(s.vhost)
    done: list[str] = []

    api.put(f"/api/users/{_q(s.rez_user)}",
            {"password": s.rez_password, "tags": "management"})
    done.append(f"user {s.rez_user}")

    # rez only needs to publish; read lets `rezbox events` consume with the same account
    api.put(f"/api/permissions/{v}/{_q(s.rez_user)}",
            {"configure": "^$", "write": f"^{s.exchange}$", "read": "^rez\\..*$"})
    done.append(f"permissions {s.rez_user}@{s.vhost}")

    api.put(f"/api/exchanges/{v}/{_q(s.exchange)}",
            {"type": s.exchange_type, "durable": True, "auto_delete": False,
             "internal": False, "arguments": {}})
    done.append(f"exchange {s.exchange} ({s.exchange_type})")

    existing = {(b["destination"], b["routing_key"])
                for b in api.get(f"/api/exchanges/{v}/{_q(s.exchange)}/bindings/source") or []}

    for q in s.queues:
        api.put(f"/api/queues/{v}/{_q(q.name)}",
                {"durable": True, "auto_delete": False,
                 "arguments": {"x-max-length": s.queue_max_length,
                               "x-overflow": "drop-head"}})
        done.append(f"queue {q.name} (max {s.queue_max_length})")
        if (q.name, q.binding_key) not in existing:
            api.post(f"/api/bindings/{v}/e/{_q(s.exchange)}/q/{_q(q.name)}",
                     {"routing_key": q.binding_key, "arguments": {}})
        done.append(f"binding {s.exchange} -[{q.binding_key}]-> {q.name}")
    return done


def queue_info(api: ManagementAPI, s: Settings) -> list[dict[str, Any]]:
    v = _q(s.vhost)
    out = []
    for q in s.queues:
        try:
            info = api.get(f"/api/queues/{v}/{_q(q.name)}")
            out.append({"name": q.name, "messages": info.get("messages", 0),
                        "binding": q.binding_key})
        except BrokerError:
            out.append({"name": q.name, "messages": None, "binding": q.binding_key})
    return out


def get_messages(api: ManagementAPI, s: Settings, queue: str, count: int = 10,
                 consume: bool = False) -> list[dict[str, Any]]:
    """Fetch messages from a queue. Without ``consume`` they are requeued."""
    body = {"count": count,
            "ackmode": "ack_requeue_false" if consume else "ack_requeue_true",
            "encoding": "auto", "truncate": 1_000_000}
    return api.post(f"/api/queues/{_q(s.vhost)}/{_q(queue)}/get", body) or []


def create_tap_queue(api: ManagementAPI, s: Settings, name: str, binding_key: str = "#",
                     expires_ms: int = 15 * 60 * 1000) -> None:
    """A private queue receiving a copy of everything sent to the rez exchange.

    Used by `rezbox test` and `rezbox events` so they never consume from (or
    need to purge) the real rez.* queues. ``x-expires`` makes the broker drop
    the queue if a run is interrupted and never cleans up.
    """
    v = _q(s.vhost)
    # durable: RabbitMQ 4.x no longer permits transient non-exclusive queues
    api.put(f"/api/queues/{v}/{_q(name)}",
            {"durable": True, "auto_delete": False,
             "arguments": {"x-expires": expires_ms, "x-max-length": s.queue_max_length}})
    api.post(f"/api/bindings/{v}/e/{_q(s.exchange)}/q/{_q(name)}",
             {"routing_key": binding_key, "arguments": {}})


def delete_queue(api: ManagementAPI, s: Settings, name: str) -> None:
    try:
        api.request("DELETE", f"/api/queues/{_q(s.vhost)}/{_q(name)}")
    except BrokerError:
        pass


def decode_messages(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Management API messages -> {routing_key, body (parsed JSON or str)}."""
    out = []
    for m in raw:
        body: Any = m.get("payload", "")
        if m.get("payload_encoding") == "base64":
            body = base64.b64decode(body).decode("utf-8", "replace")
        try:
            body = json.loads(body)
        except (TypeError, ValueError):
            pass
        out.append({"routing_key": m.get("routing_key"), "exchange": m.get("exchange"),
                    "body": body})
    return out


def publish(api: ManagementAPI, s: Settings, routing_key: str, payload: str) -> bool:
    """Publish a test message to the rez exchange (used by the smoke test)."""
    res = api.post(f"/api/exchanges/{_q(s.vhost)}/{_q(s.exchange)}/publish",
                   {"properties": {"content_type": "application/json"},
                    "routing_key": routing_key, "payload": payload,
                    "payload_encoding": "string"})
    return bool(res and res.get("routed"))
