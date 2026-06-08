-- ============================================================
--  Chile Plantation IoT – PostgreSQL Schema
--  Database : chile_iot  |  PostgreSQL 14+
-- ============================================================

-- ── Enable extensions ────────────────────────────────────────
-- CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;

-- ──────────────────────────────────────────────────────────────
--  Table: gs3_measurements
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS gs3_measurements (
    id               BIGSERIAL       PRIMARY KEY,
    ts               TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    node_id          VARCHAR(64)     NOT NULL DEFAULT 'esp32-node-01',
    moisture_vwc     NUMERIC(7, 4)   NOT NULL
                       CHECK (moisture_vwc BETWEEN 0.0 AND 1.0),
    temperature_c    NUMERIC(6, 2)   NOT NULL
                       CHECK (temperature_c BETWEEN -40.0 AND 80.0),
    conductivity_ec  NUMERIC(8, 4)   NOT NULL
                       CHECK (conductivity_ec >= 0.0),
    raw_payload      JSONB           NULL,
    created_at       TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE  gs3_measurements                IS 'Time-series readings from Decagon GS3 soil sensors';
COMMENT ON COLUMN gs3_measurements.ts             IS 'Measurement timestamp with timezone (WIB = UTC+7)';
COMMENT ON COLUMN gs3_measurements.node_id        IS 'Unique identifier of the ESP32 sensor node';
COMMENT ON COLUMN gs3_measurements.moisture_vwc   IS 'Volumetric water content m3/m3 (0-1)';
COMMENT ON COLUMN gs3_measurements.temperature_c  IS 'Soil temperature in degrees Celsius';
COMMENT ON COLUMN gs3_measurements.conductivity_ec IS 'Bulk electrical conductivity in dS/m';
COMMENT ON COLUMN gs3_measurements.raw_payload    IS 'Original JSON payload received via XMPP';

-- ── Indexes ──────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_gs3_ts         ON gs3_measurements (ts DESC);
CREATE INDEX IF NOT EXISTS idx_gs3_node_ts    ON gs3_measurements (node_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_gs3_ts_brin    ON gs3_measurements USING BRIN (ts)
                                              WITH (pages_per_range = 128);

-- ──────────────────────────────────────────────────────────────
--  Table: gs3_alerts
-- ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS gs3_alerts (
    id              BIGSERIAL    PRIMARY KEY,
    ts              TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    node_id         VARCHAR(64)  NOT NULL,
    alert_type      VARCHAR(32)  NOT NULL,
    alert_level     VARCHAR(16)  NOT NULL DEFAULT 'WARNING',
    current_value   NUMERIC(10, 4) NOT NULL,
    threshold_value NUMERIC(10, 4) NOT NULL,
    message         TEXT         NOT NULL,
    acknowledged    BOOLEAN      NOT NULL DEFAULT FALSE,
    ack_at          TIMESTAMPTZ  NULL
);

CREATE INDEX IF NOT EXISTS idx_alerts_ts    ON gs3_alerts (ts DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_unack ON gs3_alerts (ts DESC) WHERE acknowledged = FALSE;

-- ──────────────────────────────────────────────────────────────
--  Views
-- ──────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW v_gs3_hourly AS
SELECT
    date_trunc('hour', ts)              AS hour,
    node_id,
    COUNT(*)                            AS reading_count,
    AVG(moisture_vwc)::NUMERIC(7,4)     AS avg_vwc,
    MIN(moisture_vwc)::NUMERIC(7,4)     AS min_vwc,
    MAX(moisture_vwc)::NUMERIC(7,4)     AS max_vwc,
    AVG(temperature_c)::NUMERIC(6,2)    AS avg_temp,
    MIN(temperature_c)::NUMERIC(6,2)    AS min_temp,
    MAX(temperature_c)::NUMERIC(6,2)    AS max_temp,
    AVG(conductivity_ec)::NUMERIC(8,4)  AS avg_ec,
    MIN(conductivity_ec)::NUMERIC(8,4)  AS min_ec,
    MAX(conductivity_ec)::NUMERIC(8,4)  AS max_ec
FROM gs3_measurements
GROUP BY 1, 2
ORDER BY 1 DESC, 2;

CREATE OR REPLACE VIEW v_gs3_latest AS
SELECT DISTINCT ON (node_id)
    id, ts, node_id, moisture_vwc, temperature_c, conductivity_ec
FROM gs3_measurements
ORDER BY node_id, ts DESC;

-- ──────────────────────────────────────────────────────────────
--  Function: fn_insert_gs3
-- ──────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION fn_insert_gs3(
    p_node_id     VARCHAR,
    p_vwc         NUMERIC,
    p_temp        NUMERIC,
    p_ec          NUMERIC,
    p_raw_payload JSONB       DEFAULT NULL,
    p_ts          TIMESTAMPTZ DEFAULT NOW()
)
RETURNS BIGINT LANGUAGE plpgsql AS $$
DECLARE v_id BIGINT;
BEGIN
    INSERT INTO gs3_measurements (ts, node_id, moisture_vwc, temperature_c, conductivity_ec, raw_payload)
    VALUES (p_ts, p_node_id, p_vwc, p_temp, p_ec, p_raw_payload)
    RETURNING id INTO v_id;
    RETURN v_id;
END;
$$;

-- ──────────────────────────────────────────────────────────────
--  View: v_gs3_stats   (dashboard KPI cards)
-- ──────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW v_gs3_stats AS
SELECT
    COUNT(*)                           AS total_readings,
    MIN(ts)                            AS first_reading,
    MAX(ts)                            AS last_reading,
    AVG(moisture_vwc)::NUMERIC(7,4)    AS avg_vwc,
    MIN(moisture_vwc)::NUMERIC(7,4)    AS min_vwc,
    MAX(moisture_vwc)::NUMERIC(7,4)    AS max_vwc,
    AVG(temperature_c)::NUMERIC(6,2)   AS avg_temp,
    MIN(temperature_c)::NUMERIC(6,2)   AS min_temp,
    MAX(temperature_c)::NUMERIC(6,2)   AS max_temp,
    AVG(conductivity_ec)::NUMERIC(8,4) AS avg_ec,
    MIN(conductivity_ec)::NUMERIC(8,4) AS min_ec,
    MAX(conductivity_ec)::NUMERIC(8,4) AS max_ec,
    (SELECT COUNT(*) FROM gs3_alerts WHERE acknowledged = FALSE) AS unacked_alerts
FROM gs3_measurements;

-- ──────────────────────────────────────────────────────────────
--  View: v_gs3_history_24h  (chart data, downsampled ~100 pts)
-- ──────────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW v_gs3_history_24h AS
WITH numbered AS (
    SELECT
        ts,
        moisture_vwc,
        temperature_c,
        conductivity_ec,
        ROW_NUMBER() OVER (ORDER BY ts) AS rn,
        COUNT(*) OVER () AS total
    FROM gs3_measurements
    WHERE ts >= NOW() - INTERVAL '24 hours'
)
SELECT ts, moisture_vwc, temperature_c, conductivity_ec
FROM numbered
WHERE rn % GREATEST(total / 100, 1) = 0
   OR rn = total
ORDER BY ts ASC;

-- ── Seed demo data (24 h of readings) ───────────────────────
INSERT INTO gs3_measurements (ts, node_id, moisture_vwc, temperature_c, conductivity_ec, raw_payload)
SELECT
    NOW() - (n || ' minutes')::INTERVAL,
    'esp32-node-01',
    ROUND((0.65 + 0.15*SIN(n*0.05) + RANDOM()*0.02)::NUMERIC, 4),
    ROUND((26.0 + 4.0*SIN(n*0.03)  + RANDOM()*0.5 )::NUMERIC, 2),
    ROUND((1.20 + 0.40*COS(n*0.04) + RANDOM()*0.03)::NUMERIC, 4),
    NULL
FROM generate_series(1, 1440) AS n;

SELECT COUNT(*) AS total_rows, MIN(ts) AS oldest, MAX(ts) AS newest FROM gs3_measurements;
