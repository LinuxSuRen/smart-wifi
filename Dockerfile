FROM ubuntu:24.04

LABEL description="Smart WiFi Manager - dual-mode AP/STA WiFi manager"

ENV DEBIAN_FRONTEND=noninteractive
ENV WIFI_INTERFACE=wlan0
ENV AP_SSID=SmartWiFi-AP
ENV AP_PASSWORD=smartwifi123
ENV AP_CHANNEL=6
ENV AP_IP=192.168.4.1
ENV AP_NETMASK=24
ENV WEB_HOST=0.0.0.0
ENV WEB_PORT=8080

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-flask \
    hostapd dnsmasq iw wireless-tools wpasupplicant \
    iproute2 isc-dhcp-client net-tools procps \
    && rm -rf /var/lib/apt/lists/*

# Ensure wpa_supplicant control interface directory exists
RUN mkdir -p /var/run/wpa_supplicant

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --break-system-packages --no-cache-dir -r requirements.txt 2>/dev/null || true

COPY . .

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:${WEB_PORT:-8080}/api/status')" 2>/dev/null || exit 1

CMD ["python3", "main.py"]
