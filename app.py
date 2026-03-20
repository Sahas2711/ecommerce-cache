"""
=============================================================================
  E-Commerce Product Catalog API — Side Cache (Lazy Loading) Pattern
  Stack : Python 3.11 | Flask | PostgreSQL (Amazon RDS) | Redis (ElastiCache)
  Author: Production-grade assignment implementation
=============================================================================
"""

import logging
import time
from flask import Flask, jsonify, request, g
from config import Config
from product_service import ProductService
from cache_manager import CacheManager
from db_manager import DatabaseManager
from middleware import setup_logging, RequestTimingMiddleware
from dotenv import load_dotenv
load_dotenv()
# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------

def create_app(config_object: Config = None) -> Flask:
    app = Flask(__name__)
    cfg = config_object or Config()
    app.config.from_object(cfg)

    # Structured logging (writes JSON to stdout — CloudWatch picks it up)
    setup_logging(cfg.LOG_LEVEL)
    logger = logging.getLogger(__name__)

    # Shared infrastructure clients (created once, reused across requests)
    db      = DatabaseManager(cfg)
    cache   = CacheManager(cfg)
    service = ProductService(db, cache, cfg)

    # Attach timing middleware (measures wall-clock latency per request)
    app.wsgi_app = RequestTimingMiddleware(app.wsgi_app)

    # ------------------------------------------------------------------
    # Health check  (load-balancer target group uses this)
    # ------------------------------------------------------------------
    @app.route("/health")
    def health():
        try:
            # RDS
            conn = db.get_connection()
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.close()
            conn.close()

            # Redis
            cache.client.ping()   # ✅ FIXED

            return {
                "status": "healthy",
                "rds_reachable": True,
                "redis_reachable": True
            }

        except Exception as e:
            import traceback
            traceback.print_exc()

            return {
                "status": "error",
                "message": str(e)
            }, 500

    # ------------------------------------------------------------------
    # GET /products/<id>  — primary demonstration endpoint
    # ------------------------------------------------------------------
    @app.route("/products/<int:product_id>", methods=["GET"])
    def get_product(product_id: int):
        start = time.perf_counter()
        result = service.get_product(product_id)
        elapsed_ms = (time.perf_counter() - start) * 1000

        if result is None:
            return jsonify({"error": "Product not found"}), 404

        return jsonify({
            "data"        : result["data"],
            "cache_status": result["cache_status"],   # HIT | MISS
            "latency_ms"  : round(elapsed_ms, 3),
        }), 200

    # ------------------------------------------------------------------
    # GET /products  — paginated list with cursor-based pagination
    # ------------------------------------------------------------------
    @app.route("/products", methods=["GET"])
    def list_products():
        category = request.args.get("category", "all")
        page     = int(request.args.get("page", 1))
        per_page = min(int(request.args.get("per_page", 20)), 100)  # hard cap at 100

        start  = time.perf_counter()
        result = service.list_products(category=category, page=page, per_page=per_page)
        elapsed_ms = (time.perf_counter() - start) * 1000

        return jsonify({
            "data"        : result["data"],
            "cache_status": result["cache_status"],
            "page"        : page,
            "per_page"    : per_page,
            "latency_ms"  : round(elapsed_ms, 3),
        }), 200

    # ------------------------------------------------------------------
    # PUT /products/<id>  — update + explicit cache invalidation
    # ------------------------------------------------------------------
    @app.route("/products/<int:product_id>", methods=["PUT"])
    def update_product(product_id: int):
        payload = request.get_json(force=True, silent=True)
        if not payload:
            return jsonify({"error": "Invalid JSON body"}), 400

        updated = service.update_product(product_id, payload)
        if not updated:
            return jsonify({"error": "Product not found or update failed"}), 404

        return jsonify({
            "message"     : "Product updated and cache invalidated",
            "product_id"  : product_id,
            "cache_action": "INVALIDATED",
        }), 200

    # ------------------------------------------------------------------
    # DELETE /products/<id>  — delete + cache eviction
    # ------------------------------------------------------------------
    @app.route("/products/<int:product_id>", methods=["DELETE"])
    def delete_product(product_id: int):
        deleted = service.delete_product(product_id)
        if not deleted:
            return jsonify({"error": "Product not found"}), 404
        return jsonify({"message": "Deleted", "cache_action": "EVICTED"}), 200

    # ------------------------------------------------------------------
    # POST /cache/flush  — admin: clear entire product namespace
    # (protect this with an IAM-signed header in real prod)
    # ------------------------------------------------------------------
    @app.route("/cache/flush", methods=["POST"])
    def flush_cache():
        flushed = cache.flush_namespace("product")
        return jsonify({"keys_deleted": flushed}), 200

    # ------------------------------------------------------------------
    # GET /cache/stats  — operational visibility
    # ------------------------------------------------------------------
    @app.route("/cache/stats", methods=["GET"])
    def cache_stats():
        return jsonify(cache.get_stats()), 200

    logger.info("Application started | RDS=%s | Redis=%s",
                cfg.RDS_HOST, cfg.REDIS_HOST)
    return app


# ---------------------------------------------------------------------------
# Entry point (Gunicorn is used in production — see gunicorn.conf.py)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    application = create_app()
    application.run(host="0.0.0.0", port=8080, debug=False)
