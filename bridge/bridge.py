#!/usr/bin/env python3
"""
============================================================
 CIMES – Chili Intelligent Monitoring and Environmental
         Sensing System
 Component : XMPP → PostgreSQL Bridge
 Framework : slixmpp  |  DB driver: psycopg2

 Threshold-based status classification:
   VWC    : critical_low < 15% | pump_on < 25% | optimal 60–80%
             pump_off > 80% | critical_high > 85%
   Temp   : critical < 18°C or > 35°C | optimal 22–30°C
   EC     : optimal 0.8–2.0 dS/m | critical_high > 3.0 dS/m

 Install:
   pip install slixmpp psycopg2-binary python-dotenv

 Run:
   python bridge.py
============================================================
"""

import asyncio
import json
import logging
import os
import re
import signal
import sys
from datetime import datetime, timezone
from typing import Optional

import requests
from dotenv import load_dotenv
load_dotenv()

import slixmpp
from slixmpp import ClientXMPP
from slixmpp.exceptions import IqError, IqTimeout

# ──────────────────────────────────────────────────────────────
#  ANSI colour helpers (terminal output only)
# ──────────────────────────────────────────────────────────────
class C:
    RESET    = "\033[0m"
    BOLD     = "\033[1m"
    GREEN    = "\033[92m"
    YELLOW   = "\033[93m"
    RED      = "\033[91m"
    CYAN     = "\033[96m"
    MAGENTA  = "\033[95m"
    WHITE    = "\033[97m"

def coloured(text: str, colour: str) -> str:
    return f"{colour}{text}{C.RESET}"

# ──────────────────────────────────────────────────────────────
#  Logging (coloured StreamHandler + plain FileHandler)
# ──────────────────────────────────────────────────────────────
class ColouredFormatter(logging.Formatter):
    LEVEL_COLOURS = {
        logging.DEBUG:    C.WHITE,
        logging.INFO:     C.CYAN,
        logging.WARNING:  C.YELLOW,
        logging.ERROR:    C.RED,
        logging.CRITICAL: C.MAGENTA,
    }
    def format(self, record):
        colour = self.LEVEL_COLOURS.get(record.levelno, C.RESET)
        record.levelname = coloured(f"{record.levelname:<8}", colour)
        return super().format(record)

_stream_handler = logging.StreamHandler(sys.stdout)
_stream_handler.setFormatter(ColouredFormatter(
    fmt="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S"
))
_file_handler = logging.FileHandler("cimes_bridge.log", encoding="utf-8")
_file_handler.setFormatter(logging.Formatter(
    fmt="%(asctime)s [%(levelname)-8s] %(name)s – %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S"
))

logging.basicConfig(level=logging.INFO, handlers=[_stream_handler, _file_handler])
log = logging.getLogger("cimes-bridge")

# ──────────────────────────────────────────────────────────────
#  Configuration
# ──────────────────────────────────────────────────────────────
CFG = {
    "XMPP_JID":      os.getenv("XMPP_JID",      "bridge@yourdomain.local"),
    "XMPP_PASSWORD": os.getenv("XMPP_PASSWORD",  "bridgepassword"),
    "XMPP_SERVER":   os.getenv("XMPP_SERVER",    "192.168.1.100"),
    "XMPP_PORT":     int(os.getenv("XMPP_PORT",  "5222")),
    "SENSOR_JID":    os.getenv("SENSOR_JID",     "esp32node@yourdomain.local"),
    "NODE_ID":       os.getenv("NODE_ID",        "esp32-node-01"),
    "API_URL":       os.getenv("API_URL",        "http://localhost:8000"),
}

# ──────────────────────────────────────────────────────────────
#  Agricultural Thresholds  (synced with firmware & dashboard)
# ──────────────────────────────────────────────────────────────
THRESHOLDS = {
    "vwc": {
        "critical_low":  0.15,   # < 15 % VWC
        "pump_on":       0.25,   # < 25 % → firmware: VWC_PUMP_ON
        "optimal_low":   0.60,   # 60 %   → firmware: VWC_OPTIMAL_LOW
        "optimal_high":  0.80,   # 80 %   → firmware: VWC_OPTIMAL_HIGH (pump OFF)
        "critical_high": 0.85,   # > 85 %
    },
    "temp": {
        "critical_low":  18.0,   # firmware: TEMP_LOW
        "optimal_low":   22.0,   # firmware: TEMP_OPTIMAL_LOW
        "optimal_high":  30.0,   # firmware: TEMP_OPTIMAL_HIGH
        "critical_high": 35.0,   # firmware: TEMP_HIGH
    },
    "ec": {
        "optimal_low":   0.8,    # firmware: EC_LOW
        "optimal_high":  2.0,
        "critical_high": 3.0,
    },
}

# ──────────────────────────────────────────────────────────────
#  Status Classification
# ──────────────────────────────────────────────────────────────
def classify_vwc(vwc: float) -> dict:
    T = THRESHOLDS["vwc"]
    if vwc < T["critical_low"]:
        return {"status": "CRITICAL", "tag": "CRITICAL_LOW",
                "pump": "ON", "colour": C.RED,
                "message": f"CRITICAL LOW moisture {vwc:.4f} m³/m³ (<{T['critical_low']*100:.0f}%) – EMERGENCY irrigation!"}
    if vwc < T["pump_on"]:
        return {"status": "WARNING", "tag": "DRY_PUMP_ON",
                "pump": "ON", "colour": C.YELLOW,
                "message": f"Low moisture {vwc:.4f} m³/m³ (<{T['pump_on']*100:.0f}%) – Pump ACTIVATED"}
    if vwc < T["optimal_low"]:
        return {"status": "WARNING", "tag": "BELOW_OPTIMAL",
                "pump": "ON", "colour": C.YELLOW,
                "message": f"Moisture {vwc:.4f} m³/m³ below optimal range (60–80%)"}
    if vwc <= T["optimal_high"]:
        return {"status": "OPTIMAL", "tag": "OPTIMAL",
                "pump": "OFF", "colour": C.GREEN,
                "message": f"Moisture OPTIMAL {vwc:.4f} m³/m³ ({vwc*100:.1f}%)"}
    if vwc <= T["critical_high"]:
        return {"status": "WARNING", "tag": "HIGH",
                "pump": "OFF", "colour": C.YELLOW,
                "message": f"High moisture {vwc:.4f} m³/m³ (>{T['optimal_high']*100:.0f}%) – Pump STOPPED"}
    return {"status": "CRITICAL", "tag": "CRITICAL_HIGH",
            "pump": "OFF", "colour": C.RED,
            "message": f"CRITICAL HIGH moisture {vwc:.4f} m³/m³ (>{T['critical_high']*100:.0f}%) – drainage needed!"}


def classify_temp(temp: float) -> dict:
    T = THRESHOLDS["temp"]
    if temp < T["critical_low"]:
        return {"status": "CRITICAL", "tag": "CRITICAL_LOW",  "colour": C.RED,
                "message": f"CRITICAL LOW temperature {temp:.1f}°C (<{T['critical_low']}°C) – frost risk!"}
    if temp < T["optimal_low"]:
        return {"status": "WARNING",  "tag": "COOL",          "colour": C.YELLOW,
                "message": f"Cool temperature {temp:.1f}°C – below optimal (22–30°C)"}
    if temp <= T["optimal_high"]:
        return {"status": "OPTIMAL",  "tag": "OPTIMAL",       "colour": C.GREEN,
                "message": f"Temperature OPTIMAL {temp:.1f}°C"}
    if temp < T["critical_high"]:
        return {"status": "WARNING",  "tag": "WARM",          "colour": C.YELLOW,
                "message": f"Warm temperature {temp:.1f}°C – approaching critical"}
    return     {"status": "CRITICAL", "tag": "CRITICAL_HIGH", "colour": C.RED,
                "message": f"CRITICAL HIGH temperature {temp:.1f}°C (>{T['critical_high']}°C) – heat stress!"}


def classify_ec(ec: float) -> dict:
    T = THRESHOLDS["ec"]
    if ec < T["optimal_low"]:
        return {"status": "WARNING",  "tag": "LOW",           "colour": C.YELLOW,
                "message": f"Low EC {ec:.4f} dS/m (<{T['optimal_low']}) – check fertilisation"}
    if ec <= T["optimal_high"]:
        return {"status": "OPTIMAL",  "tag": "OPTIMAL",       "colour": C.GREEN,
                "message": f"EC OPTIMAL {ec:.4f} dS/m"}
    if ec < T["critical_high"]:
        return {"status": "WARNING",  "tag": "HIGH",          "colour": C.YELLOW,
                "message": f"High EC {ec:.4f} dS/m – approaching critical"}
    return     {"status": "CRITICAL", "tag": "CRITICAL_HIGH", "colour": C.RED,
                "message": f"CRITICAL HIGH EC {ec:.4f} dS/m (>{T['critical_high']}) – salt build-up!"}

# ──────────────────────────────────────────────────────────────
#  PostgreSQL pool removed in favor of HTTP API ingestion
# ──────────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────────
#  Payload parsing
# ──────────────────────────────────────────────────────────────
def parse_payload(body: str) -> Optional[dict]:
    body = body.strip()
    match = re.search(r'\{.*?\}', body, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group())
        return {
            "vwc":  float(data["vwc"]),
            "temp": float(data["temp"]),
            "ec":   float(data["ec"]),
        }
    except (KeyError, ValueError, json.JSONDecodeError):
        return None

# ──────────────────────────────────────────────────────────────
#  CIMES XMPP Bridge Client
# ──────────────────────────────────────────────────────────────
class CIMESBridgeClient(ClientXMPP):

    def __init__(self):
        super().__init__(CFG["XMPP_JID"], CFG["XMPP_PASSWORD"])
        self.register_plugin("xep_0030")
        self.register_plugin("xep_0199")
        self.add_event_handler("session_start",    self.on_session_start)
        self.add_event_handler("message",           self.on_message)
        self.add_event_handler("failed_auth",       self.on_failed_auth)
        self.add_event_handler("connection_failed", self.on_connection_failed)
        self.add_event_handler("disconnected",      self.on_disconnected)
        self._msg_count = 0
        self._err_count = 0

    async def on_session_start(self, event):
        log.info(coloured("CIMES Bridge online – XMPP session started as %s", C.GREEN), self.boundjid)
        try:
            await self.get_roster()
        except (IqError, IqTimeout):
            pass
        self.send_presence(pstatus="CIMES Bridge active – monitoring chili plantation")
        self["xep_0199"].enable_keepalive(interval=60, timeout=30)

    async def on_message(self, msg):
        sender = str(msg["from"].bare)
        body   = msg["body"].strip()
        mtype  = msg["type"]

        log.info("Incoming raw XMPP: sender=%s, type=%s, body=%r", sender, mtype, body)

        if CFG["SENSOR_JID"] and sender != CFG["SENSOR_JID"]:
            log.warning("Filtered message: sender %s does not match SENSOR_JID %s", sender, CFG["SENSOR_JID"])
            return
        if mtype not in ("chat", "normal"):
            log.warning("Filtered message: type %s is not chat or normal", mtype)
            return

        self._msg_count += 1
        payload = parse_payload(body)
        if payload is None:
            self._err_count += 1
            log.error("Invalid payload #%d: %r", self._err_count, body[:120])
            return

        vwc, temp, ec = payload["vwc"], payload["temp"], payload["ec"]

        # ── Classify ────────────────────────────────────────────
        vwc_st  = classify_vwc(vwc)
        temp_st = classify_temp(temp)
        ec_st   = classify_ec(ec)

        # ── Terminal summary ────────────────────────────────────
        def fmt(st, val, unit):
            tag = coloured(f"[{st['status']:<8}]", st['colour'])
            return f"{tag} {val:.4f} {unit}"

        log.info(
            "MSG #%d | VWC %s | Temp %s | EC %s",
            self._msg_count,
            fmt(vwc_st, vwc, "m³/m³"),
            fmt(temp_st, temp, "°C"),
            fmt(ec_st, ec, "dS/m"),
        )

        if vwc_st["pump"] == "ON":
            log.warning(coloured("  → PUMP ON  – %s", C.YELLOW), vwc_st["message"])
        else:
            log.info(coloured("  → PUMP OFF – %s", C.GREEN), vwc_st["message"])

        # ── Persist reading via HTTP API ────────────────────────
        raw_payload = {
            **payload,
            "cimes_status": {
                "vwc":  vwc_st["tag"],
                "temp": temp_st["tag"],
                "ec":   ec_st["tag"],
            }
        }
        ingest_payload = {
            "vwc": vwc,
            "temp": temp,
            "ec": ec,
            "node_id": CFG["NODE_ID"],
            "raw_payload": raw_payload
        }
        try:
            url = f"{CFG['API_URL']}/api/ingest"
            res = await asyncio.to_thread(requests.post, url, json=ingest_payload, timeout=5)
            res.raise_for_status()
            log.info("  API Ingest: success (broadcasted to %d clients)", res.json().get("broadcast_to", 0))
        except Exception as e:
            self._err_count += 1
            log.error("API Ingest FAILED: %s", e)
            return

        # ── Raise alerts for non-optimal states via HTTP API ────
        async def post_alert(alert_type, level, current, threshold, message):
            alert_payload = {
                "alert_type": alert_type,
                "alert_level": level,
                "current_value": float(current),
                "threshold_value": float(threshold),
                "message": message,
                "node_id": CFG["NODE_ID"]
            }
            try:
                alert_url = f"{CFG['API_URL']}/api/alert"
                res_alert = await asyncio.to_thread(requests.post, alert_url, json=alert_payload, timeout=5)
                res_alert.raise_for_status()
                log.info("  API Alert: success (%s: %s)", level, alert_type)
            except Exception as e:
                log.error("API Alert FAILED: %s", e)

        T = THRESHOLDS
        if vwc_st["status"] == "CRITICAL":
            threshold = T["vwc"]["critical_low"] if vwc < T["vwc"]["critical_low"] else T["vwc"]["critical_high"]
            await post_alert(vwc_st["tag"], "CRITICAL", vwc, threshold, vwc_st["message"])
        elif vwc_st["status"] == "WARNING":
            await post_alert(vwc_st["tag"], "WARNING", vwc, T["vwc"]["pump_on"], vwc_st["message"])

        if temp_st["status"] == "CRITICAL":
            threshold = T["temp"]["critical_low"] if temp < T["temp"]["optimal_low"] else T["temp"]["critical_high"]
            await post_alert(temp_st["tag"], "CRITICAL", temp, threshold, temp_st["message"])

        if ec_st["status"] == "CRITICAL":
            await post_alert(ec_st["tag"], "CRITICAL", ec, T["ec"]["critical_high"], ec_st["message"])

    def on_failed_auth(self, event):
        log.critical("XMPP authentication FAILED")
        self.disconnect()

    def on_connection_failed(self, event):
        log.critical("XMPP connection FAILED – server=%s:%d", CFG["XMPP_SERVER"], CFG["XMPP_PORT"])

    def on_disconnected(self, event):
        log.warning("XMPP disconnected – attempting reconnect in 10 s …")

    def print_stats(self):
        log.info("Final stats: messages=%d  errors=%d", self._msg_count, self._err_count)

# ──────────────────────────────────────────────────────────────
#  Entry point
# ──────────────────────────────────────────────────────────────
def main():
    print(coloured("""
╔══════════════════════════════════════════════════════════════╗
║   CIMES – Chili Intelligent Monitoring and Environmental     ║
╚══════════════════════════════════════════════════════════════╝""", C.CYAN))

    log.info("XMPP: %s → %s:%d", CFG["XMPP_JID"], CFG["XMPP_SERVER"], CFG["XMPP_PORT"])
    log.info("API : %s", CFG["API_URL"])
    _v = THRESHOLDS["vwc"]
    log.info("Thresholds VWC(m3/m3): critical_low=%.2f pump_on=%.2f optimal=%.2f-%.2f critical_high=%.2f",
             _v["critical_low"], _v["pump_on"], _v["optimal_low"], _v["optimal_high"], _v["critical_high"])
    log.info("Thresholds Temp(°C)  : critical=<%.0f or >%.0f  optimal=%.0f–%.0f",
             THRESHOLDS["temp"]["critical_low"], THRESHOLDS["temp"]["critical_high"],
             THRESHOLDS["temp"]["optimal_low"],  THRESHOLDS["temp"]["optimal_high"])
    log.info("Thresholds EC(dS/m)  : optimal=%.1f–%.1f  critical_high=%.1f",
             THRESHOLDS["ec"]["optimal_low"], THRESHOLDS["ec"]["optimal_high"],
             THRESHOLDS["ec"]["critical_high"])

    # Pre-flight API check
    try:
        health_url = f"{CFG['API_URL']}/api/health"
        res = requests.get(health_url, timeout=5)
        res.raise_for_status()
        data = res.json()
        log.info(coloured("API pre-flight OK – DB status: %s, existing rows: %d", C.GREEN),
                 data.get("database"), data.get("total_readings", 0))
    except Exception as exc:
        log.critical("API pre-flight FAILED (check that backend server is running at %s): %s", CFG['API_URL'], exc)
        sys.exit(1)

    client = CIMESBridgeClient()
    client.use_encryption = False
    client['feature_mechanisms'].unencrypted_plain = True
    client.connect(
        address=(CFG["XMPP_SERVER"], CFG["XMPP_PORT"]),
    )

    def _shutdown(sig, frame):
        log.info("Shutdown signal received")
        client.print_stats()
        client.disconnect()

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    client.loop.run_forever()


if __name__ == "__main__":
    main()