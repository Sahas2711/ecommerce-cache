"""
db_manager.py — Production PostgreSQL client for Amazon RDS.

Features:
  • Connection pooling (psycopg2 ThreadedConnectionPool)
  • Automatic retry with exponential back-off on transient errors
  • Statement timeout to prevent slow queries holding connections
  • Context-manager pattern for safe connection release
  • Parameterised queries everywhere (SQL injection prevention)
  • SSL enforcement for RDS in-transit encryption
"""

import logging
import time
from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
import psycopg2.extras
from psycopg2 import pool, OperationalError, InterfaceError
from psycopg2.extras import RealDictCursor

from config import Config

logger = logging.getLogger(__name__)

# Number of retry attempts for transient DB errors (e.g., brief network blip)
_MAX_RETRIES = 3
_RETRY_BACKOFF_BASE = 0.5   # seconds; doubles each retry


class DatabaseManager:
    """
    Thread-safe PostgreSQL connection manager backed by a pooled connection pool.

    WHY psycopg2 ThreadedConnectionPool instead of SQLAlchemy?
      Simpler, no ORM overhead, gives full control over SQL, connection lifecycle.
      In a microservice that owns a single DB schema, the ORM abstraction adds
      complexity without benefit.

    All queries return plain Python dicts — no ORM objects to serialise.
    """

    def __init__(self, cfg: Config) -> None:
        self._cfg  = cfg
        self._pool = self._build_pool(cfg)

    # ------------------------------------------------------------------
    # Pool construction
    # ------------------------------------------------------------------

    def _build_pool(self, cfg: Config) -> pool.ThreadedConnectionPool:
        """
        Build the connection pool.
        dsn is constructed explicitly so every parameter is visible.
        sslmode=require enforces TLS — RDS rejects plaintext with this setting
        (combined with the RDS parameter group setting rds.force_ssl=1).
        """
        dsn = (
            f"host={cfg.RDS_HOST} "
            f"port={cfg.RDS_PORT} "
            f"dbname={cfg.RDS_DB} "
            f"user={cfg.RDS_USER} "
            f"password={cfg.RDS_PASSWORD} "
            f"sslmode=require "
            f"connect_timeout=10 "
            f"options='-c statement_timeout={cfg.DB_STATEMENT_TIMEOUT}'"
        )
        p = pool.ThreadedConnectionPool(
            minconn=cfg.DB_POOL_MIN,
            maxconn=cfg.DB_POOL_MAX + cfg.DB_POOL_OVERFLOW,
            dsn=dsn,
        )
        logger.info(
            "RDS connection pool created | host=%s db=%s min=%d max=%d",
            cfg.RDS_HOST, cfg.RDS_DB, cfg.DB_POOL_MIN, cfg.DB_POOL_MAX,
        )
        return p

    # ------------------------------------------------------------------
    # Context manager for safe connection lifecycle
    # ------------------------------------------------------------------

    @contextmanager
    def _get_connection(self):
        """
        Obtain a connection from the pool, yield it, then release it back.
        Rolls back on exception so the connection is returned in a clean state.
        """
        conn = self._pool.getconn()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._pool.putconn(conn)

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def _execute_with_retry(
        self,
        query: str,
        params: Tuple = (),
        fetch: str = "one",     # "one" | "all" | "none"
    ) -> Any:
        """
        Execute a parameterised query with exponential back-off retry.

        WHY retry?
          RDS Multi-AZ failover causes ~20–30 s downtime and transient
          connection drops. Retrying 3× with back-off bridges that gap.

        fetch:
          "one"  → fetchone() → dict or None
          "all"  → fetchall() → list[dict]
          "none" → no fetch (INSERT / UPDATE / DELETE)
        """
        last_exc = None

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                with self._get_connection() as conn:
                    with conn.cursor(cursor_factory=RealDictCursor) as cur:
                        cur.execute(query, params)
                        if fetch == "one":
                            row = cur.fetchone()
                            return dict(row) if row else None
                        elif fetch == "all":
                            rows = cur.fetchall()
                            return [dict(r) for r in rows]
                        else:
                            return cur.rowcount

            except (OperationalError, InterfaceError) as exc:
                last_exc = exc
                wait = _RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
                logger.warning(
                    "DB transient error (attempt %d/%d) | err=%s | retry_in=%.1fs",
                    attempt, _MAX_RETRIES, exc, wait,
                )
                time.sleep(wait)

        logger.error("DB query failed after %d attempts | err=%s", _MAX_RETRIES, last_exc)
        raise last_exc

    # ------------------------------------------------------------------
    # Domain queries
    # ------------------------------------------------------------------

    def get_product_by_id(self, product_id: int) -> Optional[Dict]:
        """
        Fetch a single product with its category name.
        Uses a JOIN instead of two queries — fewer round-trips to RDS.
        """
        sql = """
            SELECT
                p.id,
                p.name,
                p.description,
                p.price,
                p.stock_quantity,
                p.sku,
                p.image_url,
                p.is_active,
                p.created_at,
                p.updated_at,
                c.name  AS category_name,
                c.slug  AS category_slug
            FROM products p
            JOIN categories c ON c.id = p.category_id
            WHERE p.id = %s
              AND p.is_active = TRUE
        """
        return self._execute_with_retry(sql, (product_id,), fetch="one")

    def get_products_by_category(
        self, category_slug: str, page: int, per_page: int
    ) -> List[Dict]:
        """
        Paginated product list for a category.
        OFFSET-based pagination is acceptable for page sizes ≤ 100 and
        catalogues of reasonable size. For millions of rows, use keyset
        pagination (WHERE id > last_seen_id).
        """
        offset = (page - 1) * per_page
        sql = """
            SELECT
                p.id,
                p.name,
                p.price,
                p.stock_quantity,
                p.sku,
                p.image_url,
                c.name  AS category_name
            FROM products p
            JOIN categories c ON c.id = p.category_id
            WHERE c.slug  = %s
              AND p.is_active = TRUE
            ORDER BY p.created_at DESC
            LIMIT %s OFFSET %s
        """
        return self._execute_with_retry(
            sql, (category_slug, per_page, offset), fetch="all"
        )

    def get_all_products(self, page: int, per_page: int) -> List[Dict]:
        """Full catalogue listing (no category filter)."""
        offset = (page - 1) * per_page
        sql = """
            SELECT
                p.id,
                p.name,
                p.price,
                p.stock_quantity,
                p.sku,
                p.image_url,
                c.name AS category_name
            FROM products p
            JOIN categories c ON c.id = p.category_id
            WHERE p.is_active = TRUE
            ORDER BY p.created_at DESC
            LIMIT %s OFFSET %s
        """
        return self._execute_with_retry(sql, (per_page, offset), fetch="all")

    def update_product(self, product_id: int, fields: Dict) -> bool:
        """
        Dynamic UPDATE — only touches fields present in `fields` dict.
        Prevents accidental null-overwrite of fields not in the payload.
        """
        ALLOWED = {"name", "description", "price", "stock_quantity", "image_url", "is_active"}
        updates = {k: v for k, v in fields.items() if k in ALLOWED}

        if not updates:
            return False

        set_clause = ", ".join(f"{col} = %s" for col in updates)
        values     = list(updates.values()) + [product_id]

        sql = f"""
            UPDATE products
            SET {set_clause},
                updated_at = NOW()
            WHERE id = %s
              AND is_active = TRUE
        """
        rows_affected = self._execute_with_retry(sql, tuple(values), fetch="none")
        return rows_affected > 0

    def get_product_category(self, product_id: int) -> Optional[str]:
        """Return category slug for a product — used during cache invalidation."""
        sql = """
            SELECT c.slug
            FROM products p
            JOIN categories c ON c.id = p.category_id
            WHERE p.id = %s
        """
        row = self._execute_with_retry(sql, (product_id,), fetch="one")
        return row["slug"] if row else None

    def delete_product(self, product_id: int) -> bool:
        """Soft delete — sets is_active = FALSE instead of hard DELETE."""
        sql = """
            UPDATE products
            SET is_active  = FALSE,
                updated_at = NOW()
            WHERE id = %s
              AND is_active = TRUE
        """
        rows = self._execute_with_retry(sql, (product_id,), fetch="none")
        return rows > 0

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    def ping(self) -> bool:
        """Lightweight liveness check used by /health endpoint."""
        try:
            result = self._execute_with_retry("SELECT 1", fetch="one")
            return result is not None
        except Exception:
            return False
