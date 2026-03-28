#!/bin/bash

set -e  # Exit immediately if a command fails

CHROOT_DIR="/mnt/chroot"
NO_CONFIRM=0
AUTO_EJECT=0

# Help message
usage() {
  echo "Usage: $0 [--noconfirm] [--eject] <btrfs_partition> <boot_partition>"
  echo
  echo "Example:"
  echo "  $0 /dev/sdb3 /dev/sdb2"
  echo
  echo "Options:"
  echo "  --noconfirm, -c, --no-confirm  Suppress prompts, but wait with info"
  echo "  --eject, -e                    Automatically eject device after chroot"
  exit 1
}

# Parse flags
POSITIONAL=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --noconfirm|--no-confirm|-c)
      NO_CONFIRM=1
      shift
      ;;
    --eject|-e)
      AUTO_EJECT=1
      shift
      ;;
    -*)
      echo "Unknown option: $1"
      usage
      ;;
    *)
      POSITIONAL+=("$1")
      shift
      ;;
  esac
done
set -- "${POSITIONAL[@]}"

# Ensure two arguments are provided
if [[ $# -ne 2 ]]; then
  echo "Error: Missing required arguments."
  usage
fi

BTRFS_PART="$1"
BOOT_PART="$2"
BASE_DEV=$(lsblk -no pkname "$BTRFS_PART" | head -n1)
BASE_DEV="/dev/${BASE_DEV}"

# Check if devices exist
for dev in "$BTRFS_PART" "$BOOT_PART"; do
  if [[ ! -b "$dev" ]]; then
    echo "Error: Device $dev does not exist."
    exit 1
  fi
done

# Preview
echo " Btrfs partition: ${BTRFS_PART}"
echo "   └─ subvol=@     -> $CHROOT_DIR"
echo "(Other subvolumes and boot will be mounted after root is checked)"
echo

if [[ $NO_CONFIRM -eq 0 ]]; then
  read -r -p "Proceed with initial root mount? [y/N] " confirm
  [[ "$confirm" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 1; }
else
  echo "[--noconfirm] Proceeding with initial root mount..."
  sleep 1
fi

# Create chroot directory if needed
if [[ -e "$CHROOT_DIR" ]]; then
  if [[ ! -d "$CHROOT_DIR" ]]; then
    echo "Error: $CHROOT_DIR exists but is not a directory."
    exit 1
  elif [[ -n "$(ls -A "$CHROOT_DIR")" ]]; then
    echo "Error: $CHROOT_DIR is not empty."
    exit 1
  fi
else
  sudo mkdir -p "$CHROOT_DIR"
fi

# Mount root subvolume only
sudo mount -t btrfs -o subvol=@ "$BTRFS_PART" "$CHROOT_DIR"

# Determine boot path
if [[ -d "$CHROOT_DIR/boot/efi" ]]; then
  BOOT_PATH="$CHROOT_DIR/boot/efi"
elif [[ -d "$CHROOT_DIR/boot" ]]; then
  BOOT_PATH="$CHROOT_DIR/boot"
else
  echo "No /boot or /boot/efi found in root subvolume."
  if [[ $NO_CONFIRM -eq 0 ]]; then
    read -r -p "Create and use /boot/efi? [y/N] " mkboot
    if [[ "$mkboot" =~ ^[Yy]$ ]]; then
      sudo mkdir -p "$CHROOT_DIR/boot/efi"
      BOOT_PATH="$CHROOT_DIR/boot/efi"
    fi
  else
    echo "[--noconfirm] Creating /boot/efi"
    sudo mkdir -p "$CHROOT_DIR/boot/efi"
    BOOT_PATH="$CHROOT_DIR/boot/efi"
  fi
fi

# Prompt to continue full mount
echo
echo " Ready to mount: ${BTRFS_PART}"
echo "   ├─ subvol=@home -> $CHROOT_DIR/home"
echo "   ├─ subvol=@log  -> $CHROOT_DIR/var/log"
echo "   └─ subvol=@pkg  -> $CHROOT_DIR/var/cache/pacman/pkg"
[[ -n "$BOOT_PATH" ]] && (
    echo " Boot partition:" && \
    echo "   └─ ${BOOT_PART} -> ${BOOT_PATH}"
)

if [[ $NO_CONFIRM -eq 0 ]]; then
  read -r -p "Continue? [y/N] " do_mount
  if ! [[ "$do_mount" =~ ^[Yy]$ ]]; then
    echo "Aborted. Unmounting root subvolume."
    sudo umount "$CHROOT_DIR"
    [[ -z "$(ls -A "$CHROOT_DIR" 2>/dev/null)" ]] && sudo rmdir "$CHROOT_DIR"
    exit 0
  fi
else
  echo "[--noconfirm] Continuing with mount..."
  sleep 1
fi

# Mount the rest
sudo mount -t btrfs -o subvol=@home "$BTRFS_PART" "$CHROOT_DIR/home"
sudo mount -t btrfs -o subvol=@log "$BTRFS_PART" "$CHROOT_DIR/var/log"
sudo mount -t btrfs -o subvol=@pkg "$BTRFS_PART" "$CHROOT_DIR/var/cache/pacman/pkg"
[[ -n "$BOOT_PATH" ]] && sudo mount "$BOOT_PART" "$BOOT_PATH"

# Enter chroot
sudo arch-chroot "$CHROOT_DIR"

# Unmount in reverse order
echo "Unmounting..."
[[ -n "$BOOT_PATH" ]] && sudo umount "$BOOT_PATH"
sudo umount "$CHROOT_DIR/var/cache/pacman/pkg"
sudo umount "$CHROOT_DIR/var/log"
sudo umount "$CHROOT_DIR/home"
sudo umount "$CHROOT_DIR"

# Clean up
[[ -z "$(ls -A "$CHROOT_DIR" 2>/dev/null)" ]] && sudo rmdir "$CHROOT_DIR" && echo "Removed $CHROOT_DIR"

# Ask if user wants to eject
if [[ $AUTO_EJECT -eq 1 ]]; then
  echo "[--eject] Ejecting $BASE_DEV"
  sudo eject "$BASE_DEV" || echo "Warning: Failed to eject $BASE_DEV"
elif [[ $NO_CONFIRM -eq 0 ]]; then
  read -r -p "Eject base device $BASE_DEV? [y/N] " eject_ans
  if [[ "$eject_ans" =~ ^[Yy]$ ]]; then
    sudo eject "$BASE_DEV" || echo "Warning: Failed to eject $BASE_DEV"
  fi
fi

exit 0
