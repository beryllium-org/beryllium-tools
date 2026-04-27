#!/usr/bin/env python3
import argparse
import os
import subprocess
import sys
import time

FASTBOOT_BIN = "/usr/bin/fastboot"
ASSET_DIR = "/usr/share/rvflasher"
FSBL_IMAGE = f"{ASSET_DIR}/FSBL.bin"
UBOOT_IMAGE = f"{ASSET_DIR}/u-boot.itb"
GPT_IMAGE = f"{ASSET_DIR}/partition_universal.json"
BOOTINFO_IMAGE = f"{ASSET_DIR}/bootinfo_sd.bin"


def run_command(cmd, capture_output=False):
    try:
        return subprocess.run(
            cmd, text=True, capture_output=capture_output, check=False
        )
    except OSError as error:
        print(f"Error running command: {' '.join(cmd)}")
        print(error)
        sys.exit(1)


def run_required(cmd, capture_output=False):
    result = run_command(cmd, capture_output=capture_output)
    if result.returncode != 0:
        if result.stdout:
            print(result.stdout.strip())
        if result.stderr:
            print(result.stderr.strip())
        print(f"Command failed: {' '.join(cmd)}")
        sys.exit(result.returncode or 1)
    return result


def ensure_fastboot():
    if os.path.exists(FASTBOOT_BIN):
        return

    print(f"{FASTBOOT_BIN} not found. Attempting to install...")
    run_required(["sudo", "pacman", "-Sy"])
    run_required(["sudo", "pacman", "-S", "--noconfirm", "android-tools"])

    if not os.path.exists(FASTBOOT_BIN):
        print(
            "Error: fastboot could not be installed. Please install 'android-tools' manually."
        )
        sys.exit(1)
    print("Installation successful.")


def wait_for_android_fastboot(timeout_seconds=10):
    print(f"Waiting for device to re-enumerate ({timeout_seconds}s timeout)...")
    start_time = time.time()

    while time.time() - start_time < timeout_seconds:
        devices_output = run_required(
            [FASTBOOT_BIN, "devices"], capture_output=True
        ).stdout
        if "Android Fastboot" in devices_output:
            print("Device detected in Android Fastboot mode.")
            return
        time.sleep(0.5)

    print(
        "Timeout: Device did not appear in 'Android Fastboot' mode within 10 seconds."
    )
    sys.exit(1)


def require_file(path, label):
    if not os.path.isfile(path):
        print(f"Missing {label}: {path}")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Flash eMMC image using the RV flasher flow."
    )
    parser.add_argument(
        "image", help="Path to image file to flash to the emmc partition"
    )
    args = parser.parse_args()

    ensure_fastboot()

    require_file(FSBL_IMAGE, "FSBL image")
    require_file(UBOOT_IMAGE, "U-Boot image")
    require_file(GPT_IMAGE, "partition layout")
    require_file(BOOTINFO_IMAGE, "bootinfo image")
    require_file(args.image, "input image")

    print("Checking device status...")
    devices_output = run_required([FASTBOOT_BIN, "devices"], capture_output=True).stdout
    if "Android Fastboot" not in devices_output and "dfu-device" not in devices_output:
        print("No compatible device detected.")
        sys.exit(1)

    print(f"Staging FSBL: {FSBL_IMAGE}")
    run_required([FASTBOOT_BIN, "stage", FSBL_IMAGE])
    print("Continuing boot...")
    run_required([FASTBOOT_BIN, "continue"])
    wait_for_android_fastboot()

    print(f"Staging U-Boot: {UBOOT_IMAGE}")
    run_required([FASTBOOT_BIN, "stage", UBOOT_IMAGE])
    print("Continuing boot...")
    run_required([FASTBOOT_BIN, "continue"])

    print(f"Flashing GPT: {GPT_IMAGE}")
    run_required([FASTBOOT_BIN, "flash", "gpt", GPT_IMAGE])
    print(f"Flashing bootinfo: {BOOTINFO_IMAGE}")
    run_required([FASTBOOT_BIN, "flash", "bootinfo", BOOTINFO_IMAGE])
    print(f"Flashing FSBL: {FSBL_IMAGE}")
    run_required([FASTBOOT_BIN, "flash", "fsbl", FSBL_IMAGE])
    print(f"Flashing eMMC image: {args.image}")
    run_required([FASTBOOT_BIN, "flash", "emmc", args.image])

    print("Flashing completed successfully.")

    print("\nRebooting device..")
    sleep(0.5)
    run_required([FASTBOOT_BIN, "reboot"])


if __name__ == "__main__":
    main()
