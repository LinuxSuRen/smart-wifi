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

    echo "=== Setting up Python virtual environment ==="
    if [ ! -d venv ]; then
        python3 -m venv venv
    fi
    source venv/bin/activate
    pip install -r requirements.txt

    echo "=== Dependencies installed ==="
}

PIDFILE="/var/run/smart-wifi.pid"
MAX_RESTART_DELAY=30

_cleanup() {
    echo "Shutting down Smart WiFi Manager..."
    if [ -f "$PIDFILE" ]; then
        pid=$(cat "$PIDFILE" 2>/dev/null)
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null
            # Give it up to 10 seconds to shut down gracefully
            for i in $(seq 1 10); do
                kill -0 "$pid" 2>/dev/null || break
                sleep 1
            done
            # Force kill if still running
            kill -9 "$pid" 2>/dev/null || true
        fi
        rm -f "$PIDFILE"
    fi
    echo "Smart WiFi Manager stopped."
}

_start() {
    export WIFI_INTERFACE="${WIFI_INTERFACE:-wlp2s0}"
    export WEB_HOST="0.0.0.0"
    export WEB_PORT="${WEB_PORT:-8080}"
    mkdir -p /var/lib/smart-wifi

    if [ -f venv/bin/activate ]; then
        . venv/bin/activate
    fi

    echo "Starting Smart WiFi Manager process..."
    python3 main.py &
    echo $! > "$PIDFILE"
    wait $!
    return $?
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
            exec sudo WIFI_INTERFACE="$WIFI_INTERFACE" WEB_HOST="$WEB_HOST" WEB_PORT="$WEB_PORT" "$SCRIPT_DIR/run.sh" local
        fi

        trap _cleanup EXIT SIGTERM SIGINT

        delay=1
        while true; do
            _start
            exit_code=$?
            echo "Process exited with code $exit_code" >&2

            # Exit codes: 0 = normal shutdown, 130/143 = SIGINT/SIGTERM
            if [ "$exit_code" -eq 0 ] || [ "$exit_code" -eq 130 ] || [ "$exit_code" -eq 143 ]; then
                echo "Normal shutdown requested, exiting." >&2
                break
            fi

            echo "Restarting in ${delay}s..." >&2
            sleep "$delay"
            # Exponential backoff up to MAX_RESTART_DELAY
            delay=$((delay * 2))
            [ "$delay" -gt "$MAX_RESTART_DELAY" ] && delay=$MAX_RESTART_DELAY
        done
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
