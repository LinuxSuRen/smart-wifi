import json as _json
import os
import secrets

SECRET_KEY = os.environ.get("SECRET_KEY", secrets.token_hex(32))
PAM_SERVICE = os.environ.get("PAM_SERVICE", "login")

WEB_HOST = os.environ.get("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.environ.get("WEB_PORT", "8080"))
WIFI_INTERFACE = os.environ.get("WIFI_INTERFACE", "wlp2s0")
AP_SSID = os.environ.get("AP_SSID", "SmartWiFi-AP")
AP_PASSWORD = os.environ.get("AP_PASSWORD", "smartwifi123")
AP_CHANNEL = int(os.environ.get("AP_CHANNEL", "6"))
AP_IP = os.environ.get("AP_IP", "192.168.4.1")
AP_NETMASK = os.environ.get("AP_NETMASK", "24")
AP_DHCP_START = os.environ.get("AP_DHCP_START", "192.168.4.2")
AP_DHCP_END = os.environ.get("AP_DHCP_END", "192.168.4.100")
CAPTIVE_PORTAL_URL = os.environ.get("CAPTIVE_PORTAL_URL", "")

DATA_DIR = os.environ.get("DATA_DIR", "/var/lib/smart-wifi")
HOSTAPD_CONFIG_PATH = os.path.join(DATA_DIR, "smart-wifi-hostapd.conf")
HOSTAPD_PID_PATH = os.path.join(DATA_DIR, "smart-wifi-hostapd.pid")
WPASUPPLICANT_CONFIG_PATH = os.path.join(DATA_DIR, "smart-wifi-wpa.conf")
DNSMASQ_CONFIG_PATH = os.path.join(DATA_DIR, "smart-wifi-dnsmasq.conf")
DNSMASQ_PID_PATH = os.path.join(DATA_DIR, "smart-wifi-dnsmasq.pid")
DNSMASQ_LEASE_PATH = os.path.join(DATA_DIR, "smart-wifi-dnsmasq.leases")
AP_STATE_FILE = os.path.join(DATA_DIR, "smart-wifi-ap-state.json")
BLACKLIST_FILE = os.path.join(DATA_DIR, "smart-wifi-blacklist.txt")
RADIO_CONFIG_FILE = os.path.join(DATA_DIR, "smart-wifi-radio-config.json")
LOG_FILE = os.path.join(DATA_DIR, "smart-wifi-server.log")
SERVICE_FILE = "/etc/systemd/system/smart-wifi.service"


def ensure_data_dir():
    os.makedirs(DATA_DIR, mode=0o755, exist_ok=True)


# ---- radio config persistence ----

def load_radio_config() -> dict:
    """Load the radio assignment config. Returns empty dict if not set."""
    try:
        with open(RADIO_CONFIG_FILE) as f:
            return _json.loads(f.read())
    except (OSError, _json.JSONDecodeError):
        return {}


def save_radio_config(ap_iface: str, sta_iface: str):
    """Save radio assignment: which interface is AP, which is STA."""
    try:
        with open(RADIO_CONFIG_FILE, "w") as f:
            _json.dump({"ap_iface": ap_iface, "sta_iface": sta_iface}, f)
    except OSError:
        pass


def get_configured_ap_iface() -> str | None:
    """Return the configured AP interface, or None if not set."""
    cfg = load_radio_config()
    return cfg.get("ap_iface")


def get_configured_sta_iface() -> str | None:
    """Return the configured STA interface, or None if not set."""
    cfg = load_radio_config()
    return cfg.get("sta_iface")
