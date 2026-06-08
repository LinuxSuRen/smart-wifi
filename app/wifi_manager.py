import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict

from app.config import (
    AP_CHANNEL, AP_DHCP_END, AP_DHCP_START, AP_IP, AP_NETMASK,
    AP_PASSWORD, AP_SSID, AP_STATE_FILE, BLACKLIST_FILE, CAPTIVE_PORTAL_URL,
    DNSMASQ_CONFIG_PATH, DNSMASQ_PID_PATH, DNSMASQ_LEASE_PATH,
    HOSTAPD_CONFIG_PATH, HOSTAPD_PID_PATH,
    WIFI_INTERFACE, WPASUPPLICANT_CONFIG_PATH, WEB_PORT,
    get_configured_ap_iface, get_configured_sta_iface, load_radio_config,
    save_radio_config, RADIO_CONFIG_FILE, DATA_DIR, ensure_data_dir,
)


@dataclass
class WiFiNetwork:
    bssid: str = ""
    ssid: str = ""
    channel: str = ""
    frequency: str = ""
    signal: int = 0
    security: str = ""


@dataclass
class WiFiStatus:
    interface: str = ""
    mode: str = "unknown"
    connected_ssid: str = ""
    ip_address: str = ""
    ap_active: bool = False
    ap_ssid: str = ""
    ap_clients: int = 0


@dataclass
class InterfaceCapabilities:
    interface: str = ""
    driver: str = ""
    chipset: str = ""
    supports_ap: bool = False
    supports_station: bool = False
    supports_dual: bool = False
    dual_mode_reason: str = ""
    additional_interfaces: list = field(default_factory=list)


@dataclass
class RadioInfo:
    phy: str = ""
    iface: str = ""
    driver: str = ""
    supports_ap: bool = False
    supports_station: bool = False
    supports_dual: bool = False
    supports_dual_channel: bool = False
    channel: int = 0


def _run(cmd: list[str], timeout: int = 15) -> tuple[int, str, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except FileNotFoundError:
        return -1, "", f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return -2, "", "timeout"


def _systemctl(*args: str) -> None:
    """Run systemctl if available (works in container or host)."""
    if os.path.exists("/usr/bin/systemctl") or os.path.exists("/bin/systemctl"):
        subprocess.run(["systemctl", *args], capture_output=True, timeout=10)


def _nm_unmanage(iface: str) -> None:
    """Tell NetworkManager to stop managing an interface."""
    _run(["nmcli", "dev", "set", iface, "managed", "no"])
    # Also try 'unmanage' via dbus if nmcli not available
    _run(["nmcli", "device", "disconnect", iface])


def detect_wireless_radios() -> dict:
    """Detect all wireless radios (PHYs) and their capabilities.

    Returns: {
      'single_nic': bool,       # True if only one physical NIC
      'dual_nic': bool,         # True if 2+ physical NICs
      'radios': [RadioInfo, ...],
      'configured': bool,       # True if user already chose AP/STA assignment
      'ap_iface': str | None,   # Currently configured AP interface
      'sta_iface': str | None,  # Currently configured STA interface
    }
    """
    code, out, _ = _run(["iw", "dev"])
    if code != 0:
        return {"single_nic": True, "dual_nic": False, "radios": [], "configured": False, "ap_iface": None, "sta_iface": None}

    phys: dict[str, list[str]] = {}
    lines = out.strip().split("\n")
    current_phy = ""
    for line in lines:
        m = re.match(r"phy#(\d+)", line)
        if m:
            current_phy = f"phy{m.group(1)}"
            if current_phy not in phys:
                phys[current_phy] = []
        if current_phy and "\tInterface " in line:
            iface = line.strip().split()[-1]
            if iface not in phys[current_phy]:
                phys[current_phy].append(iface)

    radios = []
    for phy_name in sorted(phys.keys()):
        ifaces = phys[phy_name]
        main_iface = [i for i in ifaces if not i.endswith(("_ap", "_sta"))]
        if not main_iface:
            main_iface = ifaces
        main_iface = main_iface[0]

        driver = ""
        _, phy_info, _ = _run(["iw", main_iface, "info"])
        m = re.search(r"driver:\s+(\S+)", phy_info)
        if m:
            driver = m.group(1)

        _, phy_dump, _ = _run(["iw", phy_name, "info"])

        in_modes = False
        _ap = False
        _sta = False
        for line in phy_dump.split("\n"):
            if "Supported interface modes" in line:
                in_modes = True
                continue
            if in_modes:
                s = line.strip()
                if not s or not s.startswith("*"):
                    break
                m = s.lstrip("*").strip()
                if m in ("AP", "AP/VLAN"):
                    _ap = True
                if m == "managed":
                    _sta = True
        supports_ap = _ap
        supports_station = _sta

        supports_dual = supports_ap and supports_station and len(ifaces) > 1
        if not supports_dual:
            test_code, _, _ = _run(["iw", phy_name, "interface", "add", "__dual_test", "type", "managed"])
            if test_code == 0:
                _run(["iw", "dev", "__dual_test", "del"])
                supports_dual = True
            elif supports_ap and supports_station:
                dual_friendly = ["ath9k", "ath10k", "ath11k", "mt76", "iwlwifi",
                                 "rtl8723", "rtl8821", "rtl8822", "brcmfmac"]
                if any(d in driver.lower() for d in dual_friendly):
                    supports_dual = True

        supports_dual_channel = False
        if "valid interface combinations" in phy_dump:
            section = phy_dump.split("valid interface combinations:")[1]
            section = section[:section.find("\n\n")] if "\n\n" in section else section
            if re.search(r'\bAP\b', section) and re.search(r'\bmanaged\b', section):
                if "#channels <= 2" in section:
                    supports_dual_channel = True

        radios.append(RadioInfo(
            phy=phy_name,
            iface=main_iface,
            driver=driver,
            supports_ap=supports_ap,
            supports_station=supports_station,
            supports_dual=supports_dual,
            supports_dual_channel=supports_dual_channel,
        ))

    single_nic = len(radios) <= 1
    dual_nic = len(radios) >= 2
    cfg = load_radio_config()
    ap_cfg = cfg.get("ap_iface", "")
    sta_cfg = cfg.get("sta_iface", "")
    configured = bool(ap_cfg and sta_cfg and
                      os.path.exists(f"/sys/class/net/{ap_cfg}") and
                      os.path.exists(f"/sys/class/net/{sta_cfg}"))
    if cfg and not configured:
        try:
            os.remove(RADIO_CONFIG_FILE)
        except OSError:
            pass

    return {
        "single_nic": single_nic,
        "dual_nic": dual_nic,
        "radios": radios,
        "configured": configured,
        "ap_iface": ap_cfg,
        "sta_iface": sta_cfg,
    }


def _find_wireless_iface() -> str | None:
    """Find the primary managed wireless interface for station/scanning.
    Prefers configured STA interface, then virtual STA, then any managed interface."""
    sta_cfg = get_configured_sta_iface()
    if sta_cfg and os.path.exists(f"/sys/class/net/{sta_cfg}"):
        return sta_cfg
    code, out, _ = _run(["iw", "dev"])
    if code != 0:
        return None
    all_ifaces = list(re.finditer(r"Interface\s+(\w+)", out))
    # Prefer virtual STA interface for scanning
    for m in all_ifaces:
        name = m.group(1)
        if name.endswith("_sta"):
            return name
    # Then prefer any non-AP managed wireless interface (skip AP-mode interfaces)
    for m in all_ifaces:
        name = m.group(1)
        if name == WIFI_INTERFACE and not name.endswith("_ap"):
            _, info_out, _ = _run(["iw", "dev", name, "info"])
            if "type AP" not in info_out:
                return name
    # Fallback: any wireless interface not in AP mode
    for m in all_ifaces:
        name = m.group(1)
        if not name.endswith("_ap") and name != WIFI_INTERFACE:
            _, info_out, _ = _run(["iw", "dev", name, "info"])
            if "type AP" not in info_out:
                return name
    # Last resort: any wireless interface that doesn't end with _ap
    for m in all_ifaces:
        name = m.group(1)
        if not name.endswith("_ap"):
            return name
    return None


def get_interface_capabilities(iface: str | None = None) -> InterfaceCapabilities | None:
    """Detect if a wireless card supports AP, station, and dual modes."""
    if iface is None:
        iface = _find_wireless_iface()
    if iface is None:
        return None

    _, phy_out, _ = _run(["iw", iface, "info"])
    phy_name = ""
    m = re.search(r"wiphy\s+(\w+)", phy_out)
    if m:
        phy_name = "phy" + m.group(1)

    driver = ""
    m = re.search(r"driver:\s+(\S+)", phy_out)
    if m:
        driver = m.group(1)
    if not driver:
        # Fallback: check sysfs
        try:
            drv_link = os.readlink(f"/sys/class/net/{iface}/device/driver/module")
            driver = os.path.basename(drv_link)
        except OSError:
            pass

    chipset = ""
    m = re.search(r"chipset:\s+(\S+)", phy_out)
    if m:
        chipset = m.group(1)
    if not chipset:
        m = re.search(r"device:\s+(\S+)", phy_out)
        if m:
            chipset = m.group(1)

    modes_out = ""
    if phy_name:
        _, modes_out, _ = _run(["iw", phy_name, "info"])

    supports_ap = False
    supports_station = False
    in_modes_section = False
    for line in modes_out.split("\n"):
        if "Supported interface modes" in line:
            in_modes_section = True
            continue
        if in_modes_section:
            stripped = line.strip()
            if not stripped or not stripped.startswith("*"):
                break
            mode = stripped.lstrip("*").strip()
            if mode in ("AP", "AP/VLAN"):
                supports_ap = True
            if mode == "managed":
                supports_station = True

    # Check for multi-interface support (dual mode)
    supports_dual = False
    dual_mode_reason = ""
    additional_interfaces = []

    # Check if we can create additional virtual interfaces
    if phy_name:
        code, _, err = _run(["iw", phy_name, "interface", "add",
                             "__smartwifi_test", "type", "managed"])
        if code == 0:
            _run(["iw", "dev", "__smartwifi_test", "del"])
            additional_interfaces.append("__smartwifi_test (virtual)")
        else:
            dual_mode_reason = f"Cannot create virtual interface: {err.strip()}"

        # Look for existing additional interfaces on same phy
        _, phy_devs, _ = _run(["iw", "dev"])
        phy_num = phy_name.replace("phy", "")
        phy_ifaces = []
        capture = False
        for line in phy_devs.split("\n"):
            if line.startswith("phy#") and phy_num in line:
                capture = True
                continue
            if capture and line.startswith("\tInterface "):
                name = line.strip().split()[-1]
                if name != iface:
                    phy_ifaces.append(name)
            elif capture and not line.startswith("\t"):
                capture = False
        additional_interfaces.extend(phy_ifaces)

    # Heuristic: some drivers/cards support dual mode via P2P or multiple interfaces
    dual_friendly_drivers = ["ath9k", "ath10k", "ath11k", "mt76", "iwlwifi",
                             "rtl8723", "rtl8821", "rtl8822", "brcmfmac"]
    if not supports_dual and (supports_ap and supports_station):
        if driver and any(d in driver.lower() for d in dual_friendly_drivers):
            supports_dual = True
            dual_mode_reason = "Driver known to support dual mode"
        elif additional_interfaces:
            supports_dual = True
            dual_mode_reason = "Multiple interfaces available on same PHY"

    # Run iw list to check for interface combinations
    if phy_name:
        _, iw_list, _ = _run(["iw", phy_name, "info"])
        # Look for "valid interface combinations" section
        if "valid interface combinations" in iw_list:
            # That section existing means the driver reports combination info
            # Simple check: if there's a combo that includes both AP and managed
            block_match = re.search(
                r"valid interface combinations:.*?(?=\n\S|\Z)", iw_list, re.DOTALL
            )
            if block_match:
                block = block_match.group(0)
                if "AP" in block and ("managed" in block or "station" in block):
                    supports_dual = True
                    dual_mode_reason = "Driver reports AP+station interface combination"
                if not supports_dual:
                    dual_mode_reason = "No AP+station combination reported by driver"

    return InterfaceCapabilities(
        interface=iface,
        driver=driver,
        chipset=chipset,
        supports_ap=supports_ap,
        supports_station=supports_station,
        supports_dual=supports_dual,
        dual_mode_reason=dual_mode_reason,
        additional_interfaces=additional_interfaces,
    )


def _ap_is_running() -> bool:
    """Check if hostapd is running."""
    if os.path.exists(HOSTAPD_PID_PATH):
        return True
    return subprocess.run(["pidof", "hostapd"], capture_output=True).returncode == 0


def _scan_with_ap_pause(base_iface: str) -> tuple[int, str, str]:
    """Pause AP briefly to scan, then resume. Returns (code, out, err)."""
    state = {}
    try:
        with open(AP_STATE_FILE) as f:
            state = json.loads(f.read())
    except (OSError, json.JSONDecodeError):
        pass

    _stop_hostapd()
    time.sleep(0.3)

    # Remove virtual STA to free radio resources
    sta = base_iface + "_sta"
    if os.path.exists(f"/sys/class/net/{sta}"):
        _run(["ip", "link", "set", sta, "down"])
        _run(["iw", "dev", sta, "del"])
        time.sleep(0.3)

    # Switch main interface to managed for scanning
    _run(["ip", "link", "set", base_iface, "down"])
    _run(["iw", "dev", base_iface, "set", "type", "managed"])
    _run(["ip", "link", "set", base_iface, "up"])
    time.sleep(1.5)

    _run(["iw", "dev", base_iface, "scan", "flush"])
    time.sleep(0.5)
    code, out, err = _run(["iw", "dev", base_iface, "scan"], timeout=15)

    # Restart AP with same settings
    if state:
        ssid = state.get("ssid", "")
        password = state.get("password", "")
        channel = state.get("channel", 0)
        if ssid and password:
            start_ap(ssid=ssid, password=password, channel=channel, dual=True)

    return code, out, err


def _scan_via_wpa_cli(iface: str) -> tuple[int, str, str]:
    """Scan using wpa_cli through a running wpa_supplicant.
    Returns wpa_cli scan_results in 'bssid freq signal flags ssid' format."""
    code, out, err = _run(["wpa_cli", "-i", iface, "scan"])
    if code != 0 or "OK" not in out:
        return -1, "", err or out
    time.sleep(3.5)
    return _run(["wpa_cli", "-i", iface, "scan_results"])


def scan_networks(iface: str | None = None) -> tuple[list[WiFiNetwork], str]:
    """Scan for available WiFi networks. Returns (networks, error_message)."""
    if iface is None:
        iface = _find_wireless_iface()
    if iface is None:
        return [], "No wireless interface found"

    ap_iface = get_configured_ap_iface() or WIFI_INTERFACE

    candidates = [iface]
    if iface.endswith("_sta") and os.path.exists(f"/sys/class/net/{ap_iface}"):
        if ap_iface not in candidates:
            candidates.append(ap_iface)

    last_error = ""
    used_wpa_cli = False
    for cand in candidates:
        _run(["iw", "dev", cand, "scan", "flush"])
        time.sleep(0.5)
        code, out, err = _run(["iw", "dev", cand, "scan"], timeout=20)
        if code == 0 and out:
            break
        error = err or out
        if error.strip():
            last_error = error.strip()
    else:
        if _ap_is_running():
            if _is_dual_channel_mode():
                code, out, err = _scan_via_wpa_cli(iface)
                if code == 0 and len(out.strip().split("\n")) > 2:
                    used_wpa_cli = True
                else:
                    code, out, err = _scan_with_ap_pause(ap_iface)
                    if code != 0 or not out:
                        error = err or out
                        return [], f"Scan failed: {error.strip()}" if error.strip() else "Scan failed after AP pause"
            else:
                code, out, err = _scan_with_ap_pause(ap_iface)
                if code != 0 or not out:
                    error = err or out
                    return [], f"Scan failed: {error.strip()}" if error.strip() else "Scan failed after AP pause"
        else:
            if "Operation not permitted" in last_error:
                return [], "Permission denied: run as root to scan WiFi networks"
            return [], f"Scan failed: {last_error}" if last_error else "Scan failed: No networks found"

    networks: dict[str, WiFiNetwork] = {}

    if used_wpa_cli:
        for line in out.strip().split("\n"):
            line = line.strip()
            if not line or line.startswith("Selected") or line.startswith("bssid") or line.startswith("BSSID"):
                continue
            parts = [p for p in line.split("\t") if p]
            if len(parts) < 5:
                parts = line.split()
                if len(parts) < 5:
                    continue
            bssid = parts[0]
            freq = parts[1]
            try:
                signal = int(float(parts[2]))
            except ValueError:
                signal = 0
            flags = parts[3]
            ssid = parts[4] if len(parts) > 4 else ""
            if not ssid:
                continue
            security = "Open"
            if "WPA2" in flags or "RSN" in flags:
                security = "WPA2"
            elif "WPA" in flags:
                security = "WPA"
            ch = ""
            try:
                f = float(freq)
                if 2412 <= f <= 2484:
                    ch = str(int((f - 2412) / 5 + 1))
                elif 5160 <= f <= 5885:
                    ch = str(int((f - 5000) / 5))
            except ValueError:
                pass
            if ssid not in networks:
                networks[ssid] = WiFiNetwork(
                    bssid=bssid, ssid=ssid, channel=ch, frequency=freq,
                    signal=signal, security=security,
                )
    else:
        current_bss = ""
        current_signal = 0
        current_freq = ""
        current_ssid = ""
        current_security = ""

        for line in out.split("\n"):
            line = line.strip()
            if line.startswith("BSS "):
                if current_bss and current_ssid:
                    ch = ""
                    if current_freq:
                        try:
                            f = float(current_freq)
                            if 2412 <= f <= 2484:
                                ch = str(int((f - 2412) / 5 + 1))
                            elif 5160 <= f <= 5885:
                                ch = str(int((f - 5000) / 5))
                            elif 5925 <= f <= 7125:
                                ch = str(int((f - 5950) / 5 + 1))
                        except ValueError:
                            ch = current_freq
                    networks[current_ssid] = WiFiNetwork(
                        bssid=current_bss,
                        ssid=current_ssid,
                        channel=ch,
                        frequency=current_freq,
                        signal=current_signal,
                        security=current_security,
                    )
                current_bss = line.split()[1].split("(")[0]
                current_signal = 0
                current_freq = ""
                current_ssid = ""
                current_security = "Open"
            elif "signal:" in line:
                m = re.search(r"signal:\s*(-?\d+\.?\d*)", line)
                if m:
                    current_signal = int(float(m.group(1)))
            elif "freq:" in line:
                m = re.search(r"freq:\s*(\d+)", line)
                if m:
                    current_freq = m.group(1)
            elif "SSID:" in line and "SSID hex" not in line:
                m = re.search(r"SSID:\s*(.*)", line)
                if m:
                    current_ssid = m.group(1).strip()
                    if not current_ssid:
                        current_ssid = "<hidden>"
                    elif "\\x" in current_ssid:
                        try:
                            current_ssid = current_ssid.encode().decode("unicode_escape")
                        except (UnicodeDecodeError, UnicodeEncodeError):
                            pass
            elif "RSN:" in line:
                current_security = "WPA2"
            elif "WPA:" in line:
                current_security = "WPA"

        if current_bss and current_ssid:
            ch = ""
            if current_freq:
                try:
                    f = float(current_freq)
                    if 2412 <= f <= 2484:
                        ch = str(int((f - 2412) / 5 + 1))
                    elif 5160 <= f <= 5885:
                        ch = str(int((f - 5000) / 5))
                except ValueError:
                    ch = current_freq
            networks[current_ssid] = WiFiNetwork(
                bssid=current_bss,
                ssid=current_ssid,
                channel=ch,
                frequency=current_freq,
                signal=current_signal,
                security=current_security,
            )

    return sorted(networks.values(), key=lambda n: n.signal, reverse=True), ""
def _is_dual_channel_mode() -> bool:
    """Check if dual-channel operation is currently possible
    (AP and STA can operate on different channels simultaneously)."""
    sta_cfg = get_configured_sta_iface()
    ap_cfg = get_configured_ap_iface() or WIFI_INTERFACE
    # Dual-NIC: different physical interfaces always support different channels
    if sta_cfg and sta_cfg != ap_cfg and os.path.exists(f"/sys/class/net/{sta_cfg}"):
        return True
    # Single NIC: check if phy supports dual-channel
    if os.path.exists(f"/sys/class/net/{ap_cfg}"):
        phy = _get_phy(ap_cfg)
        if phy:
            _, phy_dump, _ = _run(["iw", phy, "info"])
            if "valid interface combinations" in phy_dump:
                section = phy_dump.split("valid interface combinations:")[1]
                section = section[:section.find("\n\n")] if "\n\n" in section else section
                if ("AP" in section or "ap" in section) and ("managed" in section.lower() or "station" in section.lower()):
                    if "#channels <= 2" in section:
                        return True
    return False


def get_wifi_status(iface: str | None = None) -> WiFiStatus:
    """Get current WiFi status of the interface."""
    if iface is None:
        iface = _find_wireless_iface()
    if iface is None:
        return WiFiStatus()

    # Check if our AP (hostapd) is running first
    ap_active = False
    _, hostapd_out, _ = _run(["pgrep", "-f", f"hostapd.*{HOSTAPD_CONFIG_PATH}"])
    if hostapd_out.strip():
        ap_active = True

    # For AP mode, use the main interface (not virtual STA)
    if ap_active and iface.endswith("_sta"):
        base = iface.replace("_sta", "")
        if os.path.exists(f"/sys/class/net/{base}"):
            iface = base

    status = WiFiStatus(interface=iface)

    # Get mode from iw info
    _, info_out, _ = _run(["iw", "dev", iface, "info"])
    m = re.search(r"type\s+(\w+)", info_out)
    if m:
        mode = m.group(1)
        if mode == "AP":
            status.mode = "ap"
        elif mode == "managed":
            status.mode = "station"
        else:
            status.mode = mode

    # Check iw link info for connection details
    _, iw_out, _ = _run(["iw", "dev", iface, "link"])
    if "Not connected" not in iw_out:
        m = re.search(r"SSID:\s*(.+)", iw_out)
        if m:
            status.connected_ssid = m.group(1).strip()

    # Get IP address
    _, ip_out, _ = _run(["ip", "-4", "addr", "show", iface])
    m = re.search(r"inet\s+(\S+)", ip_out)
    if m:
        status.ip_address = m.group(1)

    status.ap_active = ap_active
    if ap_active:
        status.ap_ssid = AP_SSID
        # Count AP clients across all AP interfaces
        clients = 0
        for ap_iface in _find_all_wireless():
            if not ap_iface.endswith("_sta"):
                _, c_out, _ = _run(["iw", "dev", ap_iface, "station", "dump"])
                clients += c_out.count("Station ")
        status.ap_clients = clients

    return status


def get_hostapd_status() -> dict:
    """Get hostapd process status."""
    _, pid_out, _ = _run(["pgrep", "-f", f"hostapd.*{HOSTAPD_CONFIG_PATH}"])
    pid = pid_out.strip()
    if not pid:
        return {"running": False, "pid": 0, "config": "", "uptime": ""}
    pid = pid.split("\n")[0]
    _, uptime_out, _ = _run(["ps", "-o", "etime=", "-p", pid])
    uptime = uptime_out.strip()
    config = ""
    try:
        with open(HOSTAPD_CONFIG_PATH) as f:
            config = f.read()
    except OSError:
        pass
    return {"running": True, "pid": int(pid), "config": config, "uptime": uptime}


def _get_phy(iface: str) -> str:
    """Get the phy name for an interface."""
    _, info, _ = _run(["iw", iface, "info"])
    m = re.search(r"wiphy\s+(\w+)", info)
    return f"phy{m.group(1)}" if m else ""


def _derive_sta_mac(iface: str) -> str:
    """Derive a unique MAC for the virtual STA interface."""
    _, ip_out, _ = _run(["ip", "link", "show", iface])
    m = re.search(r"link/\S+\s+(\S+)", ip_out)
    if not m:
        return ""
    mac = m.group(1)
    parts = mac.split(":")
    last = int(parts[-1], 16)
    last = (last + 1) % 256
    parts[0] = f"{int(parts[0], 16) | 0x02:02x}"
    parts[-1] = f"{last:02x}"
    return ":".join(parts)


def start_ap(iface: str | None = None, ssid: str = "", password: str = "",
             channel: int = 0, dual: bool = True) -> tuple[bool, str]:
    """Start AP mode. Uses virtual STA for station/scanning when dual=True."""
    if iface is None:
        iface = get_configured_ap_iface() or WIFI_INTERFACE
    # Verify interface exists
    code, _, _ = _run(["iw", "dev", iface, "info"])
    if code != 0:
        iface = _find_wireless_iface()
    if iface is None:
        return False, "No wireless interface found"

    _ssid = ssid or AP_SSID
    _password = password or AP_PASSWORD
    _channel = channel if channel > 0 else AP_CHANNEL

    # Stop any existing AP services
    _stop_hostapd()
    _stop_dnsmasq()
    time.sleep(0.5)

    sta_cfg = get_configured_sta_iface()
    is_dual_nic = bool(sta_cfg and sta_cfg != iface and os.path.exists(f"/sys/class/net/{sta_cfg}"))
    ap_iface = iface

    # Only stop wpa_supplicant globally if NOT dual-NIC (preserve STA on other adapter)
    if not is_dual_nic:
        _systemctl("stop", "wpa_supplicant")
        _systemctl("stop", "wpa_supplicant.socket")
        time.sleep(0.5)

    caps = get_interface_capabilities(iface)
    if caps is not None and not caps.supports_ap:
        return False, f"Interface {iface} does not support AP mode (driver: {caps.driver}). Use a WiFi adapter that supports AP/Master mode."

    if is_dual_nic:
        use_virtual = False
    else:
        use_virtual = dual and caps is not None and caps.supports_dual
        if dual and not use_virtual:
            return False, "Dual mode requested but not supported by this interface"

    # Tell NetworkManager to stop managing this interface
    _nm_unmanage(iface)
    time.sleep(0.5)

    # Check rfkill — ensure wireless is not blocked
    _, rfkill_out, _ = _run(["rfkill", "list", iface])
    if "Soft blocked: yes" in rfkill_out or "Hard blocked: yes" in rfkill_out:
        return False, f"Interface {iface} is rfkill blocked: {rfkill_out.strip()}"

    if use_virtual:
        phy = _get_phy(iface)
        if not phy:
            return False, "Could not determine phy for interface"

        sta_iface = f"{iface}_sta"

        _run(["ip", "link", "set", sta_iface, "down"])
        _run(["iw", "dev", sta_iface, "del"])
        time.sleep(0.5)

        sta_mac = _derive_sta_mac(iface)
        code, out, err = _run(["iw", phy, "interface", "add", sta_iface, "type", "managed",
                               "addr", sta_mac])
        if code != 0:
            time.sleep(0.5)
            code, out, err = _run(["iw", phy, "interface", "add", sta_iface, "type", "managed",
                                   "addr", sta_mac])

        if code != 0:
            detail = (err or out).strip()
            return False, f"Failed to create virtual STA interface: {detail}"
        _run(["ip", "link", "set", sta_iface, "up"])
        _run(["pkill", "-f", f"wpa_supplicant.*{iface}"])
        _run(["ip", "link", "set", iface, "down"])
        _run(["iw", "dev", iface, "set", "type", "ap"])
    else:
        _run(["pkill", "-f", f"wpa_supplicant.*{iface}"])
        _run(["ip", "link", "set", iface, "down"])
        time.sleep(0.5)
        _run(["iw", "dev", iface, "set", "type", "ap"])

    # Configure IP on the AP interface
    _run(["ip", "addr", "flush", "dev", ap_iface])
    _run(["ip", "addr", "add", f"{AP_IP}/{AP_NETMASK}", "dev", ap_iface])
    _run(["ip", "link", "set", ap_iface, "up"])

    # Write hostapd config
    hostapd_conf = _generate_hostapd_config(ap_iface, _ssid, _password, _channel)
    try:
        os.remove(HOSTAPD_CONFIG_PATH)
    except OSError:
        pass
    with open(HOSTAPD_CONFIG_PATH, "w") as f:
        f.write(hostapd_conf)

    # Start hostapd
    code, out, err = _run(["hostapd", "-B", "-P", HOSTAPD_PID_PATH, HOSTAPD_CONFIG_PATH])
    if code != 0:
        error_detail = (err or out).strip()
        if "Name not unique" in error_detail and use_virtual:
            _run(["ip", "link", "set", ap_iface, "down"])
            _run(["iw", "dev", ap_iface, "del"])
            time.sleep(0.5)
            code, out, err = _run(["hostapd", "-B", "-P", HOSTAPD_PID_PATH, HOSTAPD_CONFIG_PATH])
            if code != 0:
                error_detail = (err or out).strip()
        if code != 0:
            _cleanup_virtual(ap_iface if use_virtual else None)
            return False, f"Failed to start hostapd: {error_detail}"

    # Start dnsmasq for DHCP
    dns_ok, dns_msg = _start_dnsmasq(ap_iface)
    if not dns_ok:
        return True, f"AP started but DHCP failed: {dns_msg}"

    # Enable IP forwarding
    _run(["sysctl", "-w", "net.ipv4.ip_forward=1"])

    # Captive portal: redirect HTTP to Flask server
    _setup_captive_portal(ap_iface)

    # Save AP state for auto-restore on reboot
    _save_ap_state(_ssid, _password, _channel, dual)

    tag = " (dual-mode)" if use_virtual else ""
    if use_virtual and _is_dual_channel_mode():
        tag = " (dual-mode, dual-channel)"
    return True, f"AP '{_ssid}' started on {ap_iface}{tag}"


def _cleanup_virtual(virtual_iface: str | None):
    """Remove virtual interface on failure."""
    if virtual_iface:
        _run(["iw", "dev", virtual_iface, "del"])


def _setup_captive_portal(ap_iface: str):
    """Redirect HTTP traffic to the Flask server for captive portal."""
    _teardown_captive_portal()
    url = _get_captive_portal_url()
    if not url:
        return
    # DNAT: redirect port 80 on the AP interface to Flask port
    _run(["iptables", "-t", "nat", "-A", "PREROUTING",
          "-i", ap_iface, "-p", "tcp", "--dport", "80",
          "-j", "REDIRECT", "--to-port", str(WEB_PORT)])


def _teardown_captive_portal():
    """Remove captive portal iptables rules."""
    for iface in _find_all_wireless():
        _run(["iptables", "-t", "nat", "-D", "PREROUTING",
              "-i", iface, "-p", "tcp", "--dport", "80",
              "-j", "REDIRECT", "--to-port", str(WEB_PORT)])


def _get_captive_portal_url() -> str:
    """Get the runtime captive portal URL from state file or env default."""
    try:
        with open(AP_STATE_FILE) as f:
            state = json.loads(f.read())
            return state.get("captive_portal_url", CAPTIVE_PORTAL_URL)
    except (OSError, json.JSONDecodeError):
        return CAPTIVE_PORTAL_URL


def set_captive_portal_url(url: str):
    """Update the captive portal URL in the state file."""
    try:
        try:
            with open(AP_STATE_FILE) as f:
                state = json.loads(f.read())
        except (OSError, json.JSONDecodeError):
            state = {}
        state["captive_portal_url"] = url
        with open(AP_STATE_FILE, "w") as f:
            f.write(json.dumps(state))
        return True
    except OSError:
        return False


def _get_dhcp_settings() -> dict:
    """Get DHCP settings from state file or defaults."""
    defaults = {"start": AP_DHCP_START, "end": AP_DHCP_END}
    try:
        with open(AP_STATE_FILE) as f:
            state = json.loads(f.read())
        return {
            "start": state.get("dhcp_start", defaults["start"]),
            "end": state.get("dhcp_end", defaults["end"]),
        }
    except (OSError, json.JSONDecodeError):
        return defaults


def set_dhcp_settings(start: str, end: str):
    """Update DHCP settings in state file."""
    try:
        try:
            with open(AP_STATE_FILE) as f:
                state = json.loads(f.read())
        except (OSError, json.JSONDecodeError):
            state = {}
        state["dhcp_start"] = start
        state["dhcp_end"] = end
        with open(AP_STATE_FILE, "w") as f:
            f.write(json.dumps(state))
        return True
    except OSError:
        return False


def _save_ap_state(ssid: str, password: str, channel: int, dual: bool = True):
    """Save AP state for auto-restore on server restart."""
    try:
        try:
            with open(AP_STATE_FILE) as f:
                state = json.loads(f.read())
        except (OSError, json.JSONDecodeError):
            state = {}
        state.update({"ssid": ssid, "password": password, "channel": channel, "dual": dual})
        with open(AP_STATE_FILE, "w") as f:
            f.write(json.dumps(state))
    except OSError:
        pass


def _clear_ap_state():
    """Clear persisted AP settings (keep captive portal URL)."""
    try:
        with open(AP_STATE_FILE) as f:
            state = json.loads(f.read())
        for k in ("ssid", "password", "channel", "dual"):
            state.pop(k, None)
        if state:
            with open(AP_STATE_FILE, "w") as f:
                f.write(json.dumps(state))
        else:
            os.remove(AP_STATE_FILE)
    except OSError:
        pass


def restore_ap_state():
    """Auto-start AP if it was active before server restart. Returns None if no state."""
    try:
        with open(AP_STATE_FILE) as f:
            state = json.loads(f.read())
    except (OSError, json.JSONDecodeError):
        return None
    ssid = state.get("ssid", "")
    password = state.get("password", "")
    channel = state.get("channel", 0)
    dual = state.get("dual", True)
    return start_ap(ssid=ssid, password=password, channel=channel, dual=dual)


def cleanup_wireless() -> tuple[bool, str]:
    """Reset all wireless interfaces: kill services, switch to managed, clear IPs."""
    _stop_hostapd()
    _stop_dnsmasq()
    _teardown_captive_portal()
    time.sleep(0.5)
    messages = []
    for iface in _find_all_wireless():
        _run(["pkill", "-9", "-f", f"wpa_supplicant.*{iface}"])
        _run(["pkill", "-9", "-f", f"hostapd.*{iface}"])
        code, info, _ = _run(["iw", "dev", iface, "info"])
        if code != 0:
            continue
        _run(["ip", "link", "set", iface, "down"])
        _run(["iw", "dev", iface, "set", "type", "managed"])
        _run(["ip", "addr", "flush", "dev", iface])
        _run(["ip", "link", "set", iface, "up"])
        messages.append(f"Reset {iface}")
    # Clean up stale virtual interfaces
    cleaned = set()
    for suffix in ("_ap", "_sta"):
        for iface in _find_all_wireless():
            if iface.endswith(suffix):
                _run(["ip", "link", "set", iface, "down"])
                _run(["iw", "dev", iface, "del"])
                cleaned.add(iface)
                messages.append(f"Removed {iface}")
    # Also check ip link for downed interfaces
    code, link_out, _ = _run(["ip", "link", "show"])
    for line in link_out.split("\n"):
        m = re.match(r"^\d+:\s+(\S+):", line)
        if m:
            name = m.group(1).split("@")[0]
            if (name.endswith("_ap") or name.endswith("_sta")) and name not in cleaned:
                _run(["ip", "link", "set", name, "down"])
                _run(["iw", "dev", name, "del"])
                messages.append(f"Removed stale {name}")
    msg = "; ".join(messages) if messages else "No wireless interfaces found"
    return True, msg


def stop_ap() -> tuple[bool, str]:
    """Stop AP mode and clean up virtual interfaces."""
    _stop_hostapd()
    _stop_dnsmasq()
    _teardown_captive_portal()
    _clear_ap_state()
    # Clean up any virtual AP/STA interfaces
    for iface in _find_all_wireless():
        if iface.endswith("_ap") or iface.endswith("_sta"):
            _run(["ip", "addr", "flush", "dev", iface])
            _run(["ip", "link", "set", iface, "down"])
            _run(["iw", "dev", iface, "del"])
            continue
        # Reset any wireless interface stuck in AP mode back to managed
        _, info, _ = _run(["iw", "dev", iface, "info"])
        if "type AP" in info:
            _run(["ip", "link", "set", iface, "down"])
            _run(["iw", "dev", iface, "set", "type", "managed"])
            _run(["ip", "link", "set", iface, "up"])
    return True, "AP stopped"


def _find_all_wireless() -> list[str]:
    """Find all wireless interfaces."""
    code, out, _ = _run(["iw", "dev"])
    if code != 0:
        return []
    return [m.group(1) for m in re.finditer(r"Interface\s+(\w+)", out)]


def stop_ap() -> tuple[bool, str]:
    """Stop AP mode."""
    _stop_hostapd()
    _stop_dnsmasq()
    return True, "AP stopped"


def connect_to_wifi(ssid: str, password: str = "",
                    iface: str | None = None,
                    frequency: str = "") -> tuple[bool, str]:
    """Connect to a WiFi network as a client."""
    if iface is None:
        iface = _find_wireless_iface()
    if iface is None:
        return False, "No wireless interface found"

    # Kill any running wpa_supplicant instances
    _systemctl("stop", "wpa_supplicant")
    _systemctl("stop", "wpa_supplicant.socket")
    _run(["pkill", "-9", "-f", "wpa_supplicant"])
    time.sleep(1)

    # Reset interface to managed mode (don't touch virtual AP)
    _run(["ip", "link", "set", iface, "down"])
    time.sleep(0.3)
    _run(["iw", "dev", iface, "set", "type", "managed"])
    _run(["ip", "link", "set", iface, "up"])

    # Generate wpa_supplicant config
    wpa_conf = _generate_wpa_config(ssid, password, frequency)
    try:
        os.remove(WPASUPPLICANT_CONFIG_PATH)
    except OSError:
        pass
    with open(WPASUPPLICANT_CONFIG_PATH, "w") as f:
        f.write(wpa_conf)
    os.chmod(WPASUPPLICANT_CONFIG_PATH, 0o600)

    # Ensure ctrl_interface directory exists
    os.makedirs("/var/run/wpa_supplicant", exist_ok=True)

    # Start wpa_supplicant
    wpacode, _, wpaerr = _run([
        "wpa_supplicant", "-B", "-i", iface, "-D", "nl80211",
        "-c", WPASUPPLICANT_CONFIG_PATH
    ])
    if wpacode != 0:
        return False, f"Failed to start wpa_supplicant: {wpaerr}"
    time.sleep(2)

    # Run dhclient to get IP
    code, _, dherr = _run(["dhclient", "-v", iface], timeout=20)
    if code != 0:
        return False, f"Connected but failed to get IP via DHCP: {dherr}"

    return True, f"Connected to '{ssid}'"


def disconnect_wifi(iface: str | None = None) -> tuple[bool, str]:
    """Disconnect from current WiFi network."""
    if iface is None:
        iface = _find_wireless_iface()
    if iface is None:
        return False, "No wireless interface found"

    _run(["pkill", "-f", f"wpa_supplicant.*{iface}"])
    _run(["dhclient", "-r", iface])
    _run(["ip", "addr", "flush", "dev", iface])
    _systemctl("start", "wpa_supplicant")
    _systemctl("start", "wpa_supplicant.socket")
    return True, "Disconnected"


def forget_wifi_network(iface: str | None = None) -> tuple[bool, str]:
    """Forget the currently connected/saved WiFi network.

    Disconnects and clears the wpa_supplicant config so the
    network won't reconnect automatically.
    """
    if iface is None:
        iface = _find_wireless_iface()
    if iface is None:
        return False, "No wireless interface found"

    _run(["pkill", "-f", f"wpa_supplicant.*{iface}"])
    _run(["dhclient", "-r", iface])
    _run(["ip", "addr", "flush", "dev", iface])

    try:
        os.remove(WPASUPPLICANT_CONFIG_PATH)
    except FileNotFoundError:
        pass

    _systemctl("start", "wpa_supplicant")
    _systemctl("start", "wpa_supplicant.socket")
    return True, "Network forgotten"


def get_saved_wifi_password() -> tuple[str, str]:
    """Read saved SSID and password from wpa_supplicant config.

    Returns (ssid, password) tuple. Both will be empty strings
    if no network is configured.
    """
    try:
        with open(WPASUPPLICANT_CONFIG_PATH) as f:
            content = f.read()
    except (OSError, FileNotFoundError):
        return "", ""

    ssid = ""
    password = ""
    for line in content.split("\n"):
        line = line.strip()
        if line.startswith("ssid="):
            val = line.split("=", 1)[1].strip().strip('"')
            ssid = val
        elif line.startswith("psk="):
            val = line.split("=", 1)[1].strip().strip('"')
            password = val
    return ssid, password


def _stop_hostapd():
    """Kill the hostapd process managing our config."""
    _run(["pkill", "-9", "-f", f"hostapd.*{HOSTAPD_CONFIG_PATH}"])
    _run(["pkill", "-9", "hostapd"])


def _stop_dnsmasq():
    """Kill the dnsmasq process."""
    if os.path.exists(DNSMASQ_PID_PATH):
        try:
            with open(DNSMASQ_PID_PATH) as f:
                pid = f.read().strip()
            if pid:
                _run(["kill", pid])
        except (OSError, ValueError):
            pass
        try:
            os.remove(DNSMASQ_PID_PATH)
        except OSError:
            pass
    _run(["pkill", "-9", "-f", f"dnsmasq.*{DNSMASQ_CONFIG_PATH}"])
    _run(["pkill", "-9", "dnsmasq"])


def _start_dnsmasq(iface: str) -> tuple[bool, str]:
    """Start dnsmasq as DHCP server for AP mode. Returns (ok, message)."""
    dhcp = _get_dhcp_settings()
    captive_url = _get_captive_portal_url()
    dnsmasq_conf = (
        f"interface={iface}\n"
        f"dhcp-range={dhcp['start']},{dhcp['end']},255.255.255.0,12h\n"
        f"dhcp-option=3,{AP_IP}\n"
        f"dhcp-option=6,{AP_IP}\n"
        f"dhcp-leasefile={DNSMASQ_LEASE_PATH}\n"
    )
    if captive_url:
        dnsmasq_conf += f"no-resolv\naddress=/#/{AP_IP}\n"
    try:
        os.remove(DNSMASQ_CONFIG_PATH)
    except OSError:
        pass
    with open(DNSMASQ_CONFIG_PATH, "w") as f:
        f.write(dnsmasq_conf)

    try:
        os.remove(DNSMASQ_PID_PATH)
    except OSError:
        pass

    code, _, err = _run([
        "dnsmasq", "-C", DNSMASQ_CONFIG_PATH,
        "-x", DNSMASQ_PID_PATH,
        "--bind-interfaces", "--user=root"
    ])
    if code != 0:
        msg = f"dnsmasq failed: {err.strip()}" if err.strip() else "dnsmasq returned non-zero exit code"
        print(msg, file=sys.stderr)
        return False, msg
    return True, "dnsmasq started"


def _generate_hostapd_config(iface: str, ssid: str, password: str, channel: int) -> str:
    """Generate hostapd configuration file content."""
    deny_mac = ""
    if os.path.exists(BLACKLIST_FILE):
        try:
            with open(BLACKLIST_FILE) as f:
                macs = [l.strip() for l in f if l.strip()]
            if macs:
                deny_mac = f"\ndeny_mac_file={BLACKLIST_FILE}\nmacaddr_acl=1"
        except OSError:
            pass
    lines = [
        f"interface={iface}",
        "driver=nl80211",
        f"ssid={ssid}",
        f"channel={channel}",
        f"hw_mode={'a' if channel > 14 else 'g'}",
        "ieee80211n=1",
        "wmm_enabled=1",
        "macaddr_acl=0",
        "auth_algs=1",
        "ignore_broadcast_ssid=0",
    ]
    if password:
        lines += [
            "wpa=2",
            f"wpa_passphrase={password}",
            "wpa_key_mgmt=WPA-PSK",
            "rsn_pairwise=CCMP",
        ]
    if deny_mac:
        lines[-3] = ""  # clear default macaddr_acl=0
        lines.append(deny_mac.strip())
    return "\n".join(line for line in lines if line)


def _generate_wpa_config(ssid: str, password: str, frequency: str = "") -> str:
    """Generate wpa_supplicant configuration file content."""
    lines = [
        "ctrl_interface=/var/run/wpa_supplicant",
        "update_config=1",
        "country=US",
    ]
    net_lines = [f'\tssid="{ssid}"']
    if password:
        net_lines += [f'\tpsk="{password}"', "\tscan_ssid=1"]
    else:
        net_lines.append("\tkey_mgmt=NONE")
    if frequency:
        net_lines.append(f"\tscan_freq={frequency}")
    lines.append("network={")
    lines.extend(net_lines)
    lines.append("}")
    return "\n".join(lines)


# ---------- Network interfaces info ----------

@dataclass
class NetInterface:
    name: str = ""
    mac: str = ""
    state: str = "down"
    mtu: int = 0
    ipv4: list = field(default_factory=list)
    ipv6: list = field(default_factory=list)
    rx_bytes: int = 0
    tx_bytes: int = 0
    speed: str = ""


def get_network_interfaces() -> list[NetInterface]:
    """Return all system network interfaces with their details."""
    interfaces: dict[str, NetInterface] = {}

    # Parse ip addr output
    code, out, _ = _run(["ip", "-details", "-statistics", "addr", "show"])
    if code != 0:
        return []

    current = ""
    for line in out.split("\n"):
        line = line.strip()
        if not line:
            continue
        # New interface block
        m = re.match(r"^\d+:\s+(\S+):", line)
        if m:
            name = m.group(1)
            if "@" in name:
                name = name.split("@")[0]
            current = name
            iface = NetInterface(name=name)
            if "UP" in line:
                iface.state = "up"
            if "DOWN" in line and "LOWER_UP" not in line:
                iface.state = "down"
            interfaces[current] = iface
            continue
        if not current:
            continue
        iface = interfaces[current]
        m = re.search(r"link/\S+\s+(\S+)", line)
        if m:
            iface.mac = m.group(1)
        m = re.match(r"inet\s+(\S+)", line)
        if m:
            iface.ipv4.append(m.group(1))
        m = re.match(r"inet6\s+(\S+)", line)
        if m:
            addr = m.group(1)
            if not addr.startswith("fe80:"):
                iface.ipv6.append(addr)

    # Read MTU, RX/TX stats, speed from /sys/class/net
    for name, iface in list(interfaces.items()):
        base = f"/sys/class/net/{name}/"
        for field, attr in [("rx_bytes", "rx_bytes"), ("tx_bytes", "tx_bytes")]:
            try:
                with open(base + "statistics/" + field) as f:
                    setattr(iface, attr, int(f.read().strip()))
            except (OSError, ValueError):
                pass
        try:
            with open(base + "mtu") as f:
                iface.mtu = int(f.read().strip())
        except (OSError, ValueError):
            pass
        try:
            with open(base + "speed") as f:
                speed = int(f.read().strip())
                iface.speed = f"{speed} Mbps" if speed > 0 else ""
        except (OSError, ValueError):
            pass

    return sorted(interfaces.values(), key=lambda i: (
        0 if i.name.startswith(("en", "eth")) else
        1 if i.name.startswith("wl") else
        2 if i.name == "lo" else 3
    ))


def _find_all_wireless() -> list[str]:
    """Find all wireless interfaces."""
    code, out, _ = _run(["iw", "dev"])
    if code != 0:
        return []
    return [m.group(1) for m in re.finditer(r"Interface\s+(\w+)", out)]


# ---------- Connected clients ----------

@dataclass
class APClient:
    mac: str = ""
    ip: str = ""
    hostname: str = ""
    signal: int = 0
    connected_seconds: int = 0


def get_ap_clients() -> list[APClient]:
    """Get currently connected AP clients with details."""
    clients: list[APClient] = []
    mac_info: dict[str, dict] = {}

    # Get station info from all wireless interfaces
    for iface in _find_all_wireless():
        _, dump, _ = _run(["iw", "dev", iface, "station", "dump"])
        if not dump.strip():
            continue
        current_mac = ""
        current_info: dict = {}
        for line in dump.split("\n"):
            line = line.strip()
            if line.startswith("Station "):
                if current_mac and current_info:
                    mac_info[current_mac] = current_info
                current_mac = line.split()[1].lower()
                current_info = {}
            elif current_mac:
                m = re.search(r"signal:\s*(-?\d+)", line)
                if m:
                    current_info["signal"] = int(m.group(1))
                m = re.search(r"connected time:\s*(\d+)", line)
                if m:
                    current_info["connected_seconds"] = int(m.group(1))
                m = re.search(r"inactive time:\s*(\d+)", line)
                if m and "station is not associated" in dump:
                    current_info["inactive"] = True
        if current_mac and current_info:
            mac_info[current_mac] = current_info

    # Get IP assignments from dnsmasq lease file
    lease_file = DNSMASQ_LEASE_PATH
    lease_data: dict[str, dict] = {}
    try:
        with open(lease_file) as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) >= 4:
                    mac = parts[1].lower()
                    lease_data[mac] = {"ip": parts[2], "hostname": parts[3] if parts[3] != "*" else ""}
    except OSError:
        pass

    # Fallback: ARP table
    arp_data: dict[str, str] = {}
    _, arp, _ = _run(["ip", "neigh", "show"])
    for line in arp.split("\n"):
        parts = line.strip().split()
        if len(parts) >= 4:
            ip = parts[0]
            mac = parts[4].lower() if len(parts) > 4 else parts[3].lower()
            if mac not in lease_data and mac not in arp_data:
                arp_data[mac] = ip

    for mac, info in mac_info.items():
        if info.get("inactive"):
            continue
        lease = lease_data.get(mac, {})
        arp_ip = arp_data.get(mac, "")
        clients.append(APClient(
            mac=mac,
            ip=lease.get("ip", arp_ip),
            hostname=lease.get("hostname", ""),
            signal=info.get("signal", 0),
            connected_seconds=info.get("connected_seconds", 0),
        ))

    return sorted(clients, key=lambda c: c.signal or -999, reverse=True)


def kick_client(mac: str) -> bool:
    """Disconnect a client from the AP."""
    mac = mac.strip().lower()
    for iface in _find_all_wireless():
        _run(["iw", "dev", iface, "station", "del", mac])
    return True


def is_blacklisted(mac: str) -> bool:
    """Check if a MAC is blacklisted."""
    try:
        with open(BLACKLIST_FILE) as f:
            return any(line.strip().lower() == mac.strip().lower() for line in f)
    except OSError:
        return False


def blacklist_client(mac: str) -> tuple[bool, str]:
    """Add MAC to blacklist and kick the client. Requires AP restart to take effect."""
    mac = mac.strip().lower()
    if is_blacklisted(mac):
        return False, f"{mac} is already blacklisted"
    try:
        with open(BLACKLIST_FILE, "a") as f:
            f.write(mac + "\n")
    except OSError:
        return False, "Failed to write blacklist file"
    kick_client(mac)
    return True, f"{mac} blacklisted. Restart AP to enforce."


def unblacklist_client(mac: str) -> tuple[bool, str]:
    """Remove MAC from blacklist."""
    mac = mac.strip().lower()
    try:
        with open(BLACKLIST_FILE) as f:
            lines = [l for l in f if l.strip().lower() != mac]
        with open(BLACKLIST_FILE, "w") as f:
            f.writelines(lines)
    except OSError:
        return True, "No blacklist file"
    return True, f"{mac} removed from blacklist"


def get_blacklist() -> list[str]:
    """Return all blacklisted MACs."""
    try:
        with open(BLACKLIST_FILE) as f:
            return [l.strip() for l in f if l.strip()]
    except OSError:
        return []


# ---------- USB WiFi Device Detection ----------

@dataclass
class USBWiFiDevice:
    bus: str = ""
    device: str = ""
    vendor_id: str = ""
    product_id: str = ""
    vendor_name: str = ""
    product_name: str = ""
    driver: str = ""
    module: str = ""
    interface: str = ""
    driver_state: str = "unknown"
    speed: str = ""
    sysfs_path: str = ""


@dataclass
class KernelWifiModule:
    name: str = ""
    loaded: bool = False
    size: str = ""
    used_by: list[str] = field(default_factory=list)
    description: str = ""
    license: str = ""
    version: str = ""


_WIFI_USB_VENDOR_DB = {
    # (vendor_id, product_id): (driver, description)
    # Realtek
    "0bda": {"desc": "Realtek Semiconductor", "drivers": {
        "8176": "rtl8192cu", "8178": "rtl8192cu", "8179": "rtl8188eu",
        "818b": "rtl8192eu", "8197": "rtl8188eu", "8187": "rtl8187",
        "8172": "rtl8192cu", "8171": "rtl8192cu", "8192": "rtl8192cu",
        "b720": "rtl8723bu", "b812": "rtl88x2bu", "c811": "rtl8811cu",
        "c820": "rtl8821cu", "c821": "rtl8822cu", "c82e": "rtl8821ce",
        "8812": "rtl8812au", "881a": "rtl8821ae", "b822": "rtl8822bu",
        "c812": "rtl8812cu", "c852": "rtl8852cu", "b83c": "rtl8852bu",
    }},
    # MediaTek / Ralink
    "148f": {"desc": "Ralink/MediaTek", "drivers": {
        "5370": "rt2800usb", "5372": "rt2800usb", "5572": "rt2800usb",
        "7601": "mt7601u", "7610": "mt76x0u", "7612": "mt76x2u",
        "7662": "mt7662u", "7615": "mt7615u", "7921": "mt7921u",
    }},
    # Qualcomm Atheros
    "0cf3": {"desc": "Qualcomm Atheros", "drivers": {
        "9271": "ath9k_htc", "7015": "ath9k_htc", "9374": "ath10k_usb",
    }},
    # Intel
    "8086": {"desc": "Intel", "drivers": {
        "08b1": "iwlmvm", "08b2": "iwlmvm",
    }},
}


def _read_sysfs(path: str) -> str:
    """Read a sysfs file, stripping whitespace."""
    try:
        with open(path) as f:
            return f.read().strip()
    except (OSError, PermissionError):
        return ""


def _symlink_target(path: str) -> str:
    """Resolve a symlink and return the basename of the target."""
    try:
        if os.path.islink(path):
            target = os.readlink(path)
            return os.path.basename(target)
    except (OSError, PermissionError):
        pass
    return ""


def _usb_device_driver(dev_path: str) -> str:
    """Find the driver bound to a USB device (or its interfaces)."""
    # Check device-level driver (usually 'usb' for composite devices)
    drv_path = os.path.join(dev_path, "driver")
    driver = _symlink_target(drv_path)
    if driver and driver != "usb":
        return driver

    # Check interface-level drivers
    for entry in sorted(os.listdir(dev_path)):
        if entry.startswith("usb") or entry.startswith("ep_"):
            continue
        iface_path = os.path.join(dev_path, entry)
        if not os.path.isdir(iface_path):
            continue
        driver = _symlink_target(os.path.join(iface_path, "driver"))
        if driver and driver != "usb":
            return driver
    return ""


def _find_net_iface_for_usb(dev_path: str) -> str:
    """Find the network interface associated with a USB WiFi device."""
    # Walk device tree looking for net/ subdirectory
    for root, dirs, _files in os.walk(dev_path, followlinks=False):
        if "net" in dirs:
            net_dir = os.path.join(root, "net")
            try:
                entries = os.listdir(net_dir)
                if entries:
                    return entries[0]
            except (OSError, PermissionError):
                pass
    return ""


def _lookup_usb_wifi_info(vendor_id: str, product_id: str) -> tuple[str, str]:
    """Try to identify vendor/product name for a USB WiFi device."""
    vendor_id_lower = vendor_id.lower()
    vendor_info = _WIFI_USB_VENDOR_DB.get(vendor_id_lower)
    if vendor_info:
        vendor_name = vendor_info["desc"]
        product_id_lower = product_id.lower()
        driver = vendor_info["drivers"].get(product_id_lower, "")
        product_name = f"WiFi Adapter ({product_id})"
        return vendor_name, product_name, driver
    return "", "", ""


def _is_usb_bluetooth(dev_path: str) -> bool:
    """Check if a USB device is a Bluetooth adapter by interface class."""
    for entry in sorted(os.listdir(dev_path)):
        if not entry.endswith(":1.0") and not entry.endswith(":1.1"):
            continue
        iface_path = os.path.join(dev_path, entry)
        # Bluetooth: class e0 (Wireless), subclass 01 (Radio frequency), protocol 01 (Bluetooth)
        cls = _read_sysfs(os.path.join(iface_path, "bInterfaceClass"))
        sub = _read_sysfs(os.path.join(iface_path, "bInterfaceSubClass"))
        if cls.lower() == "e0" and sub.lower() == "01":
            return True
    return False


def _is_usb_hid(dev_path: str) -> bool:
    """Check if a USB device is an HID (keyboard/mouse) device."""
    for entry in sorted(os.listdir(dev_path)):
        iface_path = os.path.join(dev_path, entry)
        cls = _read_sysfs(os.path.join(iface_path, "bInterfaceClass"))
        if cls.lower() == "03":
            return True
    return False


_WIFI_KEYWORDS = [
    "802.11", "wlan", "wifi",
    "wireless lan", "wireless network",
    "network adapter", "radio",
]


def _is_wifi_device(device_text: str) -> bool:
    """Heuristic to determine if a USB device is a WiFi adapter."""
    text_lower = device_text.lower()
    return any(kw in text_lower for kw in _WIFI_KEYWORDS)


def detect_usb_wifi_devices() -> list[USBWiFiDevice]:
    """Detect USB WiFi adapters connected to the system.

    Scans /sys/bus/usb/devices/* to find all USB WiFi adapters,
    checks driver binding, kernel module, and associated network interface.
    """
    devices = []
    usb_root = "/sys/bus/usb/devices"

    if not os.path.isdir(usb_root):
        return devices

    try:
        entries = sorted(os.listdir(usb_root))
    except (OSError, PermissionError):
        return devices

    for entry in entries:
        dev_path = os.path.join(usb_root, entry)
        if not os.path.isdir(dev_path):
            continue

        vendor_id = _read_sysfs(os.path.join(dev_path, "idVendor"))
        product_id = _read_sysfs(os.path.join(dev_path, "idProduct"))
        if not vendor_id or not product_id:
            # Try to read from the ep_00 interface
            for sub in os.listdir(dev_path) if os.path.isdir(dev_path) else []:
                sub_path = os.path.join(dev_path, sub)
                if sub.endswith(":1.0"):
                    vid = _read_sysfs(os.path.join(sub_path, "idVendor"))
                    pid = _read_sysfs(os.path.join(sub_path, "idProduct"))
                    if vid and pid:
                        vendor_id, product_id = vid, pid
                        break
            if not vendor_id or not product_id:
                continue

        # Skip hubs and non-device entries
        product_desc = (os.path.basename(_read_sysfs(os.path.join(dev_path, "product")))
                        if os.path.exists(os.path.join(dev_path, "product")) else "")
        manufacturer = (os.path.basename(_read_sysfs(os.path.join(dev_path, "manufacturer")))
                        if os.path.exists(os.path.join(dev_path, "manufacturer")) else "")
        combined_text = f"{manufacturer} {product_desc}"

        vendor_name, product_name, known_driver = _lookup_usb_wifi_info(vendor_id, product_id)
        if not vendor_name:
            vendor_name = manufacturer

        # Read USB product name from sysfs or usb.ids
        if not product_name:
            product_name = product_desc or f"USB {vendor_id}:{product_id}"

        # Exclude Bluetooth and HID devices regardless of vendor
        if _is_usb_bluetooth(dev_path) or _is_usb_hid(dev_path):
            continue

        # Check if this is a WiFi device by keywords or known vendor
        is_wifi = _is_wifi_device(combined_text)
        vendor_info = _WIFI_USB_VENDOR_DB.get(vendor_id.lower())

        if not is_wifi and not vendor_info:
            continue

        driver = _usb_device_driver(dev_path)
        module = ""
        if driver:
            module = _read_sysfs(os.path.join(dev_path, "driver", "module")) or driver
            if module:
                module = os.path.basename(module)

        if not module and known_driver:
            module = known_driver

        net_iface = _find_net_iface_for_usb(dev_path)

        speed = _read_sysfs(os.path.join(dev_path, "speed"))
        if speed:
            try:
                speed_mbps = int(speed)
                if speed_mbps >= 5000:
                    speed = f"USB 3.0 ({speed_mbps} Mbps)"
                elif speed_mbps >= 480:
                    speed = f"USB 2.0 ({speed_mbps} Mbps)"
                elif speed_mbps >= 12:
                    speed = f"USB 1.1 ({speed_mbps} Mbps)"
                else:
                    speed = f"USB ({speed_mbps} Mbps)"
            except ValueError:
                pass

        if net_iface and driver:
            driver_state = "ready"
        elif driver:
            driver_state = "bound_no_iface"
        elif module:
            driver_state = "driver_available"
        else:
            driver_state = "unbound"

        devices.append(USBWiFiDevice(
            bus=_read_sysfs(os.path.join(dev_path, "busnum")),
            device=_read_sysfs(os.path.join(dev_path, "devnum")),
            vendor_id=vendor_id,
            product_id=product_id,
            vendor_name=vendor_name,
            product_name=product_name,
            driver=driver,
            module=module or known_driver,
            interface=net_iface,
            driver_state=driver_state,
            speed=speed,
            sysfs_path=os.path.realpath(dev_path) if os.path.exists(dev_path) else dev_path,
        ))

    return devices


# ---------- Kernel Module Management ----------

_WIFI_MODULE_PATTERNS = [
    "rtl", "ath", "iwl", "mt7", "brcm", "b43", "wl", "cfg80211",
    "mac80211", "rt2x00", "rtlwifi", "rtl8", "rtw", "8821", "8822",
    "8192", "8188", "8723", "8812", "8814", "8852", "mt76", "iwlmvm",
    "iwlwifi", "bcmdhd", "bcmfmac",
]


def _is_wifi_module(name: str) -> bool:
    """Check if a kernel module appears to be WiFi-related."""
    name_lower = name.lower()
    return any(p in name_lower for p in _WIFI_MODULE_PATTERNS)


def detect_wifi_kernel_modules() -> list[KernelWifiModule]:
    """Detect WiFi-related kernel modules and their loading status.

    Parses /proc/modules and modinfo for each module.
    """
    modules = []
    loaded_modules = {}

    # Parse /proc/modules for loaded modules
    try:
        with open("/proc/modules") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 4:
                    name = parts[0]
                    size = parts[1]
                    use_count = parts[2]
                    used_by = [u.strip() for u in parts[3].split(",") if u.strip() and u.strip() != "-"]
                    loaded_modules[name] = (size, used_by)
    except (OSError, PermissionError):
        pass

    # Try to find all WiFi-related modules from sysfs
    candidate_modules: set[str] = set()

    # Check loaded modules
    for name in loaded_modules:
        if _is_wifi_module(name):
            candidate_modules.add(name)

    # Also check /lib/modules for WiFi-related modules not loaded
    import glob as _glob
    kernel_version = os.uname().release if hasattr(os, "uname") else ""
    if kernel_version:
        mod_dir = f"/lib/modules/{kernel_version}"
    else:
        try:
            kernel_version = _run(["uname", "-r"])[1].strip()
            mod_dir = f"/lib/modules/{kernel_version}"
        except Exception:
            mod_dir = "/lib/modules"

    # Check for known WiFi module paths
    wifi_module_paths = [
        "kernel/drivers/net/wireless",
        "kernel/drivers/net/wireless/realtek",
        "kernel/drivers/net/wireless/mediatek",
        "kernel/drivers/net/wireless/intel",
        "kernel/drivers/net/wireless/ath",
        "kernel/drivers/net/wireless/broadcom",
        "kernel/drivers/staging/rtl*",
    ]
    for pattern in wifi_module_paths:
        full_pattern = os.path.join(mod_dir, pattern)
        try:
            for mod_path in _glob.glob(full_pattern):
                if os.path.isdir(mod_path):
                    for root, _dirs, files in os.walk(mod_path):
                        for f in files:
                            if f.endswith(".ko") or f.endswith(".ko.xz") or f.endswith(".ko.zst"):
                                mod_name = os.path.splitext(os.path.splitext(f)[0])[0] if f.endswith(".ko.xz") or f.endswith(".ko.zst") else os.path.splitext(f)[0]
                                if _is_wifi_module(mod_name):
                                    candidate_modules.add(mod_name)
                elif mod_path.endswith(".ko") or mod_path.endswith(".ko.xz") or mod_path.endswith(".ko.zst"):
                    mod_name = os.path.basename(mod_path)
                    mod_name = os.path.splitext(os.path.splitext(mod_name)[0])[0] if mod_name.endswith(".ko.xz") or mod_name.endswith(".ko.zst") else os.path.splitext(mod_name)[0]
                    if _is_wifi_module(mod_name):
                        candidate_modules.add(mod_name)
        except Exception:
            pass

    # Also check modules referenced by USB devices
    usb_devices = detect_usb_wifi_devices()
    for dev in usb_devices:
        if dev.module:
            candidate_modules.add(dev.module)

    for mod_name in sorted(candidate_modules):
        loaded = mod_name in loaded_modules
        size_info = loaded_modules.get(mod_name, ("0", []))
        size = size_info[0]
        used_by = size_info[1]

        description = ""
        license_str = ""
        version = ""
        code, out, _ = _run(["modinfo", mod_name], timeout=5)
        if code == 0:
            for line in out.split("\n"):
                line = line.strip()
                if line.startswith("description:"):
                    description = line.split(":", 1)[1].strip()
                elif line.startswith("license:"):
                    license_str = line.split(":", 1)[1].strip()
                elif line.startswith("version:"):
                    version = line.split(":", 1)[1].strip()
                elif line.startswith("vermagic:"):
                    pass

        modules.append(KernelWifiModule(
            name=mod_name,
            loaded=loaded,
            size=size,
            used_by=used_by,
            description=description,
            license=license_str,
            version=version,
        ))

    return modules


def get_full_wifi_diagnostics() -> dict:
    """Get comprehensive WiFi hardware diagnostics.

    Returns a dict combining USB device info, kernel modules,
    wireless radios, and interface status.
    """
    usb_devices = [asdict(d) for d in detect_usb_wifi_devices()]
    kernel_modules = [asdict(m) for m in detect_wifi_kernel_modules()]
    radio_info = detect_wireless_radios()
    radio_info["radios"] = [asdict(r) for r in radio_info["radios"]]

    import shutil as _shutil
    has_lsusb = _shutil.which("lsusb") is not None
    lsusb_output = ""
    if has_lsusb:
        _, lsusb_output, _ = _run(["lsusb"], timeout=5)

    has_rfkill = _shutil.which("rfkill") is not None
    rfkill_output = ""
    if has_rfkill:
        _, rfkill_output, _ = _run(["rfkill", "list"], timeout=5)

    # Check if any USB WiFi devices are detected but no radio interfaces
    warnings = []
    if usb_devices and not radio_info["radios"]:
        warnings.append("USB WiFi adapter(s) detected but no wireless interface visible. Kernel module may not be loaded.")
    elif not usb_devices and not radio_info["radios"]:
        warnings.append("No USB WiFi adapters or wireless interfaces detected.")

    for dev in usb_devices:
        if dev["driver_state"] == "unbound":
            warnings.append(f"USB device {dev['vendor_id']}:{dev['product_id']} ({dev['product_name']}) has no driver bound. Try loading '{dev['module']}' module.")
        elif dev["driver_state"] == "bound_no_iface":
            warnings.append(f"USB device {dev['vendor_id']}:{dev['product_id']} ({dev['product_name']}) driver is loaded but no network interface found.")

    return {
        "usb_devices": usb_devices,
        "kernel_modules": kernel_modules,
        "wireless_radios": radio_info,
        "lsusb_output": lsusb_output,
        "rfkill_output": rfkill_output,
        "warnings": warnings,
        "has_lsusb": has_lsusb,
        "has_rfkill": has_rfkill,
    }


def load_kernel_module(module_name: str) -> tuple[bool, str]:
    """Load a kernel module using modprobe.

    Returns (ok, message).
    """
    import shutil as _shutil
    if not _shutil.which("modprobe"):
        return False, "modprobe not available"

    code, _, err = _run(["modprobe", module_name], timeout=15)
    if code == 0:
        return True, f"Module '{module_name}' loaded successfully"
    return False, f"Failed to load '{module_name}': {err.strip() or 'unknown error'}"


def unload_kernel_module(module_name: str) -> tuple[bool, str]:
    """Unload a kernel module using modprobe -r.

    Returns (ok, message).
    """
    import shutil as _shutil
    if not _shutil.which("modprobe"):
        return False, "modprobe not available"

    # Don't allow unloading core WiFi subsystem modules
    core_modules = {"cfg80211", "mac80211"}
    if module_name in core_modules:
        return False, f"Unloading '{module_name}' is not recommended (core WiFi subsystem)"

    code, _, err = _run(["modprobe", "-r", module_name], timeout=15)
    if code == 0:
        return True, f"Module '{module_name}' unloaded successfully"
    return False, f"Failed to unload '{module_name}': {err.strip() or 'unknown error (may be in use)'}"


# ---------- Dependency management ----------

REQUIRED_TOOLS = {
    "iw": "iw",
    "ip": "iproute2",
    "hostapd": "hostapd",
    "dnsmasq": "dnsmasq",
    "wpa_supplicant": "wpasupplicant",
    "dhclient": "isc-dhcp-client",
}


def check_dependencies() -> dict:
    """Check which required tools are available."""
    import shutil as _shutil
    missing = {}
    available = []
    for binary, pkg in REQUIRED_TOOLS.items():
        if _shutil.which(binary):
            available.append(binary)
        else:
            missing[binary] = pkg
    return {"available": available, "missing": missing, "all_ok": len(missing) == 0}


def install_dependencies() -> tuple[bool, str, str]:
    """Attempt to install missing system dependencies.
    Returns (ok, message, manual_install_command)."""
    import shutil as _shutil
    missing = check_dependencies()["missing"]
    if not missing:
        return True, "All dependencies are already installed", ""

    pkgs = list(missing.values())
    err = "no supported package manager"

    # Build manual command hint
    for pm, _, install_cmd in [
        ("apt-get", ["apt-get", "update", "-qq"], ["apt-get", "install", "-y", "-qq"]),
        ("dnf", None, ["dnf", "install", "-y"]),
        ("pacman", None, ["pacman", "-S", "--noconfirm"]),
        ("apk", None, ["apk", "add"]),
    ]:
        if _shutil.which(pm):
            manual_cmd = "sudo " + " ".join(install_cmd + pkgs)
            if pm == "apt-get":
                manual_cmd = "sudo apt-get update && " + manual_cmd
            break
    else:
        manual_cmd = "sudo apt-get install " + " ".join(pkgs)

    for pm, update_cmd, install_cmd in [
        ("apt-get", ["apt-get", "update", "-qq"], ["apt-get", "install", "-y", "-qq"]),
        ("dnf", None, ["dnf", "install", "-y"]),
        ("pacman", None, ["pacman", "-S", "--noconfirm"]),
        ("apk", None, ["apk", "add"]),
    ]:
        if _shutil.which(pm):
            for prefix in (["sudo"], []):
                # Run update first if needed (ignore failure)
                if update_cmd:
                    _run(prefix + update_cmd, timeout=60)
                # Run install
                code, out, err = _run(prefix + install_cmd + pkgs, timeout=120)
                if code == 0 and check_dependencies()["all_ok"]:
                    return True, "Dependencies installed successfully", ""
            return False, f"Auto-install failed. Run manually:", manual_cmd

    # fallback: try apt instead of apt-get
    if _shutil.which("apt"):
        for prefix in (["sudo"], []):
            _run(prefix + ["apt", "update", "-qq"], timeout=60)
            code, _, err = _run(prefix + ["apt", "install", "-y", "-qq"] + pkgs, timeout=120)
            if code == 0 and check_dependencies()["all_ok"]:
                return True, "Dependencies installed successfully", ""

    return False, "Auto-install failed. Run manually:", manual_cmd
