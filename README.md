# Side Cache (Lazy Loading) — Complete Production Solution
## Amazon RDS + ElastiCache Redis | Python Flask | E-Commerce Platform

---

# SECTION 1: ARCHITECTURE DESIGN

## 1.1 High-Level Architecture Overview

```
                         ┌──────────────────────────────────────────────────────┐
                         │              AWS REGION (ap-south-1)                 │
                         │                                                      │
  Internet               │   ┌─────────────────────────────────────────────┐   │
  Users ──► Route 53 ───►│   │          VPC: 10.0.0.0/16                   │   │
                         │   │                                              │   │
                         │   │  ┌─────────────────────────────────────────┐│   │
                         │   │  │   PUBLIC SUBNETS (10.0.1.0/24 + /24)    ││   │
                         │   │  │                                          ││   │
                         │   │  │  ┌──────────────┐  ┌──────────────────┐ ││   │
                         │   │  │  │  ALB (HTTPS) │  │  NAT Gateway     │ ││   │
                         │   │  │  │  Port 443    │  │  (outbound only) │ ││   │
                         │   │  │  └──────┬───────┘  └──────────────────┘ ││   │
                         │   │  └─────────┼───────────────────────────────┘│   │
                         │   │            │ (port 8080)                     │   │
                         │   │  ┌─────────▼───────────────────────────────┐│   │
                         │   │  │  PRIVATE SUBNETS — App (10.0.10.0/24)   ││   │
                         │   │  │                                          ││   │
                         │   │  │  ┌────────────────────────────────────┐  ││   │
                         │   │  │  │  EC2 Auto Scaling Group            │  ││   │
                         │   │  │  │  t3.medium (2 vCPU / 4 GB RAM)     │  ││   │
                         │   │  │  │                                    │  ││   │
                         │   │  │  │  Flask + Gunicorn + gevent         │  ││   │
                         │   │  │  │  Python 3.11  |  Port 8080         │  ││   │
                         │   │  │  └──────┬───────────────┬─────────────┘  ││   │
                         │   │  └─────────┼───────────────┼───────────────┘│   │
                         │   │            │               │                  │   │
                         │   │  ┌─────────▼──────────┐  ┌▼────────────────┐│   │
                         │   │  │ PRIVATE SUBNETS    │  │ PRIVATE SUBNETS ││   │
                         │   │  │ Data — RDS         │  │ Data — Cache    ││   │
                         │   │  │ 10.0.20.0/24       │  │ 10.0.30.0/24   ││   │
                         │   │  │                    │  │                 ││   │
                         │   │  │  RDS PostgreSQL 15  │  │  ElastiCache    ││   │
                         │   │  │  db.t3.medium       │  │  Redis 7.x      ││   │
                         │   │  │  Multi-AZ           │  │  cache.t3.small ││   │
                         │   │  │  Encrypted (AES256) │  │  Cluster Mode   ││   │
                         │   │  │  Read Replica (AZ2) │  │  Encrypted TLS  ││   │
                         │   │  └────────────────────┘  └─────────────────┘│   │
                         │   └─────────────────────────────────────────────┘   │
                         └──────────────────────────────────────────────────────┘
```

## 1.2 Design Decision Rationale

### VPC with Separate Public / Private Layers
**WHY:** Defence-in-depth. Public subnets hold only ALB and NAT Gateway — components that legitimately need internet access. EC2, RDS, and Redis live entirely in private subnets with no internet route. An attacker who compromises the ALB cannot directly reach the DB layer because there is no routable path.

### Three-Tier Private Subnet Isolation (App / RDS / Cache)
**WHY:** Even inside the VPC, subnets are isolated by purpose. If an attacker pivots from the app tier, the only resources reachable are those explicitly allowed by Security Group rules — not a free-for-all inside the VPC.

### Application Load Balancer (Not NLB)
**WHY:** ALB terminates HTTPS, handles SSL offloading, performs content-based routing, and integrates with WAF. All traffic between ALB and EC2 stays within the VPC (port 8080, HTTP) — no double encryption overhead.

### RDS Multi-AZ
**WHY:** Synchronous replication to a standby in a second AZ. Failover is automatic (~30 s). Without this, a single AZ outage takes down the database.

### ElastiCache Redis Cluster Mode
**WHY:** Cluster mode shards data across nodes. If the catalogue grows to millions of SKUs, a single Redis node's 32 GB RAM would be the bottleneck. Cluster mode allows horizontal scaling of memory.

### NAT Gateway (not NAT Instance)
**WHY:** Managed service — no patching, no single point of failure, scales automatically. Private EC2 instances need outbound internet access to pull OS patches and install pip packages at bootstrap.

---

# SECTION 2: SECURITY DESIGN

## 2.1 Security Group Matrix

| SG Name         | Inbound Rules                                     | Outbound Rules               |
|-----------------|---------------------------------------------------|------------------------------|
| sg-alb          | 0.0.0.0/0 → TCP 443 (HTTPS)                      | sg-appserver → TCP 8080      |
| sg-appserver    | sg-alb → TCP 8080                                 | sg-rds → TCP 5432            |
|                 |                                                   | sg-redis → TCP 6379          |
|                 |                                                   | 0.0.0.0/0 → TCP 443 (HTTPS) |
| sg-rds          | sg-appserver → TCP 5432 ONLY                      | NONE                         |
| sg-redis        | sg-appserver → TCP 6379 ONLY                      | NONE                         |
| sg-bastion      | YOUR_OFFICE_IP/32 → TCP 22                        | sg-rds → TCP 5432            |
|                 |                                                   | sg-appserver → TCP 22        |

## 2.2 Why Each Rule Exists

**sg-alb inbound 0.0.0.0/0:443** — Users reach the load balancer from anywhere. The ALB is the only internet-facing component.

**sg-appserver inbound from sg-alb:8080** — The app only accepts traffic from the ALB, not from the internet directly. Even if someone discovers the EC2 IP, they cannot bypass the ALB.

**sg-rds inbound from sg-appserver:5432** — RDS listens ONLY to the app EC2 security group. No other resource in the VPC — not the ALB, not a bastion accidentally — can reach the database unless explicitly added to sg-rds.

**sg-redis inbound from sg-appserver:6379** — Same principle for ElastiCache. The Redis port is not reachable from RDS, from the bastion, or from any other tier.

**sg-rds / sg-redis: NO outbound rules** — These data stores do not initiate any connections. Zero egress is the correct setting for services that only respond.

**sg-appserver outbound 443** — EC2 instances need to reach AWS SSM, Secrets Manager, and S3 (for code deployments) over TLS. The specific AWS service VPC endpoints could replace this if you want zero internet egress.

## 2.3 Principle of Least Privilege Applied

1. **Network level**: Private subnets have no Internet Gateway route.
2. **Security Group level**: Source is always another SG, never 0.0.0.0/0, for data tiers.
3. **IAM level**: EC2 instance profile has only: `secretsmanager:GetSecretValue`, `ssm:GetParameter`, `cloudwatch:PutMetricData`. No `*` permissions.
4. **Database level**: App DB user has only `SELECT, INSERT, UPDATE, DELETE` on the `ecommerce` schema. No `DROP TABLE`, `CREATE`, or `pg_read_all_data`.
5. **Redis level**: AUTH token required. No open Redis (no auth is a critical misconfiguration).
6. **Encryption at rest**: RDS encrypted with AWS KMS CMK. ElastiCache encrypted at rest. EC2 EBS volumes encrypted.
7. **Encryption in transit**: ALB enforces TLS 1.2+. App connects to RDS with `sslmode=require`. App connects to Redis with `ssl=True`.

---

# SECTION 3: STEP-BY-STEP AWS CONSOLE SETUP

## 3.1 VPC Creation

**Navigate:** VPC → Your VPCs → Create VPC

```
VPC Settings:
  Name tag          : ecommerce-vpc
  IPv4 CIDR         : 10.0.0.0/16
  IPv6              : No IPv6
  Tenancy           : Default
```

### Create Internet Gateway
**Navigate:** VPC → Internet Gateways → Create
```
  Name: ecommerce-igw
  Action: Attach to ecommerce-vpc
```

## 3.2 Subnet Configuration

Create 6 subnets across 2 Availability Zones:

| Name                     | CIDR           | AZ         | Type    |
|--------------------------|----------------|------------|---------|
| public-subnet-az1        | 10.0.1.0/24    | ap-south-1a| Public  |
| public-subnet-az2        | 10.0.2.0/24    | ap-south-1b| Public  |
| private-app-subnet-az1   | 10.0.10.0/24   | ap-south-1a| Private |
| private-app-subnet-az2   | 10.0.11.0/24   | ap-south-1b| Private |
| private-data-subnet-az1  | 10.0.20.0/24   | ap-south-1a| Private |
| private-data-subnet-az2  | 10.0.21.0/24   | ap-south-1b| Private |

**Navigate:** VPC → Subnets → Create subnet (repeat for each)

### Route Tables

**Public Route Table** (attached to public subnets):
```
  Destination: 0.0.0.0/0  → Target: ecommerce-igw
```

**Private Route Table** (attached to all private subnets):
```
  Destination: 0.0.0.0/0  → Target: NAT Gateway
  (NAT Gateway must be placed in a PUBLIC subnet with an Elastic IP)
```

### NAT Gateway Setup
**Navigate:** VPC → NAT Gateways → Create NAT Gateway
```
  Name       : ecommerce-nat-gw
  Subnet     : public-subnet-az1   ← MUST be public subnet
  Elastic IP : Allocate Elastic IP (click button)
```

## 3.3 Security Group Creation

**Navigate:** EC2 → Security Groups → Create security group

### sg-alb
```
VPC: ecommerce-vpc
Inbound Rules:
  Type: HTTPS  | Protocol: TCP | Port: 443  | Source: 0.0.0.0/0
Outbound Rules:
  Type: Custom TCP | Protocol: TCP | Port: 8080 | Destination: sg-appserver
```

### sg-appserver
```
VPC: ecommerce-vpc
Inbound Rules:
  Type: Custom TCP | Protocol: TCP | Port: 8080 | Source: sg-alb
Outbound Rules:
  Type: PostgreSQL | Protocol: TCP | Port: 5432 | Destination: sg-rds
  Type: Custom TCP | Protocol: TCP | Port: 6379 | Destination: sg-redis
  Type: HTTPS      | Protocol: TCP | Port: 443  | Destination: 0.0.0.0/0
```

### sg-rds
```
VPC: ecommerce-vpc
Inbound Rules:
  Type: PostgreSQL | Protocol: TCP | Port: 5432 | Source: sg-appserver
Outbound Rules: (none — remove default)
```

### sg-redis
```
VPC: ecommerce-vpc
Inbound Rules:
  Type: Custom TCP | Protocol: TCP | Port: 6379 | Source: sg-appserver
Outbound Rules: (none — remove default)
```

## 3.4 RDS Setup (PostgreSQL 15)

**Navigate:** RDS → Create database

```
Engine options:
  Engine type    : PostgreSQL
  Version        : PostgreSQL 15.x (latest minor)

Templates:
  Template       : Production

Settings:
  DB identifier  : ecommerce-db
  Master username: dbadmin
  Master password: [Use Secrets Manager — see below]

Instance configuration:
  Class          : db.t3.medium (2 vCPU, 4 GB RAM)
                   WHY t3.medium: adequate for dev/assignment; use db.r6g.large in real prod

Storage:
  Type           : gp3  (NOT gp2 — gp3 is cheaper and consistent IOPS)
  Allocated      : 100 GB
  Enable autoscaling: YES | Max: 500 GB
  Encryption     : Enable | KMS key: aws/rds (or CMK)

Availability & durability:
  Multi-AZ       : Create a standby instance  ← CRITICAL for prod

Connectivity:
  VPC                 : ecommerce-vpc
  Subnet group        : Create new
    → Include: private-data-subnet-az1, private-data-subnet-az2
  Public access       : NO  ← CRITICAL: never expose RDS publicly
  VPC security group  : sg-rds
  Port                : 5432

Database authentication:
  Password authentication  (use IAM auth in real prod)

Additional configuration:
  Initial database name : ecommerce
  Parameter group       : Create new parameter group (see params below)
  Backup retention      : 7 days
  Enable automated backups: YES
  Backup window         : 02:00–03:00 UTC (low-traffic window)
  Maintenance window    : Mon 03:00–04:00 UTC
  Enable encryption     : YES
  Enable Performance Insights: YES (retention: 7 days free)
  Enable Enhanced Monitoring : YES | Granularity: 60 s
  Enable deletion protection : YES
```

### RDS Parameter Group (create before RDS instance):
**Navigate:** RDS → Parameter Groups → Create parameter group
```
Family: postgres15

Parameters to modify:
  rds.force_ssl                    = 1        (enforce TLS)
  shared_buffers                   = 131072   (25% of 4 GB = 1 GB in 8 KB pages)
  effective_cache_size             = 393216   (75% of 4 GB)
  max_connections                  = 200
  log_min_duration_statement       = 1000     (log queries > 1 s)
  log_connections                  = on
  log_disconnections               = on
  pg_stat_statements.track         = all
```

### Store RDS Password in Secrets Manager:
**Navigate:** Secrets Manager → Store a new secret
```
  Secret type    : Credentials for Amazon RDS database
  Username       : dbadmin
  Password       : [your password]
  Database       : ecommerce-db
  Secret name    : prod/ecommerce/rds-credentials
```

## 3.5 ElastiCache Redis Setup

**Navigate:** ElastiCache → Redis OSS caches → Create

```
Cluster settings:
  Creation method    : Easy create → NO, use Custom
  Cluster mode       : Enabled (allows sharding for horizontal scale)
  Name               : ecommerce-redis
  Description        : Product catalog cache

Location:
  AWS Cloud

Cluster info:
  Port               : 6379
  Parameter group    : default.redis7.cluster.on  (or create custom)
  Node type          : cache.t3.small (1.37 GB RAM)
                       WHY: for product catalog of ~50k SKUs, this is enough.
                       Use cache.r6g.large for production with millions of SKUs.
  Number of shards   : 1  (scale to 3+ in prod)
  Replicas per shard : 1  (promotes to primary on failure)

Multi-AZ:
  Enable Multi-AZ    : YES
  Auto-failover      : YES

Subnet group:
  Create new subnet group
  Name    : ecommerce-cache-subnets
  Subnets : private-data-subnet-az1, private-data-subnet-az2

Security:
  VPC security groups : sg-redis
  Encryption at rest  : YES | KMS: aws/elasticache (or CMK)
  Encryption in transit: YES (TLS required)

Access Control:
  Redis AUTH         : YES
  Auth token         : [generate 32+ char random string]
  → Store this in Secrets Manager: prod/ecommerce/redis-auth-token

Backup:
  Enable automatic backups: YES
  Retention period        : 3 days
  Backup window           : 03:00–04:00 UTC

Maintenance:
  Maintenance window : Tue 04:00–05:00 UTC
  Auto minor version upgrade: YES

Logs:
  Slow log  → CloudWatch Log Group: /ecommerce/redis/slow-log
  Engine log → CloudWatch Log Group: /ecommerce/redis/engine-log
```

## 3.6 EC2 Setup (Application Server)

### IAM Instance Profile (create first):
**Navigate:** IAM → Roles → Create role
```
  Trusted entity : AWS service → EC2
  Policies to attach:
    AmazonSSMManagedInstanceCore          (SSM Session Manager — no SSH needed)
    CloudWatchAgentServerPolicy           (metrics + logs)
    Custom inline policy:
      {
        "Version": "2012-10-17",
        "Statement": [
          {
            "Effect": "Allow",
            "Action": ["secretsmanager:GetSecretValue"],
            "Resource": [
              "arn:aws:secretsmanager:ap-south-1:ACCOUNT:secret:prod/ecommerce/*"
            ]
          }
        ]
      }
  Role name: ecommerce-ec2-role
```

### Launch Template:
**Navigate:** EC2 → Launch Templates → Create
```
  AMI                : Amazon Linux 2023 (latest)
  Instance type      : t3.medium
  Key pair           : [create or select existing]
  VPC                : ecommerce-vpc
  Subnet             : private-app-subnet-az1
  Security group     : sg-appserver
  IAM instance profile: ecommerce-ec2-role

  EBS Volume:
    Size       : 30 GB
    Type       : gp3
    Encrypted  : YES

  User Data (bootstrap script):
    #!/bin/bash
    set -ex
    yum update -y
    yum install -y python3.11 python3.11-pip git

    # Fetch secrets from Secrets Manager (no credentials in code)
    RDS_SECRET=$(aws secretsmanager get-secret-value \
      --secret-id prod/ecommerce/rds-credentials \
      --region ap-south-1 --query SecretString --output text)

    REDIS_TOKEN=$(aws secretsmanager get-secret-value \
      --secret-id prod/ecommerce/redis-auth-token \
      --region ap-south-1 --query SecretString --output text)

    # Set env vars for the application (or use a .env file / SSM)
    cat > /etc/ecommerce.env << EOF
    export RDS_HOST=$(echo $RDS_SECRET | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['host'])")
    export RDS_PORT=5432
    export RDS_DB=ecommerce
    export RDS_USER=dbadmin
    export RDS_PASSWORD=$(echo $RDS_SECRET | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['password'])")
    export REDIS_HOST=ecommerce-redis.xxxxxx.0001.apse1.cache.amazonaws.com
    export REDIS_PORT=6379
    export REDIS_AUTH_TOKEN=$REDIS_TOKEN
    export REDIS_SSL=true
    export APP_ENV=prod
    export LOG_LEVEL=INFO
    export FLASK_SECRET_KEY=$(openssl rand -hex 32)
    EOF

    # Clone and set up the application
    git clone https://github.com/your-org/ecommerce-cache /opt/ecommerce
    cd /opt/ecommerce
    pip3.11 install -r requirements.txt

    # Run schema migration
    source /etc/ecommerce.env
    psql "host=$RDS_HOST dbname=$RDS_DB user=$RDS_USER password=$RDS_PASSWORD sslmode=require" -f schema.sql

    # Install and start application as systemd service
    cp ecommerce.service /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable ecommerce
    systemctl start ecommerce
```

### Application Load Balancer:
**Navigate:** EC2 → Load Balancers → Create ALB
```
  Scheme             : Internet-facing
  VPC                : ecommerce-vpc
  Subnets            : public-subnet-az1, public-subnet-az2
  Security group     : sg-alb
  HTTPS Listener     : 443
    → SSL certificate: ACM certificate for your domain
    → Default action : Forward to target group

  Target Group:
    Name              : ecommerce-tg
    Protocol          : HTTP
    Port              : 8080
    Health check path : /health
    Healthy threshold : 2
    Unhealthy threshold: 3
    Timeout           : 5 s
    Interval          : 30 s
```

### Auto Scaling Group:
**Navigate:** EC2 → Auto Scaling Groups → Create
```
  Launch template    : [the one created above]
  VPC                : ecommerce-vpc
  Subnets            : private-app-subnet-az1, private-app-subnet-az2
  Load balancer      : attach to ecommerce-tg
  Min capacity       : 2  (always multi-instance for HA)
  Desired capacity   : 2
  Max capacity       : 10
  Scaling policy     : Target tracking
    Metric            : Average CPU utilization
    Target value      : 60%
```

---

# SECTION 4: APPLICATION CODE

## 4.1 Project Structure

```
ecommerce_cache/
├── app.py              # Flask app factory + routes
├── config.py           # Config from env vars (no hardcoded values)
├── cache_manager.py    # Redis client, get_or_load(), key naming, jitter
├── db_manager.py       # PostgreSQL client, connection pool, retry
├── product_service.py  # Business logic — orchestrates cache + DB
├── middleware.py       # JSON logging, request timing
├── benchmark.py        # Latency measurement script
├── schema.sql          # RDS table definitions + indexes
├── requirements.txt    # Pinned dependencies
└── gunicorn.conf.py    # Production WSGI config
```

## 4.2 Lazy Loading Flow (Code Trace)

When `GET /products/42` arrives:

```
1. app.py:get_product(42)
     ↓ calls
2. product_service.py:ProductService.get_product(42)
     ↓ builds key: "prod:v1:product:detail:42"
     ↓ calls
3. cache_manager.py:CacheManager.get_or_load(key, loader, ttl)
     ↓
   3a. redis_client.GET("prod:v1:product:detail:42")
       → HIT  → deserialise JSON → return (data, "HIT")
       → MISS → call loader()
                  ↓
   3b. db_manager.py:DatabaseManager.get_product_by_id(42)
       → executes parameterised SQL
       → returns dict or None
                  ↓
   3c. redis_client.SETEX(key, ttl + jitter, json.dumps(data))
       → return (data, "MISS")
     ↓
4. app.py returns JSON: {data, cache_status, latency_ms}
```

---

# SECTION 5: TTL AND CACHE STRATEGY

## 5.1 TTL Values and Justification

| Cache Entry        | TTL        | Reason                                                            |
|--------------------|------------|-------------------------------------------------------------------|
| Product detail     | 30 minutes | Admin updates are rare; explicit invalidation handles immediate changes |
| Category list      | 5 minutes  | New products added more frequently than single-product changes     |
| Search results     | 1 minute   | Highly dynamic; prices change, new products appear                 |
| Hot/Featured items | 1 hour     | Selected manually by marketing; very stable                        |
| Inventory count    | 30 seconds | Changes on every purchase; long TTL = overselling risk             |

## 5.2 TTL Jitter (Thundering Herd Prevention)

**Problem:** If 10,000 product pages all expire at the same time (e.g., after a cache flush), 10,000 simultaneous DB queries are triggered — potentially crashing RDS.

**Solution:** Add random 0–60 seconds jitter to every TTL.
```python
effective_ttl = base_ttl + random.randint(0, TTL_JITTER_RANGE)
```

Key A expires at: 1800 + 23 = 1823 s
Key B expires at: 1800 + 51 = 1851 s
Key C expires at: 1800 + 7  = 1807 s
→ Spread across 60 s → only ~167 misses/second instead of all at once.

## 5.3 Cache Invalidation Strategy

**Write-Through Invalidation (not update):**
On `PUT /products/42`:
1. DB UPDATE runs first (source of truth updated)
2. `cache.delete("prod:v1:product:detail:42")` — key is evicted
3. Next GET /products/42 is a cache miss → re-fetches fresh data from RDS
4. Cache is re-populated with correct data

**WHY delete, not update?**
Updating the cache requires re-running the same query or logic used on read. Deleting is simpler, atomic, and guarantees consistency — the next read always gets DB truth.

**Namespace Flush (emergency):**
If a bulk price update affects thousands of products, flush the entire namespace:
```
POST /cache/flush   → uses SCAN to delete all prod:v1:product:* keys
```
SCAN is used instead of KEYS because KEYS blocks Redis on large keyspaces.

## 5.4 Cache Eviction Policy

Configure ElastiCache Redis with `maxmemory-policy = allkeys-lru`:
- When Redis memory is full, evict the Least Recently Used key
- This ensures hot products stay cached; rarely-accessed products are evicted
- Alternative: `volatile-lru` (only evicts keys with TTL set) — use if some keys must never be evicted (e.g., session data)

---

# SECTION 6: PERFORMANCE DEMONSTRATION

## 6.1 Running the Benchmark

```bash
# First request (cold cache) — measures cache MISS (RDS hit)
curl -w "\nTime: %{time_total}s\n" http://localhost:8080/products/1

# Second request (warm cache) — measures cache HIT
curl -w "\nTime: %{time_total}s\n" http://localhost:8080/products/1

# Run full benchmark (30 iterations)
python benchmark.py --base-url http://ec2-xx-xx-xx-xx.compute.amazonaws.com --product-id 1 --iterations 30
```

## 6.2 Expected Results

```
=================================================================
  Benchmark: GET http://ec2-host/products/1
  Iterations: 30
=================================================================
#     Cache    Latency (ms)    HTTP
----  ------  --------------  ------
   1    MISS         247.831     200  ◀ MISS  (cold cache, hits RDS)
   2     HIT           3.412     200
   3     HIT           2.891     200
   4     HIT           3.201     200
   5     HIT           2.750     200
  ...
  30     HIT           3.100     200

=================================================================
  RESULTS SUMMARY  (product_id=1)
=================================================================
  Metric                Cache HIT    Cache MISS
  --------------------  -----------  -----------
  Count                          29            1
  Min (ms)                    2.750      247.831
  Max (ms)                    4.102      247.831
  Mean (ms)                   3.085      247.831
  P95 (ms)                    3.900      247.831
  P99 (ms)                    4.102      247.831

  ✅ Speed improvement (MISS mean / HIT mean): 80.3×
=================================================================
```

## 6.3 Sample Structured Log Output

```json
{"timestamp":"2024-11-15T14:32:01","level":"INFO","logger":"cache_manager","message":"CACHE MISS | key=prod:v1:product:detail:1"}
{"timestamp":"2024-11-15T14:32:01","level":"INFO","logger":"cache_manager","message":"RDS query complete | ref=product_detail:1 | latency=242.31 ms"}
{"timestamp":"2024-11-15T14:32:01","level":"DEBUG","logger":"cache_manager","message":"CACHE SET  | key=prod:v1:product:detail:1 | ttl=1837 s"}
{"timestamp":"2024-11-15T14:32:01","level":"INFO","logger":"middleware","message":"REQUEST_COMPLETE","extra":{"method":"GET","path":"/products/1","status":200,"latency_ms":247.83,"request_id":"a1b2c3d4"}}

{"timestamp":"2024-11-15T14:32:02","level":"INFO","logger":"cache_manager","message":"CACHE HIT  | key=prod:v1:product:detail:1"}
{"timestamp":"2024-11-15T14:32:02","level":"INFO","logger":"middleware","message":"REQUEST_COMPLETE","extra":{"method":"GET","path":"/products/1","status":200,"latency_ms":3.41,"request_id":"b2c3d4e5"}}
```

**Querying logs in CloudWatch Logs Insights:**
```
fields @timestamp, message, extra.latency_ms, extra.cache_status
| filter extra.path like "/products"
| stats avg(extra.latency_ms), p95(extra.latency_ms), p99(extra.latency_ms) by extra.cache_status
| sort @timestamp desc
```

---

# SECTION 7: ADVANCED IMPROVEMENTS

## 7.1 Cache Key Naming Strategy

Pattern: `{env}:{version}:{namespace}:{entity}:{id}`

| Key                                    | Stores              |
|----------------------------------------|---------------------|
| prod:v1:product:detail:42              | Single product dict |
| prod:v1:product:list:electronics:1:20  | Page 1, 20 per page |
| prod:v1:product:list:all:2:20          | All products, page 2|
| prod:v1:product:search:laptop:1        | Search results      |

**Version bumping for zero-downtime schema changes:**
When the product schema changes (e.g., new field added), bump `CACHE_KEY_VERSION` from `v1` to `v2`. Old v1 keys expire naturally. New v2 keys are populated on first miss. No flush required — both versions co-exist during rollout.

## 7.2 Cache Hit/Miss Logging for Alerting

Set up a CloudWatch Metric Filter on the log group:
```
Filter pattern: { $.message = "CACHE MISS*" }
Metric namespace: EcommerceApp
Metric name: CacheMissCount
```

Create a CloudWatch Alarm:
```
Metric   : CacheMissCount
Threshold: > 500 misses/minute
Action   : SNS notification to on-call engineer
```
A sudden spike in cache misses means Redis is down, keys are expiring faster than expected, or an attacker is probing random product IDs (cache poisoning attempt).

## 7.3 Cache Stampede Protection (Advanced)

For extremely high traffic (>10,000 req/s), even the first MISS can overwhelm RDS if many threads detect the miss simultaneously. Solution: Mutex / probabilistic early expiration.

```python
# Advanced: use a Redis lock to serialize re-population
def get_or_load_with_lock(self, key, loader, ttl):
    value = self.get(key)
    if value is not None:
        return value, "HIT"

    lock_key = f"{key}:__lock"
    # Try to acquire lock (NX = only set if not exists)
    acquired = self._client.set(lock_key, "1", nx=True, ex=10)

    if acquired:
        try:
            value = loader()
            if value:
                self.set(key, value, ttl)
        finally:
            self._client.delete(lock_key)
        return value, "MISS"
    else:
        # Another thread is loading — wait briefly and retry from cache
        import time; time.sleep(0.05)
        value = self.get(key)
        return (value or loader()), "MISS_WAIT"
```

## 7.4 Scaling Considerations

**Vertical scaling (quick):** Upgrade ElastiCache from cache.t3.small to cache.r6g.large (26 GB RAM, network-optimised) — zero code change required.

**Horizontal scaling (sustained growth):**
- Increase ElastiCache shard count from 1 to 3 or 6 (consistent hashing distributes keys)
- Add RDS Read Replicas (1 per AZ); route all SELECTs to read replicas, writes to primary
- In the DB layer, point `get_*` methods to the replica endpoint URL

**Read Replica integration:**
```python
# In DatabaseManager, add a read-only pool
self._read_pool = self._build_pool(cfg, host=cfg.RDS_REPLICA_HOST)

# Route reads to replica, writes to primary
def get_product_by_id(self, product_id):
    return self._execute_with_retry(sql, (product_id,), pool=self._read_pool)
```

---

# SECTION 8: VIVA PREPARATION — 15 TOUGH PROFESSOR QUESTIONS

---

**Q1. Why did you choose Lazy Loading over Write-Through caching?**

Lazy Loading only caches data that is actually requested — it never pre-populates keys that may never be read. For a product catalogue with 100,000 SKUs where only the top 1,000 are viewed 95% of the time, Write-Through would waste Redis memory caching 99,000 rarely-requested products on every DB insert. Lazy Loading self-selects the hot data set. The trade-off: the first request after a cache miss is slow. We mitigate this with explicit invalidation on update (not waiting for TTL expiry).

---

**Q2. What is a cache stampede and how does your implementation prevent it?**

A cache stampede occurs when many concurrent requests detect a cache miss simultaneously and all query the database at once — amplifying load at the worst possible moment (just after a popular key expires). My implementation uses two mechanisms: TTL jitter (random 0–60 s spread so keys don't expire in bulk), and an optional Redis-lock mutex (`SET key NX EX`) so only one thread re-populates the cache while others wait. The lock has a 10 s expiry so it auto-releases if the thread dies.

---

**Q3. Your Redis Security Group allows port 6379 from sg-appserver. What if the app server is compromised?**

True — a compromised app server can read/write Redis. Defence-in-depth layers mitigate this: (a) ElastiCache requires an AUTH token, so the attacker also needs the token; (b) the token is stored in Secrets Manager with IAM-controlled access — not in the code; (c) ElastiCache TLS means traffic is encrypted in transit, so a network sniffer cannot capture the AUTH token; (d) Redis data is encrypted at rest with KMS. Critically, even with Redis access, the attacker cannot reach RDS — that's a separate security group.

---

**Q4. Why `SCAN` instead of `KEYS` for namespace flush?**

`KEYS pattern` is O(N) and blocks Redis's single-threaded event loop for the entire scan duration. For a Redis instance with 1 million keys, this blocks all other commands for hundreds of milliseconds — effectively a self-inflicted denial of service. `SCAN` iterates in batches of `count` keys per call, returning immediately. It's O(1) per call (amortized O(N) total), non-blocking, and production-safe.

---

**Q5. What happens to your service if ElastiCache goes down?**

The `CacheManager.get()` and `set()` methods wrap every Redis call in `try/except RedisError` and return `None` / `False` on error without raising. The service layer's `get_or_load()` detects `None` from get and calls the DB loader. The application degrades gracefully — every request becomes a cache miss and hits RDS directly, increasing latency to ~200–500 ms. The system continues to serve correct data. A CloudWatch alarm on `CacheMissCount` alerts engineers immediately.

---

**Q6. How does `sslmode=require` in psycopg2 relate to the RDS parameter `rds.force_ssl=1`?**

They work at different layers. `rds.force_ssl=1` is a server-side setting — RDS rejects any connection that doesn't negotiate TLS. `sslmode=require` is a client-side setting — psycopg2 initiates the TLS handshake. Both must be set. If you set only `rds.force_ssl=1`, a misconfigured client without `sslmode=require` would fail with a connection error (good — it fails secure). If you set only `sslmode=require`, a different PostgreSQL server without SSL configured would still accept the connection (bad). Both together guarantee encryption regardless of which side is misconfigured.

---

**Q7. Your DB user has SELECT, INSERT, UPDATE, DELETE. Why not just grant all privileges?**

Principle of least privilege. If an attacker achieves SQL injection through a query-building bug, they're limited to data manipulation — they cannot `DROP TABLE products`, `TRUNCATE`, `CREATE TABLE as SELECT` to exfiltrate schema, or call `pg_read_file()` to read OS files. Damage is bounded. In a real system you'd further split: a read-only user for GET endpoints, a read-write user for POST/PUT/DELETE, with the read-only credentials used for the majority of traffic.

---

**Q8. Why gp3 storage for RDS instead of io1/io2?**

gp3 provides 3,000 IOPS baseline at no extra cost regardless of volume size, and you can provision up to 16,000 IOPS independently of storage size. io1/io2 is more cost-effective only above ~16,000 IOPS. For a t3.medium RDS instance, the instance's network bandwidth is the bottleneck before storage IOPS becomes one — so gp3 is the correct cost-performance choice. io2 is reserved for large production instances with extreme IOPS requirements.

---

**Q9. What is the thundering herd in the context of RDS connection pooling, not just caching?**

On application server startup (or after a restart), all Gunicorn workers initialise simultaneously and each opens DB_POOL_MIN connections. With 5 workers and min=2, that's 10 simultaneous connection attempts. At autoscaling events (adding 5 EC2 instances), that's 50+ simultaneous connections. RDS has a `max_connections` limit tied to instance RAM (~87 for db.t3.medium by default). The solution: set `max_connections=200` in the parameter group, use `max_requests_jitter` in Gunicorn to stagger worker restarts, and implement connection pool overflow with a timeout.

---

**Q10. How does cache versioning (v1, v2 in key names) enable zero-downtime schema migrations?**

If I change the product JSON structure (e.g., add a `discount_price` field), old cached `v1` keys have the old schema. If I deploy new code that expects `discount_price`, it would fail on cached v1 data. By bumping `CACHE_KEY_VERSION` to `v2` in Config before deployment, new code writes and reads only v2 keys. v1 keys are never read by the new code; they expire naturally per their TTL. No flush is needed, no downtime. Old instances (if any remain briefly during rolling deploy) still use v1 keys. The two versions co-exist harmlessly.

---

**Q11. Why use Multi-AZ for both RDS and ElastiCache?**

A single AZ has hardware failures, power outages, and maintenance windows. Without Multi-AZ, an AZ failure takes down your database or cache for hours. With RDS Multi-AZ, AWS automatically fails over to the standby (in a different AZ) in ~30 seconds — the application sees a brief connection drop and reconnects. With ElastiCache Multi-AZ, a replica in another AZ is promoted automatically. For an e-commerce platform, 30 minutes of downtime could mean millions in lost revenue — Multi-AZ is non-negotiable.

---

**Q12. How does connection pool recycling (pool_recycle) prevent stale connection issues?**

Long-lived TCP connections are terminated by NAT Gateways, load balancers, or network appliances after idle timeouts (typically 350–900 seconds on AWS). If psycopg2 reuses a connection that was silently killed by the NAT Gateway, it gets a broken pipe error. `pool_recycle=1800` tells psycopg2 to close and re-open connections that are older than 30 minutes, preventing use of stale TCP connections. RDS also has an idle connection timeout — recycling ensures we stay under it.

---

**Q13. What is the difference between `allkeys-lru` and `volatile-lru` eviction policies?**

`volatile-lru`: evicts only keys with an expiry (TTL) set, using LRU order. Keys without TTL are never evicted. Use when some data (e.g., user sessions) must never be auto-evicted.

`allkeys-lru`: evicts any key using LRU order, regardless of TTL. Use when all keys in Redis are cache data — the most recently accessed data is most valuable. For our product catalog cache (all keys have TTL), `allkeys-lru` is more appropriate because it can always free memory by evicting cold data.

---

**Q14. Your benchmark shows 80× improvement. How do you know the improvement is from Redis and not from OS page cache or PostgreSQL's shared_buffers?**

Excellent point. PostgreSQL `shared_buffers` caches pages in RAM — repeated queries may return fast even without Redis. To isolate Redis's contribution: (a) run `SELECT pg_stat_reset()` to reset PostgreSQL statistics, then `SELECT * FROM pg_statio_user_tables` before and after — if `heap_blks_hit` (buffer cache hits) is high, the improvement is partly PostgreSQL's cache; (b) disable Redis in the app and measure DB-only latency with a warm PostgreSQL buffer cache — this is the true baseline for Redis comparison; (c) for a definitive test, restart the RDS instance (clears shared_buffers) and compare. In practice, we measure end-to-end application latency, which is what the user experiences.

---

**Q15. If a product price changes in RDS, what is the maximum time a user could see the old price?**

Without explicit invalidation: up to TTL_PRODUCT_DETAIL + TTL_JITTER = 1800 + 60 = 1860 seconds (31 minutes). With explicit invalidation (as implemented): near-zero — the `update_product` route calls `cache.invalidate_product()` immediately after the DB update. The next request after invalidation is a cache miss → fresh data from RDS. The window is the time between the DB commit and the cache delete — microseconds. In extreme correctness requirements (e.g., financial transactions), you'd use a queue (SQS) to ensure invalidation even if the app server crashes mid-update.

---

# APPENDIX: KEY PRODUCTION CHECKLIST

| Item                                      | Status |
|-------------------------------------------|--------|
| RDS in private subnet                     | ✅     |
| RDS public access = No                    | ✅     |
| RDS Multi-AZ enabled                      | ✅     |
| RDS encryption at rest (KMS)              | ✅     |
| RDS force_ssl = 1                         | ✅     |
| RDS password in Secrets Manager           | ✅     |
| ElastiCache in private subnet             | ✅     |
| ElastiCache TLS in-transit                | ✅     |
| ElastiCache encryption at rest            | ✅     |
| ElastiCache AUTH token                    | ✅     |
| Security Groups follow least privilege    | ✅     |
| No credentials in application code        | ✅     |
| TTL jitter implemented                    | ✅     |
| Cache MISS error handling (graceful)      | ✅     |
| Structured JSON logging to CloudWatch     | ✅     |
| Cache hit/miss metrics                    | ✅     |
| Namespace-scoped flush with SCAN (not KEYS)| ✅    |
| Connection pool recycling configured      | ✅     |
| Gunicorn production config (not dev server)| ✅    |
| Auto Scaling Group (min 2 instances)      | ✅     |
| Health check endpoint (/health)           | ✅     |
| Deletion protection on RDS                | ✅     |
#   e c o m m e r c e - c a c h e  
 