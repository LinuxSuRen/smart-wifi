import json
import os
import sys
import signal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.web_server import create_app
from app.config import WEB_HOST, WEB_PORT, AP_STATE_FILE, ensure_data_dir
from app.wifi_manager import cleanup_wireless, restore_ap_state, _ap_is_running, get_wifi_status

should_stop = False


def _handle_signal(signum, frame):
    global should_stop
    should_stop = True
    print(f"Received signal {signum}, shutting down...")


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)

ensure_data_dir()
app = create_app()

if __name__ == "__main__":
    print(f"Starting Smart-WiFi web server on {WEB_HOST}:{WEB_PORT}")

    # If AP is already running with the same SSID, skip full reset to avoid disconnecting clients
    skip_reset = False
    if _ap_is_running():
        try:
            with open(AP_STATE_FILE) as f:
                saved = json.loads(f.read())
            status = get_wifi_status()
            if status and status.ap_ssid == saved.get("ssid", ""):
                skip_reset = True
                print("AP already running with matching config, skipping wireless reset")
        except (OSError, json.JSONDecodeError):
            pass

    if not skip_reset:
        ok, msg = cleanup_wireless()
        print(f"Cleanup: {msg}")
        result = restore_ap_state()
        if result:
            ok2, msg2 = result
            print(f"Restored AP: {msg2}")

    from waitress import serve
    serve(app, host=WEB_HOST, port=WEB_PORT, _quiet=True)
