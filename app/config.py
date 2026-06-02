import os

WIFI_INTERFACE = os.environ.get("WIFI_INTERFACE", "wlp2s0")
AP_SSID = os.environ.get("AP_SSID", "SmartWiFi-AP")
AP_PASSWORD = os.environ.get("AP_PASSWORD", "smartwifi123")
AP_CHANNEL = int(os.environ.get("AP_CHANNEL", "6"))
AP_IP = os.environ.get("AP_IP", "192.168.4.1")
AP_NETMASK = os.environ.get("AP_NETMASK", "24")
AP_DHCP_START = os.environ.get("AP_DHCP_START", "192.168.4.2")
AP_DHCP_END = os.environ.get("AP_DHCP_END", "192.168.4.100")

HOSTAPD_CONFIG_PATH = "/tmp/smart-wifi-hostapd.conf"
WPASUPPLICANT_CONFIG_PATH = "/tmp/smart-wifi-wpa.conf"
DNSMASQ_CONFIG_PATH = "/tmp/smart-wifi-dnsmasq.conf"
DNSMASQ_PID_PATH = "/tmp/smart-wifi-dnsmasq.pid"

WEB_HOST = os.environ.get("WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.environ.get("WEB_PORT", "8080"))
CAPTIVE_PORTAL_URL = os.environ.get("CAPTIVE_PORTAL_URL", "")
AP_STATE_FILE = "/tmp/smart-wifi-ap-state.json"
BLACKLIST_FILE = "/tmp/smart-wifi-blacklist.txt"
