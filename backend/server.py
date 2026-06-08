#!/usr/bin/env python3
"""
============================================================
 CIMES – Chili Intelligent Monitoring and Environmental
          Sensing System
 Component : FastAPI Backend API
 Connects  : Dashboard ↔ PostgreSQL (or in-memory fallback)

 Endpoints:
   GET  /api/latest          → latest sensor reading
   GET  /api/history?hours=  → historical data for charts
   GET  /api/alerts?limit=   → alert log
   GET  /api/stats           → dashboard statistics
   POST /api/alerts/{id}/ack → acknowledge alert
   POST /api/ingest          → receive reading from bridge/simulator
   POST /api/alert           → receive alert from bridge/simulator
   GET  /api/health          → health check
   WS   /ws                  → WebSocket realtime updates
   GET  /                    → serve dashboard

 Run:
   cd backend && pip install -r requirements.txt
   python server.py

 The server automatically detects if PostgreSQL is available.
 If not, it runs with in-memory storage for demo/testing.
============================================================
"""

import asyncio
import json
import logging
import math
import os
import random
import sys
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ──────────────────────────────────────────────────────────────
#  Logging
# ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-8s] %(name)s – %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("cimes-api")

# ──────────────────────────────────────────────────────────────
#  Configuration
# ──────────────────────────────────────────────────────────────
DB_CONFIG = {
    "host":     os.getenv("DB_HOST",     "localhost"),
    "port":     int(os.getenv("DB_PORT", "5432")),
    "dbname":   os.getenv("DB_NAME",     "chile_iot"),
    "user":     os.getenv("DB_USER",     "faridazis"),
    "password": os.getenv("DB_PASSWORD", ""),
}

DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard"

# ──────────────────────────────────────────────────────────────
#  Storage Mode: PostgreSQL or In-Memory
# ──────────────────────────────────────────────────────────────
USE_DB = False  # Will be set during startup

# ── In-Memory Storage ────────────────────────────────────────
mem_readings = deque(maxlen=5000)   # Recent sensor readings
mem_alerts   = deque(maxlen=500)    # Recent alerts
mem_id_counter = 0

def mem_seed_data():
    """Seed in-memory store with 1440 demo points (24h)."""
    global mem_id_counter
    now = datetime.now(timezone.utc)
    for n in range(1440, 0, -1):
        mem_id_counter += 1
        t = n * 0.05
        ts = now - timedelta(minutes=n)
        mem_readings.append({
            "id": mem_id_counter,
            "ts": ts.isoformat(),
            "node_id": "esp32-node-01",
            "vwc":  round(0.65 + 0.15 * math.sin(t) + random.uniform(-0.01, 0.01), 4),
            "temp": round(26.0 + 4.0 * math.sin(t * 0.6) + random.uniform(-0.25, 0.25), 2),
            "ec":   round(1.20 + 0.40 * math.cos(t * 0.8) + random.uniform(-0.015, 0.015), 4),
        })
    log.info("In-memory store seeded with %d readings", len(mem_readings))


# ── PostgreSQL helpers ────────────────────────────────────────
_pool = None

def get_pool():
    global _pool
    import psycopg2
    import psycopg2.pool
    if _pool is None or _pool.closed:
        log.info("Creating DB connection pool → %s@%s:%d/%s",
                 DB_CONFIG["user"], DB_CONFIG["host"],
                 DB_CONFIG["port"], DB_CONFIG["dbname"])
        _pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=1, maxconn=5,
            connect_timeout=10,
            options="-c timezone=UTC",
            **DB_CONFIG,
        )
    return _pool


def db_query(sql: str, params=None, fetch_one=False):
    """Execute a SELECT query and return results as list of dicts."""
    import psycopg2.extras
    pool = get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            if fetch_one:
                row = cur.fetchone()
                return dict(row) if row else None
            return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        log.error("DB query failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Database error: {e}")
    finally:
        pool.putconn(conn)


def db_execute(sql: str, params=None):
    """Execute an INSERT/UPDATE/DELETE query."""
    pool = get_pool()
    conn = pool.getconn()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                if cur.description:
                    return cur.fetchone()
    except Exception as e:
        log.error("DB execute failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Database error: {e}")
    finally:
        pool.putconn(conn)


def db_seed_demo_data():
    """Insert 1440 rows of demo data if table is empty."""
    pool = get_pool()
    conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM gs3_measurements")
            count = cur.fetchone()[0]
            if count > 0:
                log.info("DB already has %d rows – skipping seed", count)
                pool.putconn(conn)
                return
        log.info("Seeding 1440 demo data points (24h)…")
        with conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO gs3_measurements
                        (ts, node_id, moisture_vwc, temperature_c, conductivity_ec, raw_payload)
                    SELECT
                        NOW() - (n || ' minutes')::INTERVAL,
                        'esp32-node-01',
                        ROUND((0.65 + 0.15*SIN(n*0.05) + RANDOM()*0.02)::NUMERIC, 4),
                        ROUND((26.0 + 4.0*SIN(n*0.03)  + RANDOM()*0.5 )::NUMERIC, 2),
                        ROUND((1.20 + 0.40*COS(n*0.04) + RANDOM()*0.03)::NUMERIC, 4),
                        NULL
                    FROM generate_series(1, 1440) AS n
                """)
        log.info("Seed complete – 1440 rows inserted")
    except Exception as e:
        log.warning("Seed failed (non-critical): %s", e)
    finally:
        pool.putconn(conn)


# ──────────────────────────────────────────────────────────────
#  JSON serializer helper (handle Decimal, datetime)
# ──────────────────────────────────────────────────────────────
def serialize_row(row: dict) -> dict:
    """Convert Decimal and datetime to JSON-safe types."""
    from decimal import Decimal
    result = {}
    for k, v in row.items():
        if isinstance(v, Decimal):
            result[k] = float(v)
        elif isinstance(v, datetime):
            result[k] = v.isoformat()
        else:
            result[k] = v
    return result


# ──────────────────────────────────────────────────────────────
#  WebSocket Connection Manager
# ──────────────────────────────────────────────────────────────
class ConnectionManager:
    """Manages active WebSocket connections for realtime broadcast."""

    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        log.info("WS client connected – total: %d", len(self.active_connections))

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
        log.info("WS client disconnected – total: %d", len(self.active_connections))

    async def broadcast(self, message: dict):
        """Send JSON message to all connected clients."""
        dead = []
        for conn in self.active_connections:
            try:
                await conn.send_json(message)
            except Exception:
                dead.append(conn)
        for conn in dead:
            if conn in self.active_connections:
                self.active_connections.remove(conn)


ws_manager = ConnectionManager()

# ──────────────────────────────────────────────────────────────
#  App Lifecycle
# ──────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global USE_DB
    # Try to connect to PostgreSQL
    try:
        import psycopg2
        pool = get_pool()
        conn = pool.getconn()
        try:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.close()
        finally:
            pool.putconn(conn)
        USE_DB = True
        log.info("✅ PostgreSQL connected – using database storage")
        # Seed demo data (non-critical, won't affect USE_DB)
        try:
            db_seed_demo_data()
        except Exception as e:
            log.warning("Seed failed (non-critical): %s", e)
    except Exception as e:
        USE_DB = False
        log.warning("⚠️  PostgreSQL not available: %s", e)
        log.info("📦 Running with in-memory storage (no database required)")
        mem_seed_data()

    yield

    # Shutdown
    if USE_DB:
        global _pool
        if _pool and not _pool.closed:
            _pool.closeall()
            log.info("Database pool closed")


# ──────────────────────────────────────────────────────────────
#  FastAPI App
# ──────────────────────────────────────────────────────────────
app = FastAPI(
    title="CIMES API",
    description="Chili Intelligent Monitoring and Environmental Sensing System – Backend API",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS for development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ══════════════════════════════════════════════════════════════
#  API ENDPOINTS
# ══════════════════════════════════════════════════════════════

# ── Health Check ──────────────────────────────────────────────
@app.get("/api/health")
async def health():
    try:
        if USE_DB:
            row = db_query("SELECT COUNT(*) AS cnt FROM gs3_measurements", fetch_one=True)
            total = row["cnt"] if row else 0
        else:
            total = len(mem_readings)
        return {
            "status": "ok",
            "database": "connected" if USE_DB else "in-memory",
            "total_readings": total,
            "ws_clients": len(ws_manager.active_connections),
        }
    except Exception:
        return JSONResponse(
            status_code=503,
            content={"status": "error", "database": "disconnected"},
        )


# ── Latest Reading ────────────────────────────────────────────
@app.get("/api/latest")
async def get_latest():
    """Return the most recent sensor reading."""
    if USE_DB:
        row = db_query(
            """
            SELECT id, ts, node_id, moisture_vwc AS vwc,
                   temperature_c AS temp, conductivity_ec AS ec
            FROM gs3_measurements
            ORDER BY ts DESC
            LIMIT 1
            """,
            fetch_one=True,
        )
        if not row:
            raise HTTPException(status_code=404, detail="No readings found")
        return serialize_row(row)
    else:
        if not mem_readings:
            raise HTTPException(status_code=404, detail="No readings found")
        return dict(mem_readings[-1])


# ── History (for Charts) ─────────────────────────────────────
@app.get("/api/history")
async def get_history(hours: int = Query(default=24, ge=1, le=168)):
    """Return sensor readings for the last N hours, downsampled to ~100 points."""
    if USE_DB:
        rows = db_query(
            """
            WITH numbered AS (
                SELECT
                    ts,
                    moisture_vwc AS vwc,
                    temperature_c AS temp,
                    conductivity_ec AS ec,
                    ROW_NUMBER() OVER (ORDER BY ts) AS rn,
                    COUNT(*) OVER () AS total
                FROM gs3_measurements
                WHERE ts >= NOW() - (%s || ' hours')::INTERVAL
            )
            SELECT ts, vwc, temp, ec
            FROM numbered
            WHERE rn %% GREATEST(total / 100, 1) = 0
               OR rn = total
            ORDER BY ts ASC
            """,
            (str(hours),),
        )
        return [serialize_row(r) for r in rows]
    else:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        filtered = [r for r in mem_readings if r["ts"] >= cutoff.isoformat()]
        # Downsample to ~100 points
        total = len(filtered)
        step = max(total // 100, 1)
        result = [filtered[i] for i in range(0, total, step)]
        if filtered and result[-1] != filtered[-1]:
            result.append(filtered[-1])
        return result


# ── Alerts ────────────────────────────────────────────────────
@app.get("/api/alerts")
async def get_alerts(limit: int = Query(default=50, ge=1, le=200)):
    """Return recent alerts."""
    if USE_DB:
        rows = db_query(
            """
            SELECT id, ts, node_id, alert_type, alert_level,
                   current_value, threshold_value, message, acknowledged
            FROM gs3_alerts
            ORDER BY ts DESC
            LIMIT %s
            """,
            (limit,),
        )
        return [serialize_row(r) for r in rows]
    else:
        alerts = list(mem_alerts)
        alerts.reverse()  # newest first
        return alerts[:limit]


# ── Acknowledge Alert ─────────────────────────────────────────
@app.post("/api/alerts/{alert_id}/ack")
async def ack_alert(alert_id: int):
    """Mark an alert as acknowledged."""
    if USE_DB:
        db_execute(
            "UPDATE gs3_alerts SET acknowledged = TRUE, ack_at = NOW() WHERE id = %s",
            (alert_id,),
        )
    else:
        for a in mem_alerts:
            if a.get("id") == alert_id:
                a["acknowledged"] = True
                break
    return {"status": "ok", "alert_id": alert_id, "acknowledged": True}


# ── Statistics ────────────────────────────────────────────────
@app.get("/api/stats")
async def get_stats():
    """Return dashboard statistics."""
    if USE_DB:
        row = db_query(
            """
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
            FROM gs3_measurements
            """,
            fetch_one=True,
        )
        if not row:
            raise HTTPException(status_code=404, detail="No data")
        return serialize_row(row)
    else:
        if not mem_readings:
            raise HTTPException(status_code=404, detail="No data")
        readings = list(mem_readings)
        vwcs  = [r["vwc"] for r in readings]
        temps = [r["temp"] for r in readings]
        ecs   = [r["ec"] for r in readings]
        return {
            "total_readings": len(readings),
            "first_reading": readings[0]["ts"],
            "last_reading": readings[-1]["ts"],
            "avg_vwc": round(sum(vwcs)/len(vwcs), 4),
            "min_vwc": round(min(vwcs), 4),
            "max_vwc": round(max(vwcs), 4),
            "avg_temp": round(sum(temps)/len(temps), 2),
            "min_temp": round(min(temps), 2),
            "max_temp": round(max(temps), 2),
            "avg_ec": round(sum(ecs)/len(ecs), 4),
            "min_ec": round(min(ecs), 4),
            "max_ec": round(max(ecs), 4),
            "unacked_alerts": sum(1 for a in mem_alerts if not a.get("acknowledged", False)),
        }


# ── Ingest (Bridge / Simulator → Backend → DB + WebSocket) ───
class SensorReading(BaseModel):
    vwc: float
    temp: float
    ec: float
    node_id: str = "esp32-node-01"
    raw_payload: Optional[dict] = None


@app.post("/api/ingest")
async def ingest_reading(reading: SensorReading):
    """Receive a sensor reading, store it, and broadcast via WebSocket."""
    global mem_id_counter
    now = datetime.now(timezone.utc)

    if USE_DB:
        try:
            payload_data = reading.raw_payload if reading.raw_payload else {"vwc": reading.vwc, "temp": reading.temp, "ec": reading.ec}
            db_execute(
                """
                INSERT INTO gs3_measurements
                    (ts, node_id, moisture_vwc, temperature_c, conductivity_ec, raw_payload)
                VALUES (NOW(), %s, %s, %s, %s, %s)
                """,
                (
                    reading.node_id,
                    round(reading.vwc, 4),
                    round(reading.temp, 2),
                    round(reading.ec, 4),
                    json.dumps(payload_data),
                ),
            )
        except Exception as e:
            log.error("Ingest DB insert failed: %s", e)
            raise HTTPException(status_code=500, detail=f"DB insert failed: {e}")
    else:
        mem_id_counter += 1
        mem_readings.append({
            "id": mem_id_counter,
            "ts": now.isoformat(),
            "node_id": reading.node_id,
            "vwc": round(reading.vwc, 4),
            "temp": round(reading.temp, 2),
            "ec": round(reading.ec, 4),
        })

    # Broadcast via WebSocket
    payload = {
        "type": "sensor_reading",
        "ts": now.isoformat(),
        "vwc": round(reading.vwc, 4),
        "temp": round(reading.temp, 2),
        "ec": round(reading.ec, 4),
        "node_id": reading.node_id,
    }
    await ws_manager.broadcast(payload)
    log.info("📡 Ingest: VWC=%.4f Temp=%.2f EC=%.4f → broadcast to %d clients",
             reading.vwc, reading.temp, reading.ec,
             len(ws_manager.active_connections))

    # Append to local sensor log file cimes_sensor.log
    try:
        log_file_path = Path(__file__).resolve().parent.parent / "cimes_sensor.log"
        # Convert UTC to local system timezone (WIB)
        local_time_str = now.astimezone().strftime("%Y-%m-%d %H:%M:%S")
        with open(log_file_path, "a", encoding="utf-8") as f:
            f.write(f"[{local_time_str}] | Node: {reading.node_id} | VWC: {reading.vwc:.4f} | Temp: {reading.temp:.2f} | EC: {reading.ec:.4f}\n")
    except Exception as e:
        log.warning("Failed to write to cimes_sensor.log: %s", e)

    return {"status": "ok", "broadcast_to": len(ws_manager.active_connections)}


class AlertPayload(BaseModel):
    alert_type: str
    alert_level: str
    current_value: float
    threshold_value: float
    message: str
    node_id: str = "esp32-node-01"


@app.post("/api/alert")
async def ingest_alert(alert: AlertPayload):
    """Receive an alert from the bridge/simulator, store it in DB/memory, and broadcast via WS."""
    global mem_id_counter
    now = datetime.now(timezone.utc)

    if USE_DB:
        try:
            db_execute(
                """
                INSERT INTO gs3_alerts
                    (node_id, alert_type, alert_level, current_value, threshold_value, message)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    alert.node_id,
                    alert.alert_type,
                    alert.alert_level,
                    round(alert.current_value, 4),
                    round(alert.threshold_value, 4),
                    alert.message,
                ),
            )
        except Exception as e:
            log.error("Alert DB insert failed: %s", e)
    else:
        mem_id_counter += 1
        mem_alerts.append({
            "id": mem_id_counter,
            "ts": now.isoformat(),
            "acknowledged": False,
            "node_id": alert.node_id,
            "alert_type": alert.alert_type,
            "alert_level": alert.alert_level,
            "current_value": alert.current_value,
            "threshold_value": alert.threshold_value,
            "message": alert.message,
        })

    payload = {
        "type": "alert",
        "ts": now.isoformat(),
        "node_id": alert.node_id,
        "alert_type": alert.alert_type,
        "alert_level": alert.alert_level,
        "current_value": alert.current_value,
        "threshold_value": alert.threshold_value,
        "message": alert.message,
    }
    await ws_manager.broadcast(payload)
    return {"status": "ok"}


# ── WebSocket endpoint ────────────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for realtime dashboard updates.

    On connect, sends the latest reading immediately.
    Then keeps the connection alive for future broadcasts.
    """
    await ws_manager.connect(websocket)
    try:
        # Send latest reading immediately on connect
        try:
            if USE_DB:
                row = db_query(
                    """SELECT ts, moisture_vwc AS vwc, temperature_c AS temp,
                              conductivity_ec AS ec, node_id
                       FROM gs3_measurements ORDER BY ts DESC LIMIT 1""",
                    fetch_one=True,
                )
                if row:
                    await websocket.send_json({
                        "type": "sensor_reading",
                        **serialize_row(row),
                    })
            else:
                if mem_readings:
                    await websocket.send_json({
                        "type": "sensor_reading",
                        **dict(mem_readings[-1]),
                    })
        except Exception as e:
            log.warning("WS initial data failed: %s", e)

        # Keep connection alive – listen for pings/messages from client
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
    except Exception:
        ws_manager.disconnect(websocket)


# ── Hourly Aggregation ───────────────────────────────────────
@app.get("/api/hourly")
async def get_hourly(hours: int = Query(default=24, ge=1, le=168)):
    """Return hourly aggregated data."""
    if USE_DB:
        rows = db_query(
            """
            SELECT
                date_trunc('hour', ts) AS hour,
                COUNT(*) AS reading_count,
                AVG(moisture_vwc)::NUMERIC(7,4) AS avg_vwc,
                AVG(temperature_c)::NUMERIC(6,2) AS avg_temp,
                AVG(conductivity_ec)::NUMERIC(8,4) AS avg_ec
            FROM gs3_measurements
            WHERE ts >= NOW() - (%s || ' hours')::INTERVAL
            GROUP BY 1
            ORDER BY 1 ASC
            """,
            (str(hours),),
        )
        return [serialize_row(r) for r in rows]
    else:
        # Simple aggregation from in-memory
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        filtered = [r for r in mem_readings if r["ts"] >= cutoff.isoformat()]
        if not filtered:
            return []
        # Group by hour
        hourly = {}
        for r in filtered:
            hour = r["ts"][:13] + ":00:00"
            if hour not in hourly:
                hourly[hour] = {"vwcs": [], "temps": [], "ecs": [], "count": 0}
            hourly[hour]["vwcs"].append(r["vwc"])
            hourly[hour]["temps"].append(r["temp"])
            hourly[hour]["ecs"].append(r["ec"])
            hourly[hour]["count"] += 1
        return [
            {
                "hour": h,
                "reading_count": d["count"],
                "avg_vwc": round(sum(d["vwcs"])/len(d["vwcs"]), 4),
                "avg_temp": round(sum(d["temps"])/len(d["temps"]), 2),
                "avg_ec": round(sum(d["ecs"])/len(d["ecs"]), 4),
            }
            for h, d in sorted(hourly.items())
        ]


# ══════════════════════════════════════════════════════════════
#  Serve Dashboard Static Files
# ══════════════════════════════════════════════════════════════

@app.get("/")
async def serve_dashboard():
    index = DASHBOARD_DIR / "index.html"
    if index.exists():
        return FileResponse(index)
    return {"message": "Dashboard not found. Place index.html in ../dashboard/"}


# Mount static files (CSS, JS) — must come after API routes
if DASHBOARD_DIR.exists():
    app.mount("/", StaticFiles(directory=str(DASHBOARD_DIR), html=True), name="dashboard")


# ──────────────────────────────────────────────────────────────
#  Entry point
# ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=int(os.getenv("API_PORT", "8000")),
        reload=True,
        log_level="info",
    )
