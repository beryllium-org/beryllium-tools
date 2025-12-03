#!/usr/bin/env python3
import os, re, sys


sdio_devices = {
    "0020": {"name": "ST-Ericsson", "devices": {"2280": "CW1200"}},
    "0089": {"name": "Intel Corp.", "devices": {}},
    "0092": {
        "name": "C-guys, Inc.",
        "devices": {
            "0001": "SD-Link11b WiFi Card (TI ACX100)",
            "0004": "EW-CG1102GC",
            "0005": "SD FM Radio 2",
            "5544": "SD FM Radio",
        },
    },
    "0097": {"name": "Texas Instruments, Inc.", "devices": {"4076": "WL1271"}},
    "0098": {
        "name": "Toshiba Corp.",
        "devices": {
            "0001": "SD BT Card 1",
            "0002": "SD BT Card 2",
            "0003": "SD BT Card 3",
        },
    },
    "0104": {
        "name": "Socket Communications, Inc.",
        "devices": {"005e": "SD Scanner", "00c5": "Bluetooth SDIO Card"},
    },
    "0271": {
        "name": "Atheros Communications, Inc.",
        "devices": {
            "0108": "AR6001",
            "0109": "AR6001",
            "010a": "AR6001",
            "010b": "AR6001",
        },
    },
    "0296": {"name": "GCT Semiconductor, Inc.", "devices": {"5347": "GDM72xx WiMAX"}},
    "02d0": {
        "name": "Broadcom Corp.",
        "devices": {
            "044b": "Nintendo Wii WLAN daughter card",
            "a887": "BCM43143 WLAN card",
            "a9a6": "BCM43438 combo WLAN and Bluetooth Low Energy (BLE)",
            "4324": "BCM43241 WLAN card",
            "4329": "BCM4329 WLAN card",
            "4330": "BCM4330 WLAN card",
            "4334": "BCM4334 WLAN card",
            "a94c": "BCM43340 WLAN card",
            "a94d": "BCM43341 WLAN card",
            "4335": "BCM4335/BCM4339 WLAN card",
            "a962": "BCM43362 WLAN card",
            "4354": "BCM4354 WLAN card",
            "aae8": "BCM43752 WLAN card (AP6275s)",
        },
    },
    "02db": {
        "name": "SyChip Inc.",
        "devices": {"0002": "Pegasus WLAN SDIO Card (6060SD)"},
    },
    "02df": {
        "name": "Marvell Technology Group Ltd.",
        "devices": {
            "9103": "Libertas",
            "9104": "SD8688 WLAN",
            "9105": "SD8688 BT",
            "9116": "SD8786 WLAN",
            "9119": "SD8787 WLAN",
            "911a": "SD8787 BT",
            "911b": "SD8787 BT AMP",
            "9129": "SD8797 WLAN",
            "912a": "SD8797 BT",
            "912e": "SD8897 BT",
            "912d": "SD8897 WLAN",
        },
    },
    "02fe": {
        "name": "Spectec Computer Co., Ltd",
        "devices": {"2128": "SDIO WLAN Card (SDW820)"},
    },
    "032a": {
        "name": "Cambridge Silicon Radio",
        "devices": {
            "0001": "UniFi 1",
            "0002": "UniFi 2",
            "0007": "UniFi 3",
            "0008": "UniFi 4",
        },
    },
    "037a": {"name": "MediaTek Inc.", "devices": {"5911": "Spectec WLAN-11b/g"}},
    "039a": {"name": "Siano Mobile Silicon", "devices": {}},
    "0501": {
        "name": "Globalsat Technology Co.",
        "devices": {"f501": "SD-501 GPS Card"},
    },
    "104c": {"name": "Texas Instruments, Inc.", "devices": {"9066": "WL1251"}},
    "1180": {"name": "Ricoh Co., Ltd", "devices": {"e823": "MMC card reader"}},
    "13d1": {"name": "AboCom Systems, Inc.", "devices": {"ac02": "SDW11G"}},
}


def read_sys_file(path):
    try:
        with open(path, "r") as f:
            return f.read().strip()
    except Exception:
        return None


def get_mmc_devices():
    base_path = "/sys/class/mmc_host"
    devices = []

    if not os.path.exists(base_path):
        return devices

    for host in os.listdir(base_path):
        if not re.match(r"mmc\d+", host):
            continue
        host_path = os.path.join(base_path, host)
        if not os.path.isdir(host_path):
            continue

        for entry in os.listdir(host_path):
            if not re.match(rf"{host}:\d+", entry):
                continue

            device_path = os.path.join(host_path, entry)

            dev_info = {
                "host": host,
                "dev": entry,
                "manfid": read_sys_file(os.path.join(device_path, "manfid")),
                "oemid": read_sys_file(os.path.join(device_path, "oemid")),
                "serial": read_sys_file(os.path.join(device_path, "serial")),
                "type": read_sys_file(os.path.join(device_path, "type")),
                "name": read_sys_file(os.path.join(device_path, "name")),
                "cid": read_sys_file(os.path.join(device_path, "cid")),
                "vendor": read_sys_file(os.path.join(device_path, "vendor")),
                "device": read_sys_file(os.path.join(device_path, "device")),
            }

            devices.append(dev_info)

    return devices


def format_mmc_entry(dev, verbose=False):
    is_sdio = dev.get("vendor") and dev.get("device")
    vendor = dev["vendor"] if is_sdio else dev.get("manfid", "0000")
    device = dev["device"] if is_sdio else dev.get("oemid", "0000")

    vendor = vendor[-4:].rjust(4, "0").lower()
    device = device[-4:].rjust(4, "0").lower()

    name = dev.get("name") or "Unknown SDIO device"
    serial = dev.get("serial") or "XXXXXXXX"

    if vendor in sdio_devices:
        if device in sdio_devices[vendor]["devices"]:
            name = (
                sdio_devices[vendor]["name"]
                + " "
                + sdio_devices[vendor]["devices"][device]
            )

    # Extract mmcX → bus number
    bus_match = re.match(r"mmc(\d+)", dev["host"])
    busnum = bus_match.group(1) if bus_match else "0"

    # Extract mmcX:00YY → devnum
    dev_match = re.match(r".+:(\d+)", dev["dev"])
    devnum = dev_match.group(1) if dev_match else "0000"

    line = f"Bus {busnum} Device {devnum} ID {vendor}:{device} {name}"
    if verbose:
        line += f" serial {serial}"
    return line


def main():
    verbose = "-v" in sys.argv[1:]
    devices = get_mmc_devices()
    for dev in devices:
        print(format_mmc_entry(dev, verbose=verbose))


if __name__ == "__main__":
    main()
