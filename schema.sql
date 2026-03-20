-- =============================================================================
--  schema.sql  — E-Commerce Product Catalog Schema
--  Target: Amazon RDS PostgreSQL 15
--  Run this ONCE after RDS instance creation.
-- =============================================================================

-- Extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_stat_statements";  -- Query performance monitoring

-- ---------------------------------------------------------------------------
-- categories
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS categories (
    id          SERIAL PRIMARY KEY,
    name        VARCHAR(100)  NOT NULL,
    slug        VARCHAR(100)  NOT NULL UNIQUE,      -- used in cache keys & URLs
    description TEXT,
    created_at  TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- products
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS products (
    id             SERIAL PRIMARY KEY,
    category_id    INT           NOT NULL REFERENCES categories(id) ON DELETE RESTRICT,
    name           VARCHAR(255)  NOT NULL,
    description    TEXT,
    price          NUMERIC(12,2) NOT NULL CHECK (price >= 0),
    stock_quantity INT           NOT NULL DEFAULT 0 CHECK (stock_quantity >= 0),
    sku            VARCHAR(100)  NOT NULL UNIQUE,
    image_url      TEXT,
    is_active      BOOLEAN       NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- Indexes — critical for query performance under high read load
-- ---------------------------------------------------------------------------

-- PK indexes are created automatically.

-- The most common query pattern: list products by category, newest first.
CREATE INDEX IF NOT EXISTS idx_products_category_active_created
    ON products (category_id, is_active, created_at DESC);

-- Direct lookup by SKU (used in inventory systems).
CREATE INDEX IF NOT EXISTS idx_products_sku
    ON products (sku) WHERE is_active = TRUE;

-- Partial index: only active products (saves index space & improves cache hit on filtered queries).
CREATE INDEX IF NOT EXISTS idx_products_active
    ON products (id) WHERE is_active = TRUE;

-- categories slug lookup
CREATE INDEX IF NOT EXISTS idx_categories_slug ON categories (slug);

-- ---------------------------------------------------------------------------
-- Trigger: auto-update updated_at on every UPDATE
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION update_timestamp()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_products_updated_at
    BEFORE UPDATE ON products
    FOR EACH ROW EXECUTE FUNCTION update_timestamp();

CREATE TRIGGER trg_categories_updated_at
    BEFORE UPDATE ON categories
    FOR EACH ROW EXECUTE FUNCTION update_timestamp();

-- ---------------------------------------------------------------------------
-- Seed data
-- ---------------------------------------------------------------------------
INSERT INTO categories (name, slug, description) VALUES
    ('Electronics',   'electronics',   'Phones, laptops, accessories'),
    ('Clothing',      'clothing',      'Men and women apparel'),
    ('Home & Kitchen','home-kitchen',  'Furniture, utensils, decor')
ON CONFLICT (slug) DO NOTHING;

INSERT INTO products (category_id, name, description, price, stock_quantity, sku) VALUES
    (1, 'Samsung Galaxy S24',   '6.2" AMOLED, 256 GB',    79999.00, 150, 'ELEC-SGS24-256'),
    (1, 'Apple MacBook Air M3', '13" Retina, 16 GB RAM',  109900.00, 60,  'ELEC-MBA-M3-16'),
    (2, 'Levi''s 511 Slim Jeans','Classic slim fit, 32x32', 3499.00, 300, 'CLO-LV511-3232'),
    (3, 'Prestige Induction',   '1800W, black mirror top', 2299.00, 200, 'HK-PRES-IND-18')
ON CONFLICT (sku) DO NOTHING;

-- ---------------------------------------------------------------------------
-- RDS Parameter Group recommendations (apply via AWS Console):
--   shared_buffers         = 25% of instance RAM
--   effective_cache_size   = 75% of instance RAM
--   max_connections        = 200   (set lower than default to avoid OOM)
--   rds.force_ssl          = 1     (enforce TLS — reject plaintext connections)
--   log_min_duration_statement = 1000  (log queries > 1 second for tuning)
--   pg_stat_statements.track = all     (track all query plans)
-- ---------------------------------------------------------------------------
