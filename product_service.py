"""
product_service.py — Business logic layer.

This is where the Lazy Loading (Side-Cache) pattern lives.
The service layer is the ONLY code that knows about both Redis and RDS.
Routes only call service methods; they never touch the DB or cache directly.

Lazy Loading flow for get_product():

  ┌─────────┐   GET /products/42   ┌───────────────┐
  │  Flask  │─────────────────────►│ProductService │
  │  Route  │                      │               │
  └─────────┘                      │  1. cache.get │──► Redis ──► HIT? return
                                   │               │              MISS? ↓
                                   │  2. db.get    │──► RDS ─────────────────►
                                   │               │              result
                                   │  3. cache.set │──► Redis (TTL)
                                   │               │
                                   └───────────────┘
"""

import logging
from typing import Any, Dict, Optional, Tuple

from cache_manager import CacheManager
from db_manager import DatabaseManager
from config import Config

logger = logging.getLogger(__name__)


class ProductService:

    def __init__(self, db: DatabaseManager, cache: CacheManager, cfg: Config) -> None:
        self._db    = db
        self._cache = cache
        self._cfg   = cfg

    # ------------------------------------------------------------------
    # get_product  — Single product fetch with Lazy Loading
    # ------------------------------------------------------------------

    def get_product(self, product_id: int) -> Optional[Dict[str, Any]]:
        """
        Lazy Loading implementation for a single product.

        Step 1: Construct the canonical cache key.
        Step 2: Delegate to cache_manager.get_or_load() which encapsulates:
                  → Redis GET
                  → On miss: call loader (RDS query) + Redis SET with TTL
        Step 3: Return (value, cache_status) dict to the route.

        WHY delegate to get_or_load() instead of explicit get/set?
          The service layer stays clean. All retry / error handling is
          in CacheManager, which can be independently tested.
        """
        key = self._cache.make_key("product", "detail", product_id)

        # Lazy load — the lambda is the "loader" called only on cache miss
        value, status = self._cache.get_or_load(
            key       = key,
            loader    = lambda: self._db.get_product_by_id(product_id),
            ttl       = self._cfg.TTL_PRODUCT_DETAIL,
            miss_label= f"product_detail:{product_id}",
        )

        if value is None:
            return None

        return {"data": value, "cache_status": status}

    # ------------------------------------------------------------------
    # list_products  — Category or full-catalogue listing
    # ------------------------------------------------------------------

    def list_products(
        self,
        category: str = "all",
        page    : int = 1,
        per_page: int = 20,
    ) -> Dict[str, Any]:
        """
        Lazy Loading for product lists.

        Cache key encodes category + page + per_page so different pages
        and page sizes get independent cache entries.
        Separate TTL (5 min) — lists change more often than detail pages.
        """
        key = self._cache.make_key("product", "list", category, page, per_page)

        if category == "all":
            loader = lambda: self._db.get_all_products(page, per_page)
        else:
            loader = lambda: self._db.get_products_by_category(category, page, per_page)

        value, status = self._cache.get_or_load(
            key       = key,
            loader    = loader,
            ttl       = self._cfg.TTL_PRODUCT_LIST,
            miss_label= f"product_list:{category}:p{page}",
        )

        return {"data": value or [], "cache_status": status}

    # ------------------------------------------------------------------
    # update_product  — Write-through cache invalidation
    # ------------------------------------------------------------------

    def update_product(self, product_id: int, fields: Dict) -> bool:
        """
        Update RDS first, then invalidate (not update) the cache.

        WHY invalidate instead of update?
          Cache-aside (write-through) would need to replicate the same
          business logic used on read. Invalidation is simpler and correct:
          the next read will re-fetch from RDS with fresh data.

        WHY RDS-first?
          If cache invalidation fails after a DB write, the next cache miss
          will re-populate with current DB data. The window of stale data
          is TTL seconds maximum.

          If we invalidated cache first and the DB write failed, we'd have
          an empty cache but no updated DB — inconsistency that's harder to detect.
        """
        # Capture category BEFORE update for cache invalidation scope
        category_slug = self._db.get_product_category(product_id)

        updated = self._db.update_product(product_id, fields)
        if not updated:
            return False

        # Evict stale cache entries
        self._cache.invalidate_product(product_id, category_slug)
        logger.info(
            "Product updated & cache invalidated | product_id=%d category=%s",
            product_id, category_slug,
        )
        return True

    # ------------------------------------------------------------------
    # delete_product  — Soft delete + cache eviction
    # ------------------------------------------------------------------

    def delete_product(self, product_id: int) -> bool:
        category_slug = self._db.get_product_category(product_id)
        deleted = self._db.delete_product(product_id)
        if not deleted:
            return False

        self._cache.invalidate_product(product_id, category_slug)
        logger.info(
            "Product soft-deleted & cache evicted | product_id=%d", product_id
        )
        return True
