# Smart WiFi Manager

Dual-mode WiFi manager with web UI — supports AP (hotspot) and STA (client) modes simultaneously.

## Quick Start

```bash
# 1. Install system dependencies
sudo apt-get install -y hostapd dnsmasq iw wireless-tools wpasupplicant iproute2 isc-dhcp-client python3 python3-pip

# 2. Install Python dependencies
pip3 install -r requirements.txt --break-system-packages

# 3. Run
./run.sh local
```

The web UI will be available at `http://<your-ip>:8080`.

## Python Dependencies

The installer automatically creates a virtual environment (`venv/`) and installs dependencies inside it.

```bash
# Automated install (creates venv, installs everything)
sudo ./run.sh install

# Or manually:
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

**Requirements** (`requirements.txt`):
- `flask>=3.0` — Web framework
- `pamela>=1.1.0` — Linux PAM authentication (login via system user accounts)

## Usage

| Tab | Description |
|---|---|
| **WiFi Client** | Scan and connect to WiFi networks |
| **Hotspot / AP** | Start/stop AP mode with configurable SSID, password, channel |
| **Clients** | View connected clients, kick or block them |
| **Interfaces** | View network interfaces with filtering |
| **Settings** | Configure captive portal URL, DHCP range, enable/disable auto-start on boot |

### Login

The first time you access the web UI, you'll need to log in with your Linux system username and password (PAM authentication).

### Auto Start on Boot

Go to **Settings** tab → **Auto Start** section → click **Enable Auto Start**. This installs a systemd service that launches the manager at boot and restores AP mode automatically.

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `WIFI_INTERFACE` | `wlp2s0` | Primary wireless interface |
| `WEB_HOST` | `0.0.0.0` | Web server bind address |
| `WEB_PORT` | `8080` | Web server port |
| `AP_SSID` | `SmartWiFi-AP` | Default AP SSID |
| `AP_PASSWORD` | `smartwifi123` | Default AP password |
| `AP_IP` | `192.168.4.1` | AP IP address |
| `DATA_DIR` | `/var/lib/smart-wifi` | Persistent data directory |
| `SECRET_KEY` | auto-generated | Flask session signing key |
| `AUTH_PASSWORD` | — | Fallback password (bypasses PAM) |

## Docker

```bash
# Privileged mode (full WiFi control)
./run.sh docker

# Safe mode (limited capabilities)
./run.sh docker-safe
```
