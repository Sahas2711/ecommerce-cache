# gunicorn.conf.py — Production Gunicorn configuration.
# Start with: gunicorn -c gunicorn.conf.py "app:create_app()"

import multiprocessing

# ── Workers ────────────────────────────────────────────────────────────────
# Formula: (2 × vCPU) + 1  → t3.medium has 2 vCPU → 5 workers
workers     = (2 * multiprocessing.cpu_count()) + 1
worker_class = "gevent"          # async I/O; each worker handles 1000 green threads
worker_connections = 1000        # concurrent connections per worker
threads     = 1                  # gevent handles concurrency internally

# ── Binding ────────────────────────────────────────────────────────────────
bind        = "0.0.0.0:8080"
backlog     = 2048               # queue size for connections not yet accepted

# ── Timeouts ───────────────────────────────────────────────────────────────
timeout     = 30                 # worker killed if silent > 30 s
keepalive   = 5                  # keep TCP connection alive for 5 s (ALB uses 60 s)
graceful_timeout = 30            # time allowed for in-flight requests on shutdown

# ── Logging ────────────────────────────────────────────────────────────────
accesslog   = "-"                # stdout → CloudWatch
errorlog    = "-"
loglevel    = "info"
access_log_format = (
    '{"time":"%(t)s","method":"%(m)s","path":"%(U)s","status":%(s)s,'
    '"bytes":%(B)s,"duration_ms":%(D)s,"referer":"%(f)s","ip":"%(h)s"}'
)

# ── Process management ─────────────────────────────────────────────────────
preload_app = True               # load app once in master; workers fork (saves RAM)
max_requests = 1000              # restart worker after N requests (prevents memory leaks)
max_requests_jitter = 100        # stagger restarts to avoid all workers recycling together
