"""
config.py — Centralised configuration pulled from environment variables.

In production (EC2), these are injected via:
  - AWS Systems Manager Parameter Store  (non-sensitive)
  - AWS Secrets Manager                  (DB password, Redis auth token)

Never hard-code credentials. This module enforces that at import time.
"""

import os
from dataclasses import dataclass, field


@dataclass
class Config:
    # ── RDS (PostgreSQL) ────────────────────────────────────────────────────
    RDS_HOST    : str = field(default_factory=lambda: os.environ["RDS_HOST"])
    RDS_PORT    : int = field(default_factory=lambda: int(os.getenv("RDS_PORT", "5432")))
    RDS_DB      : str = field(default_factory=lambda: os.environ["RDS_DB"])
    RDS_USER    : str = field(default_factory=lambda: os.environ["RDS_USER"])
    RDS_PASSWORD: str = field(default_factory=lambda: os.environ["RDS_PASSWORD"])

    # Connection pool — sized for t3.medium (2 vCPU).
    # Rule of thumb: pool_size = (2 × vCPU) + 1 for CPU-bound workloads.
    DB_POOL_MIN     : int = 2
    DB_POOL_MAX     : int = 10
    DB_POOL_OVERFLOW: int = 5          # extra connections allowed beyond max
    DB_POOL_TIMEOUT : int = 30         # seconds to wait for a connection
    DB_POOL_RECYCLE : int = 1800       # recycle connections every 30 min (avoids stale TCP)
    DB_STATEMENT_TIMEOUT: int = 5000   # 5 s — abort slow queries, not hold connections

    # ── ElastiCache Redis ────────────────────────────────────────────────────
    REDIS_HOST      : str = field(default_factory=lambda: os.environ["REDIS_HOST"])
    REDIS_PORT      : int = field(default_factory=lambda: int(os.getenv("REDIS_PORT", "6379")))
    REDIS_AUTH_TOKEN: str = field(default_factory=lambda: os.getenv("REDIS_AUTH_TOKEN", ""))
    REDIS_SSL       : bool = field(default_factory=lambda: os.getenv("REDIS_SSL", "true").lower() == "true")
    REDIS_DB        : int  = 0         # use DB-0; cluster mode ignores this

    # Connection pool for Redis (redis-py uses a thread-safe connection pool)
    REDIS_MAX_CONNECTIONS: int = 50
    REDIS_SOCKET_TIMEOUT : int = 2     # fail fast — Redis should answer in < 1 ms
    REDIS_RETRY_ON_TIMEOUT: bool = True

    # ── Cache TTL Strategy ───────────────────────────────────────────────────
    # Each TTL is tuned to how frequently the underlying data changes.

    # Individual product detail — changes when admin edits a product.
    # 30 min is safe; write-through invalidation keeps it fresh on updates.
    TTL_PRODUCT_DETAIL: int = 1800          # 30 minutes

    # Category listing — new products are added infrequently.
    TTL_PRODUCT_LIST  : int = 300           # 5 minutes

    # Search results — highly dynamic (new products, price changes).
    TTL_SEARCH_RESULT : int = 60            # 1 minute

    # Hot products (homepage / featured) — very stable, safe to cache longer.
    TTL_HOT_PRODUCTS  : int = 3600          # 1 hour

    # Inventory counts — changes on every purchase; kept very short.
    TTL_INVENTORY     : int = 30            # 30 seconds

    # Jitter applied to TTL to avoid thundering herd (cache stampede).
    # Each key's TTL = base_ttl + random(0, TTL_JITTER_RANGE)
    TTL_JITTER_RANGE  : int = 60            # ±60 s spread

    # ── Cache Key Namespace ──────────────────────────────────────────────────
    # All keys follow:  {APP_ENV}:{NAMESPACE}:{entity}:{id}:{version}
    CACHE_KEY_VERSION : str = "v1"          # bump this for schema migrations
    APP_ENV           : str = field(default_factory=lambda: os.getenv("APP_ENV", "prod"))

    # ── Application ──────────────────────────────────────────────────────────
    LOG_LEVEL         : str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))
    SECRET_KEY        : str = field(default_factory=lambda: os.environ["FLASK_SECRET_KEY"])

    # ── Gunicorn (for reference — see gunicorn.conf.py) ─────────────────────
    GUNICORN_WORKERS      : int = 4    # (2 × vCPU) + 1
    GUNICORN_WORKER_CLASS : str = "gevent"
    GUNICORN_BIND         : str = "0.0.0.0:8080"
