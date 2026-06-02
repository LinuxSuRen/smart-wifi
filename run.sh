#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

MODE="${1:-local}"

show_help() {
    echo "Smart WiFi Manager"
    echo ""
    echo "Usage: ./run.sh [mode]"
    echo ""
    echo "Modes:"
    echo "  local       Run directly on host (default)"
    echo "  docker      Run with docker-compose (privileged)"
    echo "  docker-safe Run with docker-compose (non-privileged)"
    echo "  install     Install system dependencies"
    echo ""
}

install_deps() {
    echo "=== Installing system dependencies ==="
    if command -v apt-get &>/dev/null; then
        sudo apt-get update
        sudo apt-get install -y hostapd dnsmasq iw wireless-tools \
            wpasupplicant iproute2 isc-dhcp-client python3 python3-pip
    elif command -v dnf &>/dev/null; then
        sudo dnf install -y hostapd dnsmasq iw wireless-tools \
            wpa_supplicant iproute dhcp-client python3 python3-pip
    elif command -v pacman &>/dev/null; then
        sudo pacman -S --noconfirm hostapd dnsmasq iw wireless_tools \
            wpa_supplicant iproute2 dhclient python python-pip
    else
        echo "Unknown package manager. Install manually:"
        echo "  hostapd, dnsmasq, iw, wpasupplicant, dhclient, iproute2, python3"
    fi

    echo "=== Installing Python dependencies ==="
    pip3 install -r requirements.txt --break-system-packages 2>/dev/null || \
        pip3 install -r requirements.txt

    echo "=== Dependencies installed ==="
}

case "$MODE" in
    local)
        echo "=== Starting Smart WiFi Manager (local) ==="
        if ! command -v hostapd &>/dev/null; then
            echo "Missing system dependencies. Run: ./run.sh install"
            exit 1
        fi
        if [ "$(id -u)" -ne 0 ]; then
            echo "ERROR: This program requires root privileges for WiFi operations."
            echo "Restarting with sudo..."
            export WIFI_INTERFACE="${WIFI_INTERFACE:-wlp2s0}"
            export WEB_HOST="0.0.0.0"
            export WEB_PORT="${WEB_PORT:-8080}"
            exec sudo WIFI_INTERFACE="$WIFI_INTERFACE" WEB_HOST="$WEB_HOST" WEB_PORT="$WEB_PORT" PYTHONPATH="$HOME/.local/lib/python$(python3 -c 'import sys;print(sys.version_info.major,sys.version_info.minor,sep=".")')/site-packages:${PYTHONPATH:-}" "$SCRIPT_DIR/run.sh" local
        fi
        export WIFI_INTERFACE="${WIFI_INTERFACE:-wlp2s0}"
        export WEB_HOST="0.0.0.0"
        export WEB_PORT="${WEB_PORT:-8080}"
        exec python3 main.py 2>>/tmp/smart-wifi-server.log
        ;;
    docker)
        echo "=== Starting Smart WiFi Manager (docker, privileged) ==="
        docker compose up --build -d
        echo "Web UI: http://$(hostname -I 2>/dev/null | awk '{print $1}' || echo 'localhost'):${WEB_PORT:-8080}"
        ;;
    docker-safe)
        echo "=== Starting Smart WiFi Manager (docker, non-privileged) ==="
        docker compose --profile safe up --build -d
        echo "Web UI: http://$(hostname -I 2>/dev/null | awk '{print $1}' || echo 'localhost'):${WEB_PORT:-8080}"
        ;;
    install)
        install_deps
        ;;
    -h|--help|help)
        show_help
        ;;
    *)
        echo "Unknown mode: $MODE"
        show_help
        exit 1
        ;;
esac
