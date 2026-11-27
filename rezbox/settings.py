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

# The consumer of rez's context tracking events (github.com/instinct-vfx/
# rez-ctx-tracking-consumer). It also runs the demo seeder, which imports its
# internals, so the box pins an exact version rather than "latest".
DEFAULT_CONSUMER_IMAGE = "ghcr.io/instinct-vfx/rez-ctx-tracking-consumer:0.1.0"


def _env_flag(name: str, default: bool) -> bool:
    return _env(name, "1" if default else "0").strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class QueueSpec:
    name: str
    binding_key: str


@dataclass(frozen=True)
class Settings:
    # which broker implementation runs (compose profile name)
    broker: str = field(default_factory=lambda: _env("REZBOX_BROKER", "rabbitmq"))

    # where the services are reached: published ports on 127.0.0.1 from the
    # host; compose service names (amqp, memcached) from inside the containers
    amqp_hostname: str = field(default_factory=lambda: _env("REZBOX_AMQP_HOSTNAME", "127.0.0.1"))
    memcached_hostname: str = field(default_factory=lambda: _env("REZBOX_MEMCACHED_HOSTNAME", "127.0.0.1"))

    # host-side published ports (always bound to 127.0.0.1)
    amqp_port: int = field(default_factory=lambda: _env_int("REZBOX_AMQP_PORT", 5672))
    amqp_ui_port: int = field(default_factory=lambda: _env_int("REZBOX_AMQP_UI_PORT", 15672))
    memcached_port: int = field(default_factory=lambda: _env_int("REZBOX_MEMCACHED_PORT", 11211))
    cache_ui_port: int = field(default_factory=lambda: _env_int("REZBOX_CACHE_UI_PORT", 8080))
    postgres_port: int = field(default_factory=lambda: _env_int("REZBOX_POSTGRES_PORT", 5432))
    consumer_port: int = field(default_factory=lambda: _env_int("REZBOX_CONSUMER_PORT", 9100))
    grafana_port: int = field(default_factory=lambda: _env_int("REZBOX_GRAFANA_PORT", 3000))
    grafana_hostname: str = field(default_factory=lambda: _env("REZBOX_GRAFANA_HOSTNAME", "127.0.0.1"))

    # optional service groups (compose profiles "tracking" and "dashboards");
    # `rezbox up` remembers the choice. Grafana needs Postgres, so
    # dashboards imply tracking.
    tracking: bool = field(default_factory=lambda: _env_flag("REZBOX_TRACKING", True))
    dashboards: bool = field(default_factory=lambda: _env_flag("REZBOX_DASHBOARDS", True))

    consumer_image: str = field(default_factory=lambda: _env("REZBOX_CONSUMER_IMAGE", DEFAULT_CONSUMER_IMAGE))
    # the consumer's own broker account: it may only declare the rez exchange
    # and its queue, unlike rez's publish-only account
    consumer_user: str = field(default_factory=lambda: _env("REZBOX_CONSUMER_AMQP_USER", "rez-consumer"))
    consumer_password: str = field(default_factory=lambda: _env("REZBOX_CONSUMER_AMQP_PASSWORD", "rez-consumer"))
    consumer_queue: str = "rez.context.tracking"
    # the consumer's default AMQP_BINDING_KEYS; matches REZ.CONTEXT.<ACTION>
    consumer_binding_key: str = "REZ.CONTEXT.*"

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

    # context tracking messages are kept by the consumer's queue (see
    # consumer_queue and broker.provision), release messages by this one
    @property
    def queues(self) -> tuple[QueueSpec, ...]:
        return (QueueSpec("rez.release", "REZ.PACKAGE.#"),)

    @property
    def dashboards_active(self) -> bool:
        return self.tracking and self.dashboards

    @property
    def profiles(self) -> list[str]:
        """Compose profiles of the services that should run."""
        return ([self.broker] + (["tracking"] if self.tracking else [])
                + (["dashboards"] if self.dashboards_active else []))

    @property
    def grafana_url(self) -> str:
        return f"http://{self.grafana_hostname}:{self.grafana_port}"

    @property
    def grafana_host_url(self) -> str:
        return f"http://127.0.0.1:{self.grafana_port}"

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
        return {"REZBOX_LAVINMQ_PASSWORD_HASH": self.lavinmq_password_hash,
                "REZBOX_CONSUMER_IMAGE": self.consumer_image}

    @property
    def management_url(self) -> str:
        return f"http://{self.amqp_hostname}:{self.amqp_ui_port}"

    @property
    def amqp_address(self) -> str:
        return f"{self.amqp_hostname}:{self.amqp_port}"

    @property
    def memcached_address(self) -> str:
        return f"{self.memcached_hostname}:{self.memcached_port}"

    @property
    def cache_ui_url(self) -> str:
        return f"http://127.0.0.1:{self.cache_ui_port}"


def load() -> Settings:
    s = Settings()
    if s.broker not in BROKERS:
        raise ValueError(f"REZBOX_BROKER must be one of {BROKERS}, got {s.broker!r}")
    return s
