import json as _json
import logging
import os
import subprocess
import sys

from flask import Flask, jsonify, render_template, request, session


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
_log = logging.getLogger("web_server")

from app.config import (AP_CHANNEL, AP_IP, AP_NETMASK, AP_PASSWORD, AP_SSID,
                        CAPTIVE_PORTAL_URL, WIFI_INTERFACE, WEB_HOST, WEB_PORT,
                        AP_STATE_FILE, AP_DHCP_START, AP_DHCP_END,
                        DATA_DIR, SERVICE_FILE, SECRET_KEY)
from app.wifi_manager import (WiFiNetwork, WiFiStatus, RadioInfo, USBWiFiDevice,
                               KernelWifiModule,
                                connect_to_wifi, disconnect_wifi, forget_wifi_network,
                                get_saved_wifi_password,
                               get_ap_clients, get_blacklist,
                               get_hostapd_status,
                               kick_client, blacklist_client, unblacklist_client,
                               get_interface_capabilities, get_wifi_status,
                               get_network_interfaces,
                               restore_ap_state,
                               scan_networks, set_captive_portal_url, set_dhcp_settings,
                               start_ap, stop_ap,
                               cleanup_wireless, detect_wireless_radios,
                               detect_usb_wifi_devices, detect_wifi_kernel_modules,
                               get_full_wifi_diagnostics,
                               load_kernel_module, unload_kernel_module,
                               check_dependencies, install_dependencies)
from app.config import save_radio_config


import ctypes
_libcrypt = ctypes.CDLL("libcrypt.so.1")
_libcrypt.crypt.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
_libcrypt.crypt.restype = ctypes.c_char_p


def _unix_crypt(password: str, salt: str) -> str:
    result = _libcrypt.crypt(password.encode(), salt.encode())
    return result.decode() if result else ""


def _check_auth(username: str, password: str) -> bool:
    """Authenticate against Linux system user (root can read /etc/shadow directly)."""
    fallback_pwd = os.environ.get("AUTH_PASSWORD", "")
    if fallback_pwd and password == fallback_pwd:
        _log.info("Auth success (env fallback) for user=%s", username)
        return True

    # Read shadow entry directly (requires root)
    try:
        with open("/etc/shadow") as f:
            for line in f:
                parts = line.split(":")
                if parts[0] == username:
                    pw_hash = parts[1]
                    if pw_hash in ("", "*", "!", "!!"):
                        _log.warning("Auth failed for user=%s: account has no password or is locked", username)
                        return False
                    if _unix_crypt(password, pw_hash) == pw_hash:
                        _log.info("Auth success (shadow) for user=%s", username)
                        return True
                    _log.warning("Auth failed (shadow) for user=%s: password mismatch", username)
                    return False
    except OSError as e:
        _log.error("Cannot read /etc/shadow for user=%s: %s", username, e)

    _log.warning("Auth failed for user=%s: not found or system error", username)
    return False


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = SECRET_KEY

    @app.route("/")
    def index():
        if not session.get("authenticated"):
            return render_template("login.html")
        return render_template("index.html")

    @app.route("/api/auth/status")
    def api_auth_status():
        return jsonify({
            "authenticated": session.get("authenticated", False),
            "username": session.get("username", ""),
        })

    @app.route("/api/login", methods=["POST"])
    def api_login():
        data = request.get_json(silent=True) or {}
        username = data.get("username", "")
        password = data.get("password", "")
        if not username or not password:
            return jsonify({"ok": False, "message": "Username and password required"}), 400
        if _check_auth(username, password):
            session["authenticated"] = True
            session["username"] = username
            return jsonify({"ok": True, "message": "Login successful"})
        return jsonify({"ok": False, "message": "Invalid username or password"}), 401

    @app.route("/api/logout", methods=["POST"])
    def api_logout():
        session.clear()
        return jsonify({"ok": True, "message": "Logged out"})

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

    @app.route("/api/forget", methods=["POST"])
    def api_forget():
        ok, msg = forget_wifi_network()
        return jsonify({"ok": ok, "message": msg})

    @app.route("/api/saved-password")
    def api_saved_password():
        ssid, password = get_saved_wifi_password()
        return jsonify({"ok": True, "ssid": ssid, "password": password})

    @app.route("/api/ap/start", methods=["POST"])
    def api_start_ap():
        data = request.get_json(silent=True) or {}
        ssid = data.get("ssid", "")
        password = data.get("password", "")
        channel = data.get("channel", 0)
        dual = data.get("dual", True)
        iface = data.get("iface")
        ok, msg = start_ap(iface=iface, ssid=ssid, password=password, channel=channel, dual=dual)
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
                "dual": st.get("dual", True),
                "captive_portal_url": st.get("captive_portal_url", CAPTIVE_PORTAL_URL),
                "dhcp_start": st.get("dhcp_start", AP_DHCP_START),
                "dhcp_end": st.get("dhcp_end", AP_DHCP_END),
            })
        except (OSError, _json.JSONDecodeError):
            return jsonify({
                "ssid": AP_SSID,
                "password": AP_PASSWORD,
                "channel": AP_CHANNEL,
                "dual": True,
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

    @app.route("/api/ap/hostapd-status")
    def api_hostapd_status():
        return jsonify(get_hostapd_status())

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

    @app.route("/api/diagnostics")
    def api_diagnostics():
        return jsonify({"ok": True, **get_full_wifi_diagnostics()})

    @app.route("/api/diagnostics/usb")
    def api_diagnostics_usb():
        devices = detect_usb_wifi_devices()
        return jsonify({"ok": True, "devices": [_usb_to_dict(d) for d in devices]})

    @app.route("/api/diagnostics/modules")
    def api_diagnostics_modules():
        modules = detect_wifi_kernel_modules()
        return jsonify({"ok": True, "modules": [_mod_to_dict(m) for m in modules]})

    @app.route("/api/diagnostics/modules/load", methods=["POST"])
    def api_diagnostics_load_module():
        data = request.get_json(silent=True) or {}
        module_name = data.get("module", "")
        if not module_name:
            return jsonify({"ok": False, "message": "Module name required"}), 400
        ok, msg = load_kernel_module(module_name)
        return jsonify({"ok": ok, "message": msg})

    @app.route("/api/diagnostics/modules/unload", methods=["POST"])
    def api_diagnostics_unload_module():
        data = request.get_json(silent=True) or {}
        module_name = data.get("module", "")
        if not module_name:
            return jsonify({"ok": False, "message": "Module name required"}), 400
        ok, msg = unload_kernel_module(module_name)
        return jsonify({"ok": ok, "message": msg})

    @app.route("/api/autostart/status")
    def api_autostart_status():
        enabled = os.path.exists(SERVICE_FILE)
        active = False
        if enabled:
            r = subprocess.run(["systemctl", "is-active", "smart-wifi.service"],
                               capture_output=True, text=True, timeout=10)
            active = r.stdout.strip() == "active"
        return jsonify({
            "enabled": enabled,
            "active": active,
            "service_path": SERVICE_FILE,
        })

    @app.route("/api/autostart/toggle", methods=["POST"])
    def api_autostart_toggle():
        data = request.get_json(silent=True) or {}
        enable = data.get("enable", True)
        script_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        python = sys.executable or "/usr/bin/python3"
        main_py = os.path.join(script_dir, "main.py")

        if enable:
            unit = (
                "[Unit]\n"
                "Description=Smart WiFi Manager\n"
                "After=network-online.target\n"
                "Wants=network-online.target\n"
                "\n"
                "[Service]\n"
                f"Type=simple\n"
                f"ExecStart={script_dir}/run.sh local\n"
                f"WorkingDirectory={script_dir}\n"
                "Restart=on-failure\n"
                "RestartSec=5\n"
                "KillMode=process\n"
                f"Environment=DATA_DIR={DATA_DIR}\n"
                "\n"
                "[Install]\n"
                "WantedBy=multi-user.target\n"
            )
            try:
                with open(SERVICE_FILE, "w") as f:
                    f.write(unit)
                subprocess.run(["systemctl", "daemon-reload"], capture_output=True, timeout=30)
                subprocess.run(["systemctl", "enable", "smart-wifi.service"], capture_output=True, timeout=30)
                subprocess.run(["systemctl", "start", "smart-wifi.service"], capture_output=True, timeout=30)
                return jsonify({"ok": True, "message": "Auto-start enabled"})
            except OSError as e:
                return jsonify({"ok": False, "message": f"Failed to write service file: {e}"}), 500
        else:
            try:
                subprocess.run(["systemctl", "stop", "smart-wifi.service"], capture_output=True, timeout=30)
                subprocess.run(["systemctl", "disable", "smart-wifi.service"], capture_output=True, timeout=30)
                if os.path.exists(SERVICE_FILE):
                    os.remove(SERVICE_FILE)
                subprocess.run(["systemctl", "daemon-reload"], capture_output=True, timeout=30)
                return jsonify({"ok": True, "message": "Auto-start disabled"})
            except OSError as e:
                return jsonify({"ok": False, "message": f"Failed to remove service file: {e}"}), 500

    @app.before_request
    def _check_api_auth():
        if request.path.startswith("/api/"):
            if request.path in ("/api/auth/status", "/api/login", "/api/logout"):
                return None
            if not session.get("authenticated"):
                return jsonify({"ok": False, "message": "Authentication required"}), 401
        return None

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
        if path.startswith("/api/") or path.startswith("/captive") or path == "/":
            return None
        from flask import redirect
        return redirect(captive_url, code=302)

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


def _usb_to_dict(d: USBWiFiDevice) -> dict:
    return {
        "bus": d.bus,
        "device": d.device,
        "vendor_id": d.vendor_id,
        "product_id": d.product_id,
        "vendor_name": d.vendor_name,
        "product_name": d.product_name,
        "driver": d.driver,
        "module": d.module,
        "interface": d.interface,
        "driver_state": d.driver_state,
        "speed": d.speed,
    }


def _mod_to_dict(m: KernelWifiModule) -> dict:
    return {
        "name": m.name,
        "loaded": m.loaded,
        "size": m.size,
        "used_by": m.used_by,
        "description": m.description,
        "license": m.license,
        "version": m.version,
    }
