# Troubleshooting USB Wi-Fi Adapters (From Plug-in to AP Mode)

This guide walks through the full bring-up chain of a USB Wi-Fi adapter on
Linux: detection, driver loading, and AP-capability verification — everything
that has to work *before* smart-wifi (or any hostapd-based hotspot) can start.
It is distilled from a real-world bring-up on a Unitree robot (custom kernel
`6.5.2-rt8`, NetworkManager 1.36) with a Tenda USB adapter based on the
AICSemi AIC8800D80 chipset.

## Symptom

You plug in a USB Wi-Fi adapter, but:

```bash
iw dev          # prints nothing — no wireless interface at all
```

and smart-wifi cannot start an AP because the interface from
`WIFI_INTERFACE` does not exist.

## Step 1 — Check whether the adapter was detected at all

```bash
lsusb                      # is the adapter listed?
sudo dmesg | grep -iE 'usb|wlan|wifi|ieee80211|cfg80211'
```

Three typical outcomes:

1. **Not in `lsusb` at all** — power delivery or hub issue. Try another port
   (prefer a powered hub; robot/NUC USB ports are often current-limited).
2. **Listed, and `dmesg` shows an `ieee80211` driver registering** — go to
   Step 3.
3. **Listed, but it was claimed by `usb-storage`** — the adapter is in
   "Zero-CD" (mass-storage / driver-CD) mode. Go to Step 2.

## Step 2 — Switch out of Zero-CD (MSC) mode

Many USB Wi-Fi adapters enumerate first as a virtual CD-ROM or flash disk
holding Windows drivers. The kernel logs show `usb-storage` claiming it, and
`lsblk -S` lists a disk like:

```text
NAME HCTL       TYPE VENDOR   MODEL  REV SERIAL   TRAN
sda  2:0:0:0    disk AIC      flash 1.0  20200203 usb
```

Real-world example — an AIC8800D80 adapter in MSC mode shows up as:

```text
Bus 001 Device 005: ID a69c:5730 aicsemi Aic MSC
```

Send a SCSI *eject* to make it re-enumerate as a Wi-Fi device:

```bash
sudo eject /dev/sda
# alternatives if eject is unavailable:
sudo usb_modeswitch -v 0xa69c -p 0x5730 ...   # needs a matching message
sudo sg_raw /dev/sda 1b 00 00 00 02 00        # raw SCSI START STOP UNIT
```

After re-enumeration the same physical device appears with its real ID:

```text
Bus 001 Device 007: ID 2604:0023 Tenda AIC 8800D80
```

Note that the *vendor/product ID changes* when leaving MSC mode — match the
Wi-Fi driver against the **second** ID, not the first.

## Step 3 — Check whether a driver claims it

```bash
iw dev                                    # wlanX interface present?
lsmod | grep -iE 'aic|8821|8812|mt76'     # driver loaded?
modinfo <driver_module> | grep 'usb:'     # does its ID table contain your ID?
```

If the interface exists, continue with Step 4. If the module is loaded but
your `vendor:product` ID is **not** in its `alias` table, the kernel will
silently ignore the device.

### Do NOT force-bind via `new_id`

It is tempting to do this:

```bash
echo "2604 0023" | sudo tee /sys/bus/usb/drivers/aic8800_fdrv/new_id   # DANGEROUS
```

**Don't.** Vendor Wi-Fi drivers typically branch on the USB ID to select
chip-specific configuration. An unlisted ID can walk into an uninitialised
branch and crash the kernel. In the real-world case above, the forced bind
caused an immediate NULL-pointer `oops` inside `aic8800_fdrv`:

```text
BUG: kernel NULL pointer dereference
note: tee[175082] exited with irqs disabled
```

After the oops the system was left half-alive: `lsusb`, `ps` and writes to
sysfs all hung, load average climbed above 15. Recovery options, in order of
preference:

1. `echo "2604 0023" | sudo tee /sys/bus/usb/drivers/aic8800_fdrv/remove_id`
   and unplug the adapter (worked in this case — but only if sysfs still
   responds);
2. physically unplug the adapter;
3. reboot.

If you ever experiment with `new_id` on such drivers, do it on a machine you
can afford to reboot, never on a production robot.

### The correct path: build an up-to-date driver

For AIC8800-family chips (AIC8800D80, AIC8800DC — common in Tenda, Mercusys,
TP-Link AX300-class USB adapters), actively maintained drivers exist:

| Repository | Notes |
|---|---|
| <https://github.com/shenmintao/aic8800d80> | dedicated AIC8800D80 driver, actively maintained |
| <https://github.com/Kiborgik/aic8800dc-linux-patched> | AIC8800DC family (Tenda/Mercusys/TP-Link), kernel 6.2–7.4+, DKMS support |
| <https://github.com/radxa-pkg/aic8800> | Radxa packaging of the AICsemi driver |

Building an out-of-tree module requires the headers of the **running**
kernel (`/lib/modules/$(uname -r)/build`). On vendor kernels (robots,
appliances) those headers are often missing — check for them before
planning the build, and prefer a DKMS package so the module survives kernel
updates.

## Step 4 — Verify the adapter supports AP mode

An interface existing is not enough; the chipset/firmware must support the
`AP` interface mode, otherwise `hostapd` will fail to start:

```bash
iw list | grep -A 12 'Supported interface modes'
```

Expected output must contain `* AP`:

```text
Supported interface modes:
     * IBSS
     * managed
     * AP
     * AP/VLAN
     ...
```

If `* AP` is missing, that adapter cannot serve as a hotspot on Linux
regardless of the software you use.

## Step 5 — Sanity-check the AP data path before smart-wifi

To isolate hardware/driver problems from application problems, bring up a
minimal AP with NetworkManager and try to associate a client:

```bash
nmcli connection add type wifi ifname wlan0 con-name ap-test ssid test-ap \
  802-11-wireless.mode ap \
  802-11-wireless-security.key-mgmt wpa-psk \
  ipv4.method shared \
  wifi-sec.psk "changeme123"
nmcli connection up ap-test
```

If a client can connect and receive a DHCP lease, the adapter is fully
capable — any remaining problem is in the hostapd/dnsmasq configuration that
smart-wifi manages. Remember to remove the test profile afterwards:

```bash
nmcli connection delete ap-test
```

## Quick checklist

| Check | Command | Pass criterion |
|---|---|---|
| USB detection | `lsusb` | adapter listed |
| Storage mode | `lsblk -S` / `dmesg` | no unexpected `usb-storage` disk |
| Mode switch | `sudo eject /dev/sdX` | re-enumerates with Wi-Fi ID |
| Driver bound | `iw dev` | `wlanX` interface exists |
| Driver ID table | `modinfo <mod> \| grep 'usb:'` | your `vendor:product` listed |
| AP capability | `iw list` | `* AP` in supported modes |
| AP data path | `nmcli ... mode ap` (above) | client connects + gets IP |
