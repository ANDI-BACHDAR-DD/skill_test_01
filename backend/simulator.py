#!/usr/bin/env python3
"""
============================================================
 CIMES – Sensor Simulator
 Replaces ESP32 + XMPP bridge for local testing.

 Generates realistic sensor data and POSTs to the backend
 /api/ingest endpoint every few seconds. The backend then
 broadcasts via WebSocket to connected dashboards.

 Usage:
   cd backend && python simulator.py

 Options (env vars):
   API_URL      – Backend URL (default: http://localhost:8000)
   INTERVAL     – Seconds between readings (default: 3)
   SCENARIO     – dry | wet | optimal | cycle (default: cycle)
============================================================
"""

import math
import os
import random
import signal
import sys
import time

import requests

# ──────────────────────────────────────────────────────────────
#  ANSI colours
# ──────────────────────────────────────────────────────────────
class C:
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    GREEN   = "\033[92m"
    YELLOW  = "\033[93m"
    RED     = "\033[91m"
    CYAN    = "\033[96m"
    MAGENTA = "\033[95m"
    WHITE   = "\033[97m"
    DIM     = "\033[2m"

# ──────────────────────────────────────────────────────────────
#  Configuration
# ──────────────────────────────────────────────────────────────
API_URL  = os.getenv("API_URL", "http://localhost:8000")
INTERVAL = float(os.getenv("INTERVAL", "3"))
SCENARIO = os.getenv("SCENARIO", "cycle")
NODE_ID  = "esp32-node-01"

# ──────────────────────────────────────────────────────────────
#  Thresholds (synced with firmware, bridge, dashboard)
# ──────────────────────────────────────────────────────────────
THRESH = {
    "vwc": {
        "critical_low": 0.15,
        "pump_on":      0.25,
        "optimal_low":  0.60,
        "optimal_high": 0.80,
        "critical_high":0.85,
    },
    "temp": {
        "critical_low": 18.0,
        "optimal_low":  22.0,
        "optimal_high": 30.0,
        "critical_high":35.0,
    },
    "ec": {
        "optimal_low":  0.8,
        "optimal_high": 2.0,
        "critical_high":3.0,
    },
}

# ──────────────────────────────────────────────────────────────
#  Classify for terminal display
# ──────────────────────────────────────────────────────────────
def classify_vwc(v):
    T = THRESH["vwc"]
    if v < T["critical_low"]:  return ("CRITICAL LOW",  C.RED)
    if v < T["pump_on"]:       return ("DRY–PUMP ON",   C.YELLOW)
    if v < T["optimal_low"]:   return ("LOW",           C.YELLOW)
    if v <= T["optimal_high"]: return ("OPTIMAL",       C.GREEN)
    if v <= T["critical_high"]:return ("HIGH",          C.YELLOW)
    return                            ("CRITICAL HIGH", C.RED)

def classify_temp(v):
    T = THRESH["temp"]
    if v < T["critical_low"]:  return ("CRITICAL LOW",  C.RED)
    if v < T["optimal_low"]:   return ("COOL",          C.YELLOW)
    if v <= T["optimal_high"]: return ("OPTIMAL",       C.GREEN)
    if v < T["critical_high"]: return ("WARM",          C.YELLOW)
    return                            ("CRITICAL HIGH", C.RED)

def classify_ec(v):
    T = THRESH["ec"]
    if v < T["optimal_low"]:   return ("LOW",           C.YELLOW)
    if v <= T["optimal_high"]: return ("OPTIMAL",       C.GREEN)
    if v < T["critical_high"]: return ("HIGH",          C.YELLOW)
    return                            ("CRITICAL HIGH", C.RED)

# ──────────────────────────────────────────────────────────────
#  Data generation
# ──────────────────────────────────────────────────────────────
_tick = 0

def generate_reading():
    """Generate a realistic sensor reading based on scenario."""
    global _tick
    _tick += 1
    t = _tick * 0.08  # phase

    if SCENARIO == "dry":
        # Dry scenario: VWC drops below pump threshold, cycles through pump ON/OFF
        vwc  = 0.18 + 0.10 * math.sin(t * 0.3) + random.uniform(-0.01, 0.01)
        temp = 31.0 + 3.0 * math.sin(t * 0.2) + random.uniform(-0.3, 0.3)
        ec   = 0.50 + 0.15 * math.cos(t * 0.25) + random.uniform(-0.02, 0.02)

    elif SCENARIO == "wet":
        # Wet scenario: VWC high, near critical high
        vwc  = 0.82 + 0.05 * math.sin(t * 0.4) + random.uniform(-0.01, 0.01)
        temp = 25.0 + 2.0 * math.sin(t * 0.3) + random.uniform(-0.2, 0.2)
        ec   = 1.80 + 0.30 * math.cos(t * 0.35) + random.uniform(-0.02, 0.02)

    elif SCENARIO == "optimal":
        # Stable optimal conditions
        vwc  = 0.70 + 0.05 * math.sin(t * 0.5) + random.uniform(-0.01, 0.01)
        temp = 26.0 + 2.0 * math.sin(t * 0.3) + random.uniform(-0.2, 0.2)
        ec   = 1.30 + 0.20 * math.cos(t * 0.4) + random.uniform(-0.02, 0.02)

    else:  # "cycle" — default
        # Full cycle: transitions through dry → pump ON → rising → optimal → high → falling
        vwc  = 0.55 + 0.30 * math.sin(t * 0.15) + random.uniform(-0.015, 0.015)
        temp = 26.0 + 5.0 * math.sin(t * 0.08) + random.uniform(-0.3, 0.3)
        ec   = 1.20 + 0.50 * math.cos(t * 0.10) + random.uniform(-0.03, 0.03)

    # Clamp values to valid ranges
    vwc  = max(0.05, min(0.95, vwc))
    temp = max(10.0, min(45.0, temp))
    ec   = max(0.10, min(4.50, ec))

    return {
        "vwc":     round(vwc, 4),
        "temp":    round(temp, 2),
        "ec":      round(ec, 4),
        "node_id": NODE_ID,
    }

# ──────────────────────────────────────────────────────────────
#  POST to backend
# ──────────────────────────────────────────────────────────────
def send_reading(data):
    """POST reading to /api/ingest."""
    try:
        resp = requests.post(
            f"{API_URL}/api/ingest",
            json=data,
            timeout=5,
        )
        if resp.status_code == 200:
            result = resp.json()
            return result.get("broadcast_to", 0)
        else:
            print(f"  {C.RED}✗ HTTP {resp.status_code}{C.RESET}")
            return -1
    except requests.exceptions.ConnectionError:
        print(f"  {C.RED}✗ Connection refused – is the backend running?{C.RESET}")
        return -1
    except Exception as e:
        print(f"  {C.RED}✗ Error: {e}{C.RESET}")
        return -1

# ──────────────────────────────────────────────────────────────
#  Main loop
# ──────────────────────────────────────────────────────────────
def main():
    print(f"""{C.CYAN}{C.BOLD}
╔══════════════════════════════════════════════════════════════╗
║   CIMES – Sensor Simulator                                   ║
║   Generates realistic GS3 data → POST /api/ingest            ║
║   Dashboard receives instant updates via WebSocket            ║
╚══════════════════════════════════════════════════════════════╝{C.RESET}
""")
    print(f"  {C.DIM}Backend  :{C.RESET} {API_URL}")
    print(f"  {C.DIM}Interval :{C.RESET} {INTERVAL}s")
    print(f"  {C.DIM}Scenario :{C.RESET} {SCENARIO}")
    print(f"  {C.DIM}Node     :{C.RESET} {NODE_ID}")
    print()

    # Check backend connectivity
    print(f"  {C.CYAN}Checking backend connectivity...{C.RESET}")
    try:
        resp = requests.get(f"{API_URL}/api/health", timeout=5)
        if resp.status_code == 200:
            health = resp.json()
            print(f"  {C.GREEN}✓ Backend online – DB: {health.get('database', '?')} – "
                  f"Readings: {health.get('total_readings', 0)} – "
                  f"WS clients: {health.get('ws_clients', 0)}{C.RESET}")
        else:
            print(f"  {C.YELLOW}⚠ Backend returned HTTP {resp.status_code}{C.RESET}")
    except requests.exceptions.ConnectionError:
        print(f"  {C.RED}✗ Backend not reachable at {API_URL}{C.RESET}")
        print(f"  {C.DIM}  Start the backend first: cd backend && python server.py{C.RESET}")
        sys.exit(1)
    except Exception as e:
        print(f"  {C.RED}✗ Health check failed: {e}{C.RESET}")
        sys.exit(1)

    print()
    print(f"  {C.GREEN}▶ Starting sensor simulation...{C.RESET}")
    print(f"  {C.DIM}  Press Ctrl+C to stop{C.RESET}")
    print()
    print(f"  {'#':<5} {'VWC':>8} {'Status':<14} {'Temp':>7} {'Status':<14} {'EC':>8} {'Status':<14} {'→ WS':>5}")
    print(f"  {'─'*5} {'─'*8} {'─'*14} {'─'*7} {'─'*14} {'─'*8} {'─'*14} {'─'*5}")

    count = 0
    running = True

    def shutdown(sig, frame):
        nonlocal running
        running = False
        print(f"\n\n  {C.CYAN}Simulator stopped – {count} readings sent{C.RESET}\n")
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    while running:
        count += 1
        data = generate_reading()

        # Classify for display
        vwc_label, vwc_color = classify_vwc(data["vwc"])
        temp_label, temp_color = classify_temp(data["temp"])
        ec_label, ec_color = classify_ec(data["ec"])

        # Send to backend
        ws_count = send_reading(data)

        if ws_count >= 0:
            print(
                f"  {count:<5} "
                f"{data['vwc']:>8.4f} {vwc_color}{vwc_label:<14}{C.RESET} "
                f"{data['temp']:>7.2f} {temp_color}{temp_label:<14}{C.RESET} "
                f"{data['ec']:>8.4f} {ec_color}{ec_label:<14}{C.RESET} "
                f"{C.CYAN}→ {ws_count}{C.RESET}"
            )

        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
