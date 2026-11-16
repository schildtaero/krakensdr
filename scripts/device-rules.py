#!/usr/bin/env python3
"""Render narrowly scoped udev rules; this helper itself changes no OS files."""
import argparse
import re


def rules_for(user: str) -> str:
    if not re.fullmatch(r"[a-z_][a-z0-9_-]*\$?", user):
        raise ValueError("Unsupported local account name for udev owner")
    return f'''# Installed by KrakenDF setup; owner is the local demo operator.
# Only factory Kraken tuner serials 1000..1004; do not grant all RTL devices.
SUBSYSTEM=="usb", ENV{{DEVTYPE}}=="usb_device", ATTR{{idVendor}}=="0bda", ATTR{{idProduct}}=="2838", ATTR{{serial}}=="100[0-4]", OWNER="{user}", MODE="0660", TAG+="uaccess", RUN+="/usr/lib/udev/krakendf-usb-memory"
SUBSYSTEM=="usb", ENV{{DEVTYPE}}=="usb_device", ATTR{{idVendor}}=="0bda", ATTR{{idProduct}}=="2832", ATTR{{serial}}=="100[0-4]", OWNER="{user}", MODE="0660", TAG+="uaccess", RUN+="/usr/lib/udev/krakendf-usb-memory"
# VK-162 units commonly expose the native u-blox USB vendor 1546.
# Other USB-serial variants can be configured by port in the app.
SUBSYSTEM=="tty", ATTRS{{idVendor}}=="1546", OWNER="{user}", GROUP="dialout", MODE="0660", TAG+="uaccess"
'''


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", required=True)
    args = parser.parse_args()
    print(rules_for(args.user), end="")
