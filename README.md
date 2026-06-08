# CIMES – Chili Intelligent Monitoring and Environmental Sensing System

> **IoT Pervasive Computing** · ESP32 + GS3 Sensor · XMPP · PostgreSQL · FastAPI

---

## Project Structure

```
sdi12_project_iot_perpasiv_coumputing/
├── firmware/
│   └── gs3_sensor.ino       # ESP32 C++ – SDI-12 GPIO 14 → XMPP JSON
├── database/
│   └── schema.sql            # PostgreSQL schema + indexes + views
├── bridge/
│   ├── bridge.py             # Python XMPP→PostgreSQL middleware
│   └── requirements.txt
├── backend/
│   ├── server.py             # FastAPI REST API backend
│   ├── requirements.txt
│   ├── Dockerfile
│   └── .env.example
├── dashboard/
│   ├── index.html            # Glassmorphism IoT dashboard
│   ├── style.css
│   └── app.js                # LIVE API + DEMO fallback
├── docker-compose.yml        # One-command deployment
└── README.md
```

---

## Quick Start (Docker)

```bash
# Start PostgreSQL + Backend API
docker-compose up -d

# Open dashboard
open http://localhost:8000
```

That's it! The database is auto-initialized with demo data.

---

## Manual Setup

### 1 · Database Setup

```bash
# Install PostgreSQL (macOS)
brew install postgresql@16
brew services start postgresql@16

# Create database and load schema
createdb chile_iot
psql chile_iot < database/schema.sql
```

### 2 · Backend API

```bash
cd backend
pip install -r requirements.txt

# (Optional) Copy and edit .env
cp .env.example .env

# Run the API server
python server.py
# → http://localhost:8000 (serves dashboard + API)
```

### 3 · Dashboard

The dashboard is automatically served by the backend at `http://localhost:8000`.

Alternatively, open `dashboard/index.html` directly — it will run in **DEMO mode** with simulated data.

```bash
# Standalone (demo mode)
cd dashboard
python3 -m http.server 8080
# → http://localhost:8080
```

---

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/health` | GET | Health check + DB status |
| `/api/latest` | GET | Latest sensor reading |
| `/api/history?hours=24` | GET | Historical data for charts |
| `/api/alerts?limit=50` | GET | Alert log |
| `/api/stats` | GET | Dashboard statistics |
| `/api/hourly?hours=24` | GET | Hourly aggregated data |
| `/api/alerts/{id}/ack` | POST | Acknowledge an alert |

---

## Dashboard Modes

| Mode | Badge | Description |
|------|-------|-------------|
| 🟢 **LIVE** | Green pulsing | Connected to backend API → real database data |
| 🟡 **DEMO** | Amber pulsing | Backend unavailable → simulated sensor data |

The dashboard automatically detects the backend and switches modes. It checks every 15 seconds for reconnection.

---

## Hardware Setup

| Component | Detail |
|-----------|--------|
| MCU       | ESP32 (any dev board) |
| Sensor    | Decagon GS3 – Soil Moisture, Temperature, EC |
| Data Pin  | **GPIO 14** (SDI-12) |
| Relay     | **GPIO 32** (Pump control) |
| LED Green | **GPIO 16** |
| LED Yellow| **GPIO 17** |
| LED Red   | **GPIO 18** |

**Wiring:**

```
GS3 Red   → 3.3V (or 5V)
GS3 Black → GND
GS3 White → GPIO 14
```

---

## XMPP Server (ejabberd / Prosody)

```bash
# Install Prosody on Ubuntu/Debian
sudo apt install prosody

# Create accounts
prosodyctl adduser esp32node@yourdomain.local
prosodyctl adduser bridge@yourdomain.local
```

---

## Python Bridge (XMPP → PostgreSQL)

```bash
cd bridge
pip install -r requirements.txt

# Configure via env vars
export XMPP_JID="bridge@yourdomain.local"
export XMPP_PASSWORD="bridgepassword"
export XMPP_SERVER="192.168.1.100"
export SENSOR_JID="esp32node@yourdomain.local"

python bridge.py
```

---

## Alert Thresholds

| Parameter | Threshold | Level |
|-----------|-----------|-------|
| VWC       | < 0.15 m³/m³ | CRITICAL – emergency irrigation |
| VWC       | < 0.25 m³/m³ | WARNING – pump activated |
| VWC       | 0.60–0.80 m³/m³ | OPTIMAL range |
| Temperature | < 18°C | CRITICAL – frost risk |
| Temperature | 22–30°C | OPTIMAL range |
| Temperature | > 35°C | CRITICAL – heat stress |
| EC | 0.8–2.0 dS/m | OPTIMAL range |
| EC | > 3.0 dS/m | CRITICAL – salt build-up |

---

## Data Format (XMPP payload)

```json
{"vwc": 0.2730, "temp": 27.85, "ec": 0.4512}
```

---

## Architecture

```
ESP32 + GS3 ──SDI-12──→ ESP32 ──XMPP──→ bridge.py ──SQL──→ PostgreSQL
                                                                │
                                              FastAPI ◄─────────┘
                                                │
                                          Dashboard (HTML/JS)
```

---

*CIMES – Pervasive Computing IoT Project · Smart Farming Cabai*
# skill_test_01
