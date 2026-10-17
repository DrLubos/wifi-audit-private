"""What the heartbeat reports about the sensor (server/docs/live-upload.md 4.5):
the capture adapter's identity from sysfs (USB VID:PID and MAC - the identity;
driver, chipset, interface for display), disk, uptime. Nothing about any
other device. Standard library only (tests/test_sensorinfo.py)."""

import os

# driver -> chipset (display only)
CHIPSETS = {"rtw88_8821cu": "RTL8821CU", "rtw88_8822bu": "RTL8822BU", "rtw88_8812au": "RTL8812AU",
            "mt7921u": "MT7921AU", "mt76x2u": "MT7612U", "mt7601u": "MT7601U", "ath9k_htc": "AR9271",
            "rt2800usb": "RT2800/RT3070/RT5370"}


def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def adapter(iface, sysfs="/sys"):
    """{usb_id, mac, driver, chipset, interface} of the capture interface (or
    its Kismet monitor VIF iface+'mon'), None when neither exists."""
    for name in (iface, iface + "mon"):
        net = os.path.join(sysfs, "class", "net", name)
        if not os.path.isdir(net):
            continue
        mac = _read(os.path.join(net, "address"))
        dev = os.path.realpath(os.path.join(net, "device"))
        driver = os.path.basename(os.path.realpath(os.path.join(dev, "driver"))) \
            if os.path.exists(os.path.join(dev, "driver")) else None
        usb = None
        parent = os.path.dirname(dev)          # the USB device above the interface
        vid, pid = _read(os.path.join(parent, "idVendor")), _read(os.path.join(parent, "idProduct"))
        if vid and pid:
            usb = "%s:%s" % (vid.lower(), pid.lower())
        return {"usb_id": usb, "mac": mac.upper() if mac else None, "driver": driver,
                "chipset": CHIPSETS.get(driver), "interface": iface}
    return None


def uptime_s(path="/proc/uptime"):
    v = _read(path)
    try:
        return float(v.split()[0]) if v else None
    except ValueError:
        return None


def disk_free(path="/"):
    try:
        st = os.statvfs(path)
        return st.f_bavail * st.f_frsize
    except OSError:
        return None


def buffer_bytes(db_path):
    total = 0
    for p in (db_path, db_path + "-wal"):
        try:
            total += os.path.getsize(p)
        except OSError:
            pass
    return total or None
