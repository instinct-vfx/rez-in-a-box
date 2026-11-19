# SPDX-License-Identifier: Apache-2.0
"""Central settings for rez-in-a-box.

Every value can be overridden through an environment variable of the same
name. compose.yaml uses the same variable names (with the same defaults), so
the host tooling and the containers always agree.
"""
from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass, field


def _env(name: str, default: str) -> str:
    return os.environ.get(name) or default


def _env_int(name: str, default: int) -> int:
    return int(_env(name, str(default)))


BROKERS = ("rabbitmq", "lavinmq")


@dataclass(frozen=True)
class QueueSpec:
    name: str
    binding_key: str


@dataclass(frozen=True)
class Settings:
    # which broker implementation runs (compose profile name)
    broker: str = field(default_factory=lambda: _env("REZBOX_BROKER", "rabbitmq"))

    # host-side published ports (always bound to 127.0.0.1)
    amqp_port: int = field(default_factory=lambda: _env_int("REZBOX_AMQP_PORT", 5672))
    amqp_ui_port: int = field(default_factory=lambda: _env_int("REZBOX_AMQP_UI_PORT", 15672))
    memcached_port: int = field(default_factory=lambda: _env_int("REZBOX_MEMCACHED_PORT", 11211))
    cache_ui_port: int = field(default_factory=lambda: _env_int("REZBOX_CACHE_UI_PORT", 8080))

    # broker admin account (management API only, not used by rez)
    admin_user: str = field(default_factory=lambda: _env("REZBOX_AMQP_ADMIN_USER", "admin"))
    admin_password: str = field(default_factory=lambda: _env("REZBOX_AMQP_ADMIN_PASSWORD", "admin"))

    # account rez publishes with
    rez_user: str = field(default_factory=lambda: _env("REZBOX_AMQP_USER", "rez"))
    rez_password: str = field(default_factory=lambda: _env("REZBOX_AMQP_PASSWORD", "rez"))

    # rez's context_tracking_amqp has no vhost key, pika defaults to "/"
    vhost: str = "/"
    exchange: str = field(default_factory=lambda: _env("REZBOX_AMQP_EXCHANGE", "rez"))
    exchange_type: str = "topic"
    queue_max_length: int = field(default_factory=lambda: _env_int("REZBOX_QUEUE_MAX_LENGTH", 10000))

    # rez routing keys: context tracking publishes "<key>.<ACTION>",
    # the amqp release hook publishes its key verbatim.
    context_routing_key: str = "REZ.CONTEXT"
    release_routing_key: str = "REZ.PACKAGE.RELEASED"

    @property
    def queues(self) -> tuple[QueueSpec, ...]:
        return (
            QueueSpec("rez.context", f"{self.context_routing_key}.#"),
            QueueSpec("rez.release", "REZ.PACKAGE.#"),
        )

    @property
    def lavinmq_password_hash(self) -> str:
        """RabbitMQ-style hash: base64(salt[4] + sha256(salt + password)).

        The salt is derived from the password so the value is stable and
        compose does not recreate the container on every run.
        """
        pw = self.admin_password.encode()
        salt = hashlib.sha256(b"rezbox:" + pw).digest()[:4]
        return base64.b64encode(salt + hashlib.sha256(salt + pw).digest()).decode()

    def compose_env(self) -> dict[str, str]:
        """Variables compose needs that cannot be written as plain defaults."""
        return {"REZBOX_LAVINMQ_PASSWORD_HASH": self.lavinmq_password_hash}

    @property
    def management_url(self) -> str:
        return f"http://127.0.0.1:{self.amqp_ui_port}"

    @property
    def cache_ui_url(self) -> str:
        return f"http://127.0.0.1:{self.cache_ui_port}"


def load() -> Settings:
    s = Settings()
    if s.broker not in BROKERS:
        raise ValueError(f"REZBOX_BROKER must be one of {BROKERS}, got {s.broker!r}")
    return s
