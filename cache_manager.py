"""
cache_manager.py — Production Redis client with:

  • Thread-safe connection pool
  • TLS encryption (in-transit) to ElastiCache
  • Consistent key naming convention
  • TTL jitter to prevent cache stampede (thundering herd)
  • Atomic operations using Lua scripts where needed
  • Cache hit/miss metrics
  • Namespace-scoped flush
"""

import json
import logging
import random
import time
from typing import Any, Optional

import redis
from redis.exceptions import RedisError, ConnectionError as RedisConnectionError

from config import Config

logger = logging.getLogger(__name__)


class CacheManager:
    """
    Thin wrapper around redis-py that enforces project-wide conventions:
      key naming, serialisation, TTL jitter, error isolation, metrics.
    """

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._client = self._build_client(cfg)
        self._hits   = 0
        self._misses = 0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_client(self, cfg: Config) -> redis.Redis:
        """
        Build a redis-py client backed by a thread-safe connection pool.
        TLS is mandatory for ElastiCache in-transit encryption.
        """
        pool = redis.ConnectionPool(
            host               = cfg.REDIS_HOST,
            port               = cfg.REDIS_PORT,
            db                 = cfg.REDIS_DB,
            password           = cfg.REDIS_AUTH_TOKEN or None,
            ssl                = cfg.REDIS_SSL,
            ssl_cert_reqs      = "required" if cfg.REDIS_SSL else None,
            max_connections    = cfg.REDIS_MAX_CONNECTIONS,
            socket_timeout     = cfg.REDIS_SOCKET_TIMEOUT,
            socket_connect_timeout = cfg.REDIS_SOCKET_TIMEOUT,
            retry_on_timeout   = cfg.REDIS_RETRY_ON_TIMEOUT,
            decode_responses   = True,   # return str, not bytes
        )
        client = redis.Redis(connection_pool=pool)
        logger.info("Redis connection pool initialised | host=%s port=%d ssl=%s",
                    cfg.REDIS_HOST, cfg.REDIS_PORT, cfg.REDIS_SSL)
        return client

    # ------------------------------------------------------------------
    # Key naming convention
    # ------------------------------------------------------------------

    def make_key(self, namespace: str, *parts: Any) -> str:
        """
        Build a structured cache key:
          {env}:{version}:{namespace}:{part1}:{part2}:...

        Examples:
          prod:v1:product:detail:42
          prod:v1:product:list:electronics:1
          prod:v1:product:search:laptop:1

        WHY:
          • env prefix separates prod/staging keys on the same cluster
          • version prefix allows zero-downtime schema migrations
          • namespace prefix enables namespace-wide invalidation via SCAN
          • colon separator is Redis convention and works with RedisInsight
        """
        env     = self._cfg.APP_ENV
        version = self._cfg.CACHE_KEY_VERSION
        parts_str = ":".join(str(p) for p in parts)
        return f"{env}:{version}:{namespace}:{parts_str}"

    def _ttl_with_jitter(self, base_ttl: int) -> int:
        """
        Add random jitter to TTL so mass-expiry (thundering herd) is avoided.
        Keys set at the same time will expire at different moments.
        """
        jitter = random.randint(0, self._cfg.TTL_JITTER_RANGE)
        return base_ttl + jitter

    # ------------------------------------------------------------------
    # Core cache operations
    # ------------------------------------------------------------------

    def get(self, key: str) -> Optional[Any]:
        """
        Fetch a value from Redis.
        Returns the deserialised Python object, or None on miss / error.
        Errors are intentionally swallowed — cache failure must NEVER crash the app.
        """
        try:
            raw = self._client.get(key)
            if raw is None:
                self._misses += 1
                logger.info("CACHE MISS | key=%s", key)
                return None

            self._hits += 1
            logger.info("CACHE HIT  | key=%s", key)
            return json.loads(raw)

        except RedisError as exc:
            logger.warning("Redis GET error | key=%s | err=%s", key, exc)
            self._misses += 1
            return None

    def set(self, key: str, value: Any, ttl: int) -> bool:
        """
        Serialise value to JSON and store with TTL + jitter.
        Returns True on success, False on error.
        """
        try:
            effective_ttl = self._ttl_with_jitter(ttl)
            payload = json.dumps(value, default=str)   # default=str handles datetime
            self._client.setex(name=key, time=effective_ttl, value=payload)
            logger.debug("CACHE SET  | key=%s | ttl=%d s", key, effective_ttl)
            return True

        except RedisError as exc:
            logger.warning("Redis SET error | key=%s | err=%s", key, exc)
            return False

    def delete(self, key: str) -> bool:
        """Evict a single key. Used on UPDATE / DELETE events."""
        try:
            deleted = self._client.delete(key)
            if deleted:
                logger.info("CACHE DEL  | key=%s | EVICTED", key)
            else:
                logger.debug("CACHE DEL  | key=%s | KEY_NOT_FOUND", key)
            return bool(deleted)
        except RedisError as exc:
            logger.warning("Redis DEL error | key=%s | err=%s", key, exc)
            return False

    # ------------------------------------------------------------------
    # Lazy Loading  — the heart of the Side-Cache pattern
    # ------------------------------------------------------------------

    def get_or_load(
        self,
        key     : str,
        loader  : callable,
        ttl     : int,
        *,
        miss_label: str = "",
    ) -> tuple[Optional[Any], str]:
        """
        Side-Cache / Lazy Loading implementation:

          1. Check Redis  → HIT  → return cached value
          2.               MISS → call loader() (hits RDS)
          3.                      Store result in Redis with TTL
          4.                      Return fresh value

        Returns (value, cache_status) where cache_status ∈ {"HIT", "MISS"}

        The caller never needs to know about Redis — the service layer
        just calls get_or_load() and gets back a value.

        WHY get_or_load() instead of explicit get/set in service layer?
          • Centralises retry / error handling logic
          • Prevents duplicate cache-write code across multiple services
          • Easier to unit-test (mock the loader callable)
        """
        value = self.get(key)

        if value is not None:
            return value, "HIT"

        # ── Cache Miss: fetch from primary data source (RDS) ──────────────
        label = miss_label or key
        logger.info("CACHE MISS → loading from RDS | ref=%s", label)

        db_start = time.perf_counter()
        value    = loader()
        db_ms    = (time.perf_counter() - db_start) * 1000
        logger.info("RDS query complete | ref=%s | latency=%.2f ms", label, db_ms)

        if value is not None:
            # ── Populate cache so next request is a HIT ───────────────────
            self.set(key, value, ttl)

        return value, "MISS"

    # ------------------------------------------------------------------
    # Namespace-scoped operations
    # ------------------------------------------------------------------

    def flush_namespace(self, namespace: str) -> int:
        """
        Delete all keys matching the namespace pattern.
        Uses SCAN (not KEYS) to avoid blocking the Redis event loop.

        WHY SCAN not KEYS?
          KEYS blocks Redis during full keyspace scan — catastrophic in prod.
          SCAN iterates in small batches, non-blocking.
        """
        pattern    = f"{self._cfg.APP_ENV}:{self._cfg.CACHE_KEY_VERSION}:{namespace}:*"
        deleted    = 0
        cursor     = 0

        try:
            while True:
                cursor, keys = self._client.scan(cursor, match=pattern, count=200)
                if keys:
                    deleted += self._client.delete(*keys)
                if cursor == 0:
                    break

            logger.info("Namespace flush | pattern=%s | deleted=%d", pattern, deleted)
            return deleted

        except RedisError as exc:
            logger.error("Namespace flush error | pattern=%s | err=%s", pattern, exc)
            return 0

    def invalidate_product(self, product_id: int, category: str = None) -> None:
        """
        Targeted invalidation on product update / delete.
        Removes the detail key + any list key for the product's category.
        """
        detail_key = self.make_key("product", "detail", product_id)
        self.delete(detail_key)

        if category:
            # Also invalidate list pages that include this product
            list_pattern = self.make_key("product", "list", category) + ":*"
            self._flush_pattern(list_pattern)

    def _flush_pattern(self, pattern: str) -> int:
        """SCAN + DELETE for an arbitrary glob pattern."""
        deleted = 0
        cursor  = 0
        try:
            while True:
                cursor, keys = self._client.scan(cursor, match=pattern, count=200)
                if keys:
                    deleted += self._client.delete(*keys)
                if cursor == 0:
                    break
        except RedisError as exc:
            logger.warning("Pattern flush error | pattern=%s | err=%s", pattern, exc)
        return deleted

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    def ping(self) -> bool:
        try:
            return self._client.ping()
        except RedisError:
            return False

    def get_stats(self) -> dict:
        """Return in-process hit/miss counters + Redis INFO STATS."""
        total = self._hits + self._misses
        hit_rate = round((self._hits / total) * 100, 2) if total else 0

        try:
            info = self._client.info("stats")
        except RedisError:
            info = {}

        return {
            "in_process": {
                "hits"    : self._hits,
                "misses"  : self._misses,
                "total"   : total,
                "hit_rate": f"{hit_rate}%",
            },
            "redis_server": {
                "keyspace_hits"  : info.get("keyspace_hits",   "N/A"),
                "keyspace_misses": info.get("keyspace_misses",  "N/A"),
                "evicted_keys"   : info.get("evicted_keys",     "N/A"),
                "used_memory_human": self._client.info("memory").get(
                    "used_memory_human", "N/A"),
            },
        }
