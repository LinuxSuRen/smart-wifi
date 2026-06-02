import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.web_server import create_app
from app.config import WEB_HOST, WEB_PORT
from app.wifi_manager import cleanup_wireless, restore_ap_state

app = create_app()

if __name__ == "__main__":
    print(f"Starting Smart-WiFi web server on {WEB_HOST}:{WEB_PORT}")
    ok, msg = cleanup_wireless()
    print(f"Cleanup: {msg}")
    result = restore_ap_state()
    if result:
        ok2, msg2 = result
        print(f"Restored AP: {msg2}")
    app.run(host=WEB_HOST, port=WEB_PORT, debug=False)
