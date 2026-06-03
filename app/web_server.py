import json as _json

from flask import Flask, jsonify, render_template, request

from app.config import (AP_CHANNEL, AP_IP, AP_NETMASK, AP_PASSWORD, AP_SSID,
                        CAPTIVE_PORTAL_URL, WIFI_INTERFACE, WEB_HOST, WEB_PORT,
                        AP_STATE_FILE, AP_DHCP_START, AP_DHCP_END)
from app.wifi_manager import (WiFiNetwork, WiFiStatus, RadioInfo,
                               connect_to_wifi, disconnect_wifi,
                               get_ap_clients, get_blacklist,
                               kick_client, blacklist_client, unblacklist_client,
                               get_interface_capabilities, get_wifi_status,
                               get_network_interfaces,
                               restore_ap_state,
                               scan_networks, set_captive_portal_url, set_dhcp_settings,
                               start_ap, stop_ap,
                               cleanup_wireless, detect_wireless_radios,
                               check_dependencies, install_dependencies)
from app.config import save_radio_config


def create_app() -> Flask:
    app = Flask(__name__)

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/api/status")
    def api_status():
        status = get_wifi_status()
        caps = get_interface_capabilities()
        return jsonify({
            "status": _status_to_dict(status),
            "capabilities": _caps_to_dict(caps) if caps else None,
        })

    @app.route("/api/scan")
    def api_scan():
        iface = request.args.get("iface")
        networks, error = scan_networks(iface if iface else None)
        return jsonify({"networks": [_net_to_dict(n) for n in networks], "error": error})

    @app.route("/api/connect", methods=["POST"])
    def api_connect():
        data = request.get_json(silent=True) or {}
        ssid = data.get("ssid", "")
        password = data.get("password", "")
        frequency = data.get("frequency", "")
        if not ssid:
            return jsonify({"ok": False, "message": "SSID is required"}), 400
        ok, msg = connect_to_wifi(ssid, password, frequency=frequency)
        return jsonify({"ok": ok, "message": msg})

    @app.route("/api/disconnect", methods=["POST"])
    def api_disconnect():
        ok, msg = disconnect_wifi()
        return jsonify({"ok": ok, "message": msg})

    @app.route("/api/ap/start", methods=["POST"])
    def api_start_ap():
        data = request.get_json(silent=True) or {}
        ssid = data.get("ssid", "")
        password = data.get("password", "")
        channel = data.get("channel", 0)
        dual = data.get("dual", True)
        ok, msg = start_ap(ssid=ssid, password=password, channel=channel, dual=dual)
        return jsonify({"ok": ok, "message": msg})

    @app.route("/api/ap/stop", methods=["POST"])
    def api_stop_ap():
        ok, msg = stop_ap()
        return jsonify({"ok": ok, "message": msg})

    @app.route("/api/ap/clients")
    def api_ap_clients():
        clients = get_ap_clients()
        return jsonify({"clients": [_client_to_dict(c) for c in clients]})

    @app.route("/captive")
    def captive_redirect():
        target = CAPTIVE_PORTAL_URL
        if not target:
            return "Captive portal URL not configured", 404
        return f'<meta http-equiv="refresh" content="0;url={target}">', 200

    @app.route("/api/capabilities")
    def api_capabilities():
        caps = get_interface_capabilities()
        if caps is None:
            return jsonify({"ok": False, "message": "No wireless interface found"}), 404
        return jsonify({"ok": True, "capabilities": _caps_to_dict(caps)})

    @app.route("/api/config", methods=["GET", "PUT"])
    def api_config():
        if request.method == "GET":
            import json as _json
            captive = CAPTIVE_PORTAL_URL
            dhcp_start = AP_DHCP_START
            dhcp_end = AP_DHCP_END
            try:
                with open(AP_STATE_FILE) as f:
                    st = _json.loads(f.read())
                    captive = st.get("captive_portal_url", captive)
                    dhcp_start = st.get("dhcp_start", dhcp_start)
                    dhcp_end = st.get("dhcp_end", dhcp_end)
            except (OSError, _json.JSONDecodeError):
                pass
            return jsonify({
                "ap_ssid": AP_SSID,
                "ap_password": "****" if AP_PASSWORD else "",
                "ap_channel": AP_CHANNEL,
                "ap_ip": AP_IP,
                "ap_netmask": AP_NETMASK,
                "wifi_interface": WIFI_INTERFACE,
                "captive_portal_url": captive,
                "dhcp_start": dhcp_start,
                "dhcp_end": dhcp_end,
            })
        else:
            data = request.get_json(silent=True) or {}
            if "captive_portal_url" in data:
                set_captive_portal_url(data["captive_portal_url"])
            if "dhcp_start" in data or "dhcp_end" in data:
                set_dhcp_settings(
                    data.get("dhcp_start", AP_DHCP_START),
                    data.get("dhcp_end", AP_DHCP_END),
                )
            return jsonify({
                "ok": True,
                "message": "Config updated. Restart AP to apply changes.",
            })

    @app.route("/api/ap/saved")
    def api_ap_saved():
        import json as _json
        try:
            with open(AP_STATE_FILE) as f:
                st = _json.loads(f.read())
            return jsonify({
                "ssid": st.get("ssid", AP_SSID),
                "password": st.get("password", AP_PASSWORD),
                "channel": st.get("channel", AP_CHANNEL),
                "captive_portal_url": st.get("captive_portal_url", CAPTIVE_PORTAL_URL),
                "dhcp_start": st.get("dhcp_start", AP_DHCP_START),
                "dhcp_end": st.get("dhcp_end", AP_DHCP_END),
            })
        except (OSError, _json.JSONDecodeError):
            return jsonify({
                "ssid": AP_SSID,
                "password": AP_PASSWORD,
                "channel": AP_CHANNEL,
                "captive_portal_url": CAPTIVE_PORTAL_URL,
                "dhcp_start": AP_DHCP_START,
                "dhcp_end": AP_DHCP_END,
            })

    @app.route("/api/ap/clients/kick", methods=["POST"])
    def api_kick_client():
        data = request.get_json(silent=True) or {}
        mac = data.get("mac", "")
        if not mac:
            return jsonify({"ok": False, "message": "MAC address required"}), 400
        kick_client(mac)
        return jsonify({"ok": True, "message": f"Kicked {mac}"})

    @app.route("/api/ap/clients/blacklist", methods=["POST"])
    def api_blacklist_client():
        data = request.get_json(silent=True) or {}
        mac = data.get("mac", "")
        if not mac:
            return jsonify({"ok": False, "message": "MAC address required"}), 400
        ok, msg = blacklist_client(mac)
        return jsonify({"ok": ok, "message": msg})

    @app.route("/api/ap/clients/unblacklist", methods=["POST"])
    def api_unblacklist_client():
        data = request.get_json(silent=True) or {}
        mac = data.get("mac", "")
        if not mac:
            return jsonify({"ok": False, "message": "MAC address required"}), 400
        ok, msg = unblacklist_client(mac)
        return jsonify({"ok": ok, "message": msg})

    @app.route("/api/ap/blacklist")
    def api_get_blacklist():
        return jsonify({"blacklist": get_blacklist()})

    @app.route("/api/reset", methods=["POST"])
    def api_reset():
        ok, msg = cleanup_wireless()
        return jsonify({"ok": ok, "message": msg})

    @app.route("/api/radios/detect")
    def api_radios_detect():
        info = detect_wireless_radios()
        return jsonify({"ok": True, **info, "radios": [_radio_to_dict(r) for r in info["radios"]]})

    @app.route("/api/radios/configure", methods=["POST"])
    def api_radios_configure():
        data = request.get_json(silent=True) or {}
        ap_iface = data.get("ap_iface", "")
        sta_iface = data.get("sta_iface", "")
        if not ap_iface or not sta_iface:
            return jsonify({"ok": False, "message": "Both ap_iface and sta_iface are required"}), 400
        save_radio_config(ap_iface, sta_iface)
        return jsonify({"ok": True, "message": f"Configured AP={ap_iface}, STA={sta_iface}"})

    @app.route("/api/interfaces")
    def api_interfaces():
        ifaces = get_network_interfaces()
        return jsonify({"interfaces": [_iface_to_dict(i) for i in ifaces]})

    @app.route("/api/deps")
    def api_deps():
        return jsonify(check_dependencies())

    @app.route("/api/deps/install", methods=["POST"])
    def api_deps_install():
        ok, msg, manual_cmd = install_dependencies()
        return jsonify({"ok": ok, "message": msg, "manual_cmd": manual_cmd, "deps": check_dependencies()})

    @app.before_request
    def _captive_portal_intercept():
        import json as _json
        captive_url = CAPTIVE_PORTAL_URL
        try:
            with open(AP_STATE_FILE) as f:
                st = _json.loads(f.read())
                captive_url = st.get("captive_portal_url", captive_url)
        except (OSError, _json.JSONDecodeError):
            pass
        if not captive_url:
            return None
        path = request.path
        host = request.host.split(":")[0]
        if path.startswith("/api/") or path.startswith("/captive") or path == "/":
            return None
        if host in ("localhost", "127.0.0.1", AP_IP, "0.0.0.0"):
            return None
        return f'<html><head><meta http-equiv="refresh" content="0;url={captive_url}"></head><body>Redirecting to <a href="{captive_url}">{captive_url}</a></body></html>', 200

    return app


def _status_to_dict(s: WiFiStatus) -> dict:
    return {
        "interface": s.interface,
        "mode": s.mode,
        "connected_ssid": s.connected_ssid,
        "ip_address": s.ip_address,
        "ap_active": s.ap_active,
        "ap_ssid": s.ap_ssid,
        "ap_clients": s.ap_clients,
    }


def _caps_to_dict(c) -> dict:
    return {
        "interface": c.interface,
        "driver": c.driver,
        "chipset": c.chipset,
        "supports_ap": c.supports_ap,
        "supports_station": c.supports_station,
        "supports_dual": c.supports_dual,
        "dual_mode_reason": c.dual_mode_reason,
        "additional_interfaces": c.additional_interfaces,
    }


def _net_to_dict(n: WiFiNetwork) -> dict:
    return {
        "bssid": n.bssid,
        "ssid": n.ssid,
        "channel": n.channel,
        "frequency": n.frequency,
        "signal": n.signal,
        "security": n.security,
    }


def _iface_to_dict(i) -> dict:
    return {
        "name": i.name,
        "mac": i.mac,
        "state": i.state,
        "mtu": i.mtu,
        "ipv4": i.ipv4,
        "ipv6": i.ipv6,
        "rx_bytes": i.rx_bytes,
        "tx_bytes": i.tx_bytes,
        "speed": i.speed,
    }


def _client_to_dict(c) -> dict:
    return {
        "mac": c.mac,
        "ip": c.ip,
        "hostname": c.hostname,
        "signal": c.signal,
        "connected_seconds": c.connected_seconds,
    }


def _radio_to_dict(r: RadioInfo) -> dict:
    return {
        "phy": r.phy,
        "iface": r.iface,
        "driver": r.driver,
        "supports_ap": r.supports_ap,
        "supports_station": r.supports_station,
        "supports_dual": r.supports_dual,
        "supports_dual_channel": r.supports_dual_channel,
    }
