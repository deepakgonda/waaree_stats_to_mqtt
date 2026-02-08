# Waaree Solar → MQTT Poller

Scrapes solar energy/earnings data from [Waaree Digital](https://digital.waaree.com) and publishes it to an **MQTT broker** — designed for [Home Assistant](https://www.home-assistant.io/) integration.

Uses **Playwright** (headless Chromium) to log in, intercept the earnings API response, and publish the JSON payload to MQTT on a schedule. Includes a built-in **status dashboard** on port 8099.

### Features
- Headless browser scraping (Playwright + Chromium)
- Day/Night adaptive fetch intervals (2 min daytime, 1 hour nighttime)
- MQTT publish every 2 minutes with retained messages
- MQTT connection retry with exponential backoff
- Live status dashboard with fetch/publish stats and cached payload view
- JSON API endpoints (`/api/status`, `/api/payload`)
- Docker-first with `restart: unless-stopped`
- Pre-built ARM64 image via GitHub Actions (for Raspberry Pi)

---

## Quick Start (Docker)

### 1. Clone & configure
```bash
git clone https://github.com/deepakgonda/waaree_stats_to_mqtt.git
cd waaree_stats_to_mqtt
cp .env.example .env
nano .env   # set WAAREE_USER, WAAREE_PASS, MQTT_HOST
```

### 2. Run
```bash
# Option A: Pull pre-built ARM64 image (recommended for Pi)
docker compose pull && docker compose up -d

# Option B: Build locally
docker compose up -d --build
```

### 3. Status Dashboard

Open **http://\<your-ip\>:8099** in a browser:
- Current mode (Day ☀️ / Night 🌙), uptime, timezone
- MQTT connection status, publish count
- Last fetch/publish times, error info
- Cached earnings JSON payload

JSON API:
- `GET /api/status` — machine-readable status
- `GET /api/payload` — last cached earnings JSON

### 4. Manage
```bash
docker compose logs -f        # view logs
docker compose restart         # restart
docker compose stop            # stop (won't auto-restart)
docker compose down            # stop and remove container
```

---

## Configuration

All config is via environment variables (set in `.env` file):

| Variable | Default | Description |
|---|---|---|
| `WAAREE_USER` | *(required)* | Waaree Digital username |
| `WAAREE_PASS` | *(required)* | Waaree Digital password |
| `MQTT_HOST` | `192.168.0.2` | MQTT broker IP |
| `MQTT_PORT` | `1883` | MQTT broker port |
| `MQTT_TOPIC` | `homeassistant/waaree/energy` | MQTT topic |
| `DAY_START_HOUR` | `5` | Day mode start (24h) |
| `NIGHT_START_HOUR` | `19` | Night mode start (24h) |
| `PUBLISH_INTERVAL` | `120` | Publish interval (seconds) |
| `DAY_FETCH_INTERVAL` | `120` | Fetch interval during day |
| `NIGHT_FETCH_INTERVAL` | `3600` | Fetch interval at night |
| `STATUS_PORT` | `8099` | Status dashboard port |
| `LOG_LEVEL` | `INFO` | Logging level |

See [.env.example](.env.example) for the full list.

---

## CasaOS

A CasaOS app manifest is included at [casaos-waree-poller.yaml](casaos-waree-poller.yaml). Import it in CasaOS and set your credentials in the environment variables.

---

## License

MIT



