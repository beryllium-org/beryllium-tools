#!/bin/bash

# Rockchip flash dumper script.
# Requires the device to be in maskrom (with a loaded spl) or in recovery mode.
#
# This script is part of BredOS-Tools, licenced under the GPL-3.0 licence.
# Bill Sideris <bill88t@bredos.org>

show_help() {
    echo "Usage: $0 [--first N|-f N] [--spi|-s|--emmc|-e|--sd|-d] <filename>"
    echo ""
    echo "Description:"
    echo "  Dumps data from a device connected via rkdeveloptool to a specified file."
    echo ""
    echo "Arguments:"
    echo "  --first N, -f N   Optional. Limit dump to the first N megabytes."
    echo "  --spi, -s         Select SPI flash storage."
    echo "  --emmc, -e        Select eMMC storage."
    echo "  --sd, -d          Select SD card storage."
    echo "  <filename>        The name of the output file where the dump will be saved."
    echo ""
    echo "Example:"
    echo "  $0 --first 128 dump.img"
    echo ""
    echo "Requirements:"
    echo "  - sudo privileges to run rkdeveloptool commands."
    echo "  - rkdeveloptool must be installed and accessible in the PATH."
}

# Parse args
size_mb=""
filename=""
storage_id=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)
            show_help
            exit 0
            ;;
        --first|-f)
            if [[ -n "$2" && "$2" =~ ^[0-9]+$ ]]; then
                size_mb="$2"
                shift
            else
                echo "Error: --first/-f requires a positive integer argument."
                exit 1
            fi
            ;;
        --spi|-s)
            storage_id=9
            ;;
        --emmc|-e)
            storage_id=1
            ;;
        --sd|-d)
            storage_id=2
            ;;
        -*)
            echo "Error: Unknown option '$1'"
            exit 1
            ;;
        *)
            if [[ -z "$filename" ]]; then
                filename="$1"
            else
                echo "Error: Multiple filenames or unexpected argument '$1'"
                exit 1
            fi
            ;;
    esac
    shift
done

if [[ -z "$filename" ]]; then
    echo "Error: Please provide a filename."
    show_help
    exit 1
fi

# Check if rkdeveloptool is installed
if ! command -v rkdeveloptool &>/dev/null; then
    echo "Error: rkdeveloptool is not installed or not in your PATH."
    echo "Please install rkdeveloptool and try again."
    exit 1
fi

# Change storage if requested
if [[ -n "$storage_id" ]]; then
    echo "Switching storage to ID $storage_id..."
    if ! sudo rkdeveloptool cs "$storage_id"; then
        echo "Error: Failed to switch storage."
        exit 1
    fi
fi

# Get flash info
output=$(sudo rkdeveloptool rfi 2>/dev/null)
sector_count=$(echo "$output" | grep -i "Sectors" | awk '{print $3}')
if [[ -z "$sector_count" || ! "$sector_count" =~ ^[0-9]+$ ]]; then
    echo "Error: Unable to extract a valid sector count from rkdeveloptool."
    exit 1
fi

# Convert limit if requested
sectors_per_mb=2048  # 512-byte sector size
if [[ -n "$size_mb" ]]; then
    limit_sectors=$(( size_mb * sectors_per_mb ))
    if (( limit_sectors > sector_count )); then
        echo "Error: Requested size ($size_mb MB) exceeds device capacity ($(sector_count / sectors_per_mb) MB)."
        exit 1
    fi
else
    limit_sectors=$sector_count
fi

echo "Dumping $limit_sectors sectors ($(($limit_sectors / sectors_per_mb)) MB) to $filename..."

# Dump
sudo rkdeveloptool rl 0 "$limit_sectors" "$filename" && sudo chown $UID:$UID "$filename"

if [[ $? -eq 0 ]]; then
    echo "Successfully dumped $limit_sectors sectors to $filename"
else
    echo "Error: Dump failed!"
    exit 1
fi
