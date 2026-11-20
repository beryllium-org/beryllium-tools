#!/usr/bin/env python3
# beryllium-fde: Full Disk Encryption for Beryllium OS.
#
# Two phases:
#   beryllium-fde prepare   (run on the unencrypted system, read-only audit + handoff)
#   beryllium-fde live      (run from the live ISO: picks the disk, converts it)
#
# Target layout (the endorsed Beryllium OS FDE design):
#   p1 ESP (vfat) holds ONLY EFI/BOOT/BOOTAA64.EFI (or BOOTX64.EFI)
#   p2 LUKS2:
#     slot 0: user passphrase, pbkdf2/sha256
#     slot 1: 64-byte keyfile, argon2id, for silent initramfs unlock
#   keyfile lives at /etc/cryptsetup-keys.d/cryptroot.key and is embedded
#   into the (encrypted) initramfs via dracut install_items.
#   Result: exactly one passphrase prompt, at GRUB.

import argparse, errno, fcntl, getpass, json, os, re, platform, shutil, signal, subprocess, sys, time

HANDOFF_DIR = "/var/lib/beryllium-fde"
HANDOFF_FILE = HANDOFF_DIR + "/handoff.json"
WORKDIR = "/run/beryllium-fde"
ROOT_MNT = WORKDIR + "/root"
# single-instance lock for the destructive live phase; it deliberately
# lives OUTSIDE WORKDIR so cleanup()'s rmtree can never delete it
LOCK_FILE = "/run/beryllium-fde.lock"
MAPPER_NAME = "cryptroot"  # Don't change this.
MAPPER_DEV = "/dev/mapper/" + MAPPER_NAME
PASSFILE = WORKDIR + "/passphrase"
KEYFILE = WORKDIR + "/cryptroot.key"
KEYFILE_INSTALLED = "/etc/cryptsetup-keys.d/cryptroot.key"
TAIL_SHRINK = "64M"
REDUCE_SIZE = "32M"
MIN_FREE_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB
RETRY_DELAY = 2  # seconds between retries of operations that may stall

# partition classification for the strict target-layout checks
ESP_FSTYPES = ("vfat", "fat32")
# Known types that hold data: the endorsed layout has exactly one ESP and
# exactly one btrfs root, so any *other* partition with one of these types
# is an extra data partition and blocks the conversion. Everything not
# listed here is either a system/non-data role or of unknown type and
# stays out of the layout decision (see partition_role). Swap is a
# non-data role: an ordinary swap partition is deliberately ignored and
# never blocks an otherwise valid disk.
DATA_FSTYPES = frozenset(
    {
        "ext2",
        "ext3",
        "ext4",
        "xfs",
        "f2fs",
        "jfs",
        "reiserfs",
        "nilfs2",
        "bcachefs",
        "ntfs",
        "exfat",
        "udf",
        "hfs",
        "hfsplus",
        "zfs",
        "LVM2_member",
        "linux_raid_member",
        "squashfs",
    }
)

# Stage numbers, handoff["stage_completed"] holds the last FINISHED one
ST_BACKUP = 0
ST_TAIL = 1
ST_ENCRYPT = 2
ST_RECLAIM = 3
ST_KEYGEN = 4
ST_KEYNOTICE = 5  # Informational keyfile-path pause (no automatic backup)
ST_ENROLL = 6
ST_CONFIG = 7
ST_DRACUTCONF = 8
ST_CHROOT = 9
ST_PRUNE = 10
ST_VERIFY = 11
ST_DONE = 12

REQUIRED_BINS = [
    "cryptsetup",
    "dracut",
    "grub-install",
    "grub-mkconfig",
    "grub-script-check",
    "btrfs",
    "arch-chroot",
    "lsblk",
    "findmnt",
    "mount",
    "umount",
]


def flush_stdin() -> None:
    # Discard any buffered input so stale Enter presses cannot
    # answer the next prompt. Harmless when stdin is not a tty.
    try:
        import termios

        fd = sys.stdin.fileno()
        termios.tcflush(fd, termios.TCIFLUSH)
    except Exception:
        pass


def _aborted() -> None:
    print("\nAborted.")
    raise SystemExit(130)


def ask(prompt: str) -> str:
    flush_stdin()
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        # Ctrl+D and Ctrl+C both abort instead of answering the prompt
        _aborted()
        raise AssertionError("unreachable: _aborted() exits the process")


def ask_yes_no(prompt: str, default: bool | None = None) -> bool:
    while True:
        suffix = ""
        if default is True:
            suffix = " [Y/n]"
        elif default is False:
            suffix = " [y/N]"
        answer = ask(prompt + suffix + " ").strip().lower()
        if answer == "" and default is not None:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("Please answer y or n.")


def press_enter(prompt: str = "Press Enter to continue...") -> None:
    ask(prompt)


def ask_passphrase(prompt: str) -> str:
    flush_stdin()
    try:
        return getpass.getpass(prompt)
    except (EOFError, KeyboardInterrupt):
        # Ctrl+D and Ctrl+C both abort instead of answering the prompt
        _aborted()
        raise AssertionError("unreachable: _aborted() exits the process")


def run_command(
    cmd: list[str], capture_output: bool = False
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            cmd, text=True, capture_output=capture_output, check=False
        )
    except OSError as error:
        print(f"Error running command: {' '.join(cmd)}")
        print(error)
        sys.exit(1)


def run_required(
    cmd: list[str], capture_output: bool = False
) -> subprocess.CompletedProcess[str]:
    result = run_command(cmd, capture_output=capture_output)
    if result.returncode != 0:
        if result.stdout:
            print(result.stdout.strip())
        if result.stderr:
            print(result.stderr.strip())
        print(f"Command failed: {' '.join(cmd)}")
        sys.exit(result.returncode or 1)
    return result


def die(message: str) -> None:
    print("Error: " + message)
    sys.exit(1)


def human_size(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024:
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024.0
    return f"{n:.1f}PiB"


def require_root() -> None:
    if os.geteuid() != 0:
        die("this tool must run as root")


def require_bins(where: str) -> None:
    # where: "live environment" or a description of the target
    missing = [b for b in REQUIRED_BINS if shutil.which(b) is None]
    if missing:
        die(f"missing required commands in the {where}: " + ", ".join(missing))


def lsblk_tree() -> list:
    result = run_required(
        [
            "lsblk",
            "-J",
            "-b",
            "-o",
            "NAME,TYPE,SIZE,SERIAL,MODEL,TRAN,FSTYPE,PTUUID,MOUNTPOINT,RM,UUID",
        ],
        capture_output=True,
    )
    return json.loads(result.stdout)["blockdevices"]


def lsblk_value(device: str, column: str) -> str | None:
    result = run_command(["lsblk", "-dno", column, device], capture_output=True)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def device_fstype(device: str) -> str | None:
    return lsblk_value(device, "FSTYPE")


# ------------------------------------------------- layout classification ---


def partition_role(fstype: str | None) -> str | None:
    """Classify a partition for the strict target-layout checks.

    'esp' for a FAT/ESP, 'btrfs' for the data root, 'luks' for a LUKS
    container, 'data' for any other known data holder -- and None for
    everything that must stay out of the decision: partitions without a
    recognizable filesystem (BIOS boot and similar system/non-data roles),
    swap partitions (non-data by definition, so they never block a disk)
    and partitions of unknown type. Ignoring those is what keeps an
    unusual-but-harmless partition from blocking a valid disk, while a
    second known data partition always blocks it."""
    if not fstype or "bios" in fstype.lower():
        return None
    if fstype in ESP_FSTYPES:
        return "esp"
    if fstype == "btrfs":
        return "btrfs"
    if fstype == "crypto_LUKS":
        return "luks"
    if fstype in DATA_FSTYPES:
        return "data"
    return None


def partition_children(node: dict) -> list:
    """Direct partitions of an lsblk tree node.

    Only 'part' children count: a disk's tree may also carry
    device-mapper or other nodes, and those are not partitions of this
    disk -- they must never take part in a layout decision."""
    return [
        child
        for child in (node.get("children") or [])
        if child.get("type") in ("part", None)
    ]


def disk_partitions(disk_name: str) -> list | None:
    """The partitions of a disk ('sda' or '/dev/sda'), read-only.

    None when the disk cannot be found in the lsblk tree."""
    wanted = (disk_name or "").split("/")[-1]
    for node in lsblk_tree():
        if node.get("name") == wanted and node.get("type") == "disk":
            return partition_children(node)
    return None


def validate_target_layout(disk: str, esp_part: str, root_part: str) -> None:
    """The endorsed target layout, proven before anything is planned.

    Exactly one FAT/ESP partition and exactly one btrfs partition, and no
    other partition that holds data. Partitions with a system/non-data
    role (UEFI, BL, swap and friends) or an unknown type are ignored. The
    mounted ESP and root must also be *those* two partitions -- this is
    what stops the live phase from pruning an ESP that is not the one on
    the target disk."""

    partitions = disk_partitions(disk)
    if partitions is None:
        die(f"Cannot enumerate the partitions of disk {disk}.")
    esp_parts = []
    btrfs_parts = []
    extra = []
    for child in partitions:
        role = partition_role(child.get("fstype"))
        name = "/dev/" + child["name"]
        if role == "esp":
            esp_parts.append(name)
        elif role == "btrfs":
            btrfs_parts.append(name)
        elif role in ("data", "luks"):
            extra.append(f"{name} ({child.get('fstype')})")

    if len(esp_parts) != 1:
        die(
            f"disk {disk} has {len(esp_parts)} FAT/ESP partition(s) "
            f"({', '.join(esp_parts) or 'none'}); expected exactly one."
        )
    if os.path.realpath(esp_parts[0]) != os.path.realpath(esp_part):
        die(
            f"the mounted ESP {esp_part} is not the only ESP partition "
            f"({esp_parts[0]}) of {disk}; the layout is ambiguous."
        )
    if len(btrfs_parts) != 1:
        die(
            f"disk {disk} has {len(btrfs_parts)} btrfs partition(s) "
            f"({', '.join(btrfs_parts) or 'none'}); expected exactly one."
        )
    if os.path.realpath(btrfs_parts[0]) != os.path.realpath(root_part):
        die(
            f"the root partition {root_part} is not the only btrfs partition "
            f"({btrfs_parts[0]}) of {disk}; the layout is ambiguous."
        )
    if extra:
        die(
            f"disk {disk} has extra data partition(s): "
            + ", ".join(extra)
            + "; the endorsed layout is exactly one ESP and one btrfs root."
        )


def require_single_device_btrfs(device: str, expect: str | None, what: str) -> None:
    """Abort unless the btrfs filesystem on DEVICE is single-device.

    The conversion rewrites one partition in place, so a multi-device
    filesystem would be edited through a device this tool never sees;
    expect, when given, is the partition the one device must resolve to.
    The inspection is a read-only 'btrfs filesystem show' -- nothing is
    mounted, unlocked or changed by it."""
    result = run_command(["btrfs", "filesystem", "show", device], capture_output=True)
    output = (result.stdout or "") + (result.stderr or "")
    totals = [int(total) for total in re.findall(r"Total devices\s+(\d+)", output)]
    if result.returncode != 0 or not totals:
        lines = output.strip().splitlines()
        die(
            f"cannot inspect the {what} on {device}"
            + (": " + lines[-1] if lines else "")
            + "; refusing to continue."
        )
    if len(totals) > 1:
        die(f"more than one btrfs filesystem found on {device}; refusing to guess.")
    if totals[0] != 1:
        paths = re.findall(r"\bpath\s+(\S+)", output)
        die(
            f"the {what} on {device} is a multi-device btrfs "
            f"(btrfs reports {totals[0]} devices"
            + (": " + ", ".join(paths) if paths else "")
            + "); only a single-device filesystem whose only device is the "
            "target root partition can be converted safely."
        )
    if expect:
        expect_real = os.path.realpath(expect)
        paths = re.findall(r"\bpath\s+(\S+)", output)
        if len(paths) != 1:
            die(
                f"cannot parse exactly one device path from 'btrfs filesystem "
                f"show {device}' (found {len(paths)} path(s)"
                + (": " + ", ".join(paths) if paths else "")
                + f"); expected only {expect}; refusing to continue."
            )
        if os.path.realpath(paths[0]) != expect_real:
            die(
                f"the {what} on {device} reports {paths[0]} as its device; "
                f"expected only {expect}."
            )


def require_target_offline(ctx) -> None:
    """Refuse to convert a target disk that is still in use.

    Before this run mounts, unlocks, shrinks or reencrypts anything,
    every device of the target has to be silent: no partition mounted
    anywhere (an automounted ESP included), no active swap, no open
    mapping/holder, and no other mount of the target's filesystem. Every
    conflict is named so it can be undone.

    prepare() deliberately does not run this check: there the installed
    system's own root is mounted by definition. It matters here, where
    the conversion happens."""
    devices = ctx.get("target_devices") or []
    resolved = {}
    by_devno = {}
    for device in devices:
        try:
            info = os.stat(device)
        except FileNotFoundError:
            die(
                f"target device {device} is missing (it was found by lsblk "
                "moments ago); refusing to continue."
            )
        except OSError as error:
            die(
                f"cannot inspect target device {device} ({error}); "
                "cannot prove it is offline; refusing to continue."
            )
        resolved[os.path.realpath(device)] = device
        by_devno[info.st_rdev] = device
    uuids = ctx.get("target_uuids") or {}

    def match(path: str | None) -> str | None:
        """The target device this source names, if it is one of ours."""
        if not path:
            return None
        tag = re.match(r"^(UUID|LABEL|PARTUUID|PARTLABEL)=(.*)$", path)
        if tag:
            return uuids.get((tag.group(2) or "").lower())
        real = os.path.realpath(path)
        if real in resolved:
            return resolved[real]
        try:
            devno = os.stat(path).st_rdev
        except OSError:
            return None
        return by_devno.get(devno) if devno else None

    problems = []

    # every mount whose source is one of the target's own devices
    result = run_command(
        ["findmnt", "-e", "-rn", "-o", "SOURCE,TARGET"], capture_output=True
    )
    if result.returncode != 0:
        # fail closed: an unreadable mount table means the offline state
        # cannot be proven
        die("cannot read the mount table with findmnt; refusing to continue.")
    for line in (result.stdout or "").splitlines():
        fields = line.split(None, 1)
        if len(fields) != 2:
            continue
        source, target = fields
        # a btrfs subvolume mount appends the subvol: /dev/sda2[/@]
        device = match(source.split("[", 1)[0])
        if device:
            target = target.replace("\\x20", " ").replace("\\040", " ")
            problems.append(f"{device} is mounted at {target}")

    # active swap on any of them
    try:
        with open("/proc/swaps", "r", encoding="utf-8") as handle:
            swap_lines = handle.read().splitlines()[1:]
    except OSError as error:
        die(
            f"cannot read /proc/swaps ({error}); cannot prove the target is "
            "not used as swap; refusing to continue."
        )
    for line in swap_lines:
        fields = line.split()
        if not fields:
            continue
        device = match(fields[0])
        if device:
            problems.append(f"{fields[0]} is active swap for {device}")

    # open mappings/holders (LUKS, LVM, mdraid, ...) on any of them
    for device in devices:
        holders_dir = "/sys/class/block/" + os.path.basename(device) + "/holders"
        try:
            holders = sorted(os.listdir(holders_dir))
        except FileNotFoundError:
            die(
                f"cannot read {holders_dir} (no holders entry for "
                f"{device}); cannot prove it has no active mapping; "
                "refusing to continue."
            )
        except OSError as error:
            die(
                f"cannot read {holders_dir} ({error}); cannot prove "
                f"{device} has no active mapping/holder; refusing to continue."
            )
        for holder in holders:
            problems.append(f"{device} has an active mapping/holder: {holder}")

    if problems:
        print("Target is not offline; refusing to touch it:")
        for problem in problems:
            print("  - " + problem)
        die(
            f"{ctx['disk']} is still in use; unmount, swapoff or close the "
            "conflicting device(s) listed above and re-run this tool."
        )


def grub_target_for_machine() -> tuple[str, str]:
    machine = platform.machine()
    if machine == "aarch64":
        return "arm64-efi", "BOOTAA64.EFI"
    if machine == "x86_64":
        return "x86_64-efi", "BOOTX64.EFI"
    die(f"unsupported machine architecture: {machine}")
    raise AssertionError("unreachable: die() exits the process")


def read_handoff(root_mnt: str) -> dict:
    path = root_mnt + HANDOFF_FILE
    if not os.path.isfile(path):
        die(
            "handoff file not found at "
            + path
            + "\nRun 'beryllium-fde prepare' on the installed system first."
        )
    try:
        with open(path, "r", encoding="utf-8") as handle:
            handoff = json.load(handle)
    except (OSError, ValueError) as error:
        die(f"cannot read handoff file {path}: {error}")
    validate_handoff(handoff, path)
    return handoff


def validate_handoff(handoff: dict, path: str) -> None:
    """Reject any handoff that does not match the written schema exactly.

    The handoff drives destructive stages; a partial, corrupted or
    hand-edited file must abort the run instead of letting later stages
    trust values that were never validated. Every required key has to be
    present with the exact type and non-empty content it was written
    with; unknown keys are tolerated (forward compatibility), but the
    values that steer the run are checked hard."""
    if not isinstance(handoff, dict):
        die(f"handoff file {path} is not a JSON object; refusing to use it.")
    if handoff.get("tool") != "beryllium-fde":
        die(
            f"handoff file {path} was not written by this tool "
            f"(tool={handoff.get('tool')!r}); refusing to use it."
        )

    def bad(key: str, expected: str) -> None:
        die(
            f"handoff file {path}: key {key!r} is invalid "
            f"(expected {expected}, got {handoff.get(key)!r}); "
            "refusing to use a corrupted handoff."
        )

    for key in (
        "disk",
        "ptuuid",
        "part",
        "esp_part",
        "esp_uuid",
        "btrfs_uuid",
        "grub_target",
        "boot_name",
    ):
        value = handoff.get(key)
        if not isinstance(value, str) or not value.strip():
            bad(key, "a non-empty string")
    if "disk_serial" in handoff and not isinstance(handoff["disk_serial"], str):
        bad("disk_serial", "a string")
    for key in ("luks_uuid",):
        if key in handoff and handoff[key] is not None:
            value = handoff[key]
            if not isinstance(value, str) or not value.strip():
                bad(key, "a non-empty string")
    stage = handoff.get("stage_completed")
    if not isinstance(stage, int) or isinstance(stage, bool):
        bad("stage_completed", "an integer")
    if stage is None or stage < -1 or stage > ST_DONE:
        die(
            f"handoff file {path}: stage_completed={stage} is outside the "
            f"valid range -1..{ST_DONE}; refusing to use a corrupted handoff."
        )
    phase = handoff.get("phase")
    if phase not in ("prepared", "encrypted"):
        die(
            f"handoff file {path}: phase={phase!r} is neither 'prepared' "
            "nor 'encrypted'; refusing to use a corrupted handoff."
        )


def write_handoff(root_mnt: str, handoff: dict) -> None:
    path = root_mnt + HANDOFF_FILE
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    # write to a temporary file in the same directory, flush it to disk and
    # rename it over the real file: an interruption can then never leave a
    # truncated or half-written handoff behind. The handoff carries no
    # secrets but stays root-only (0600).
    tmp_path = os.path.join(directory, ".handoff.json.tmp")
    try:
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(handoff, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
        dir_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def set_stage(ctx, stage: int) -> None:
    ctx["handoff"]["stage_completed"] = stage
    # a finished stage must survive a crash or power loss immediately, so
    # the handoff is written right away -- remounting the root first when
    # the stage left it unmounted (the encrypt stage does)
    persist_handoff(ctx)


def persist_handoff(ctx) -> None:
    if not os.path.ismount(ROOT_MNT):
        # mount_root picks the plain UUID source while the disk is not LUKS
        # yet and the mapper source afterwards, so unlock the mapper first
        # when a previous stage already converted the disk
        if ctx.get("mapper"):
            open_mapper(ctx, None)
        mount_root(ctx)
    write_handoff(ROOT_MNT, ctx["handoff"])


def mountinfo_targets() -> list[str] | None:
    """Every mountpoint the kernel currently reports, from /proc/self/mountinfo.

    Returns None when the mount table cannot be read at all -- callers
    must treat that as 'unknown', never as 'nothing is mounted', because
    a deletion or an unmount verified against an empty result would then
    act on missing information."""
    targets = []
    try:
        with open("/proc/self/mountinfo", "r", encoding="utf-8") as handle:
            for line in handle:
                fields = line.split()
                if len(fields) < 5:
                    continue
                target = fields[4]
                for code, char in (("\\040", " "), ("\\011", "\t"), ("\\134", "\\")):
                    target = target.replace(code, char)
                targets.append(target)
    except OSError:
        return None
    return targets


def mounted_under(prefix: str) -> list[str] | None:
    """Mountpoints at or below prefix, deepest first -- or None when the
    mount table is unreadable and nothing can be established."""
    targets = mountinfo_targets()
    if targets is None:
        return None
    prefix = os.path.abspath(prefix)
    hits = {
        target
        for target in targets
        if target == prefix or target.startswith(prefix + os.sep)
    }
    return sorted(hits, key=lambda point: point.count(os.sep), reverse=True)


def is_mounted(target: str) -> bool:
    target = os.path.abspath(target)
    targets = mountinfo_targets()
    if targets is None:
        # the mount table is unreadable: fall back to a stat-based check
        # for this one target rather than claiming it is unmounted
        return os.path.ismount(target)
    return target in targets


def umount_retry(target: str) -> None:
    """Unmount target, retrying until it is really gone.

    A slow SD card or a busy filesystem can fail umount for minutes;
    giving up is never an option here, because an unmount failure would
    let later cleanup skip work -- or delete a tree that is still mounted."""
    if not is_mounted(target):
        return
    attempt = 0
    while is_mounted(target):
        attempt += 1
        result = run_command(["umount", target], capture_output=True)
        if not is_mounted(target):
            return
        if attempt == 1 or attempt % 10 == 0:
            output = (result.stderr or "").strip() or (result.stdout or "").strip()
            detail = ": " + output.splitlines()[-1] if output else ""
            print(
                f"umount {target} still mounted (attempt {attempt}){detail}; "
                "waiting and retrying..."
            )
        time.sleep(RETRY_DELAY)


def mount_root(ctx) -> None:
    if os.path.ismount(ROOT_MNT):
        return
    os.makedirs(ROOT_MNT, exist_ok=True)
    if ctx["mapper"]:
        source = MAPPER_DEV
    else:
        source = "UUID=" + ctx["handoff"]["btrfs_uuid"]
    result = run_command(
        ["mount", "-o", "subvol=@", source, ROOT_MNT], capture_output=True
    )
    if result.returncode != 0:
        # fall back to a subvol-less mount, but the endorsed layout has @
        result2 = run_command(["mount", source, ROOT_MNT], capture_output=True)
        if result2.returncode != 0:
            if result.stderr:
                print(result.stderr.strip())
            die(f"cannot mount btrfs {source} at {ROOT_MNT}")
        print("Warning: mounted without subvol=@; endorsed layout expects it.")


def umount_root(ctx) -> None:
    umount_retry(ROOT_MNT)


def read_passfile() -> str | None:
    """Passphrase an earlier step left in PASSFILE, if any."""
    try:
        with open(PASSFILE, "r", encoding="utf-8") as handle:
            content = handle.read()
    except OSError:
        return None
    if content.endswith("\n"):
        # files written by older versions of this tool had a trailing
        # newline; strip it so they keep unlocking
        content = content[:-1]
    return content or None


def write_passfile(passphrase: str) -> None:
    """Persist the passphrase exactly as typed -- no trailing newline.

    Single source of truth for every passphrase write: cryptsetup's
    --key-file consumes the complete file, so a newline would become part
    of the key. The self-tests would then all pass (they read the same
    file) while GRUB, which sees only what the user typed, could never
    unlock the disk."""
    fd = os.open(PASSFILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
        handle.write(passphrase)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(PASSFILE, 0o600)


def drop_passfile() -> None:
    try:
        os.unlink(PASSFILE)
    except OSError:
        pass


def mapper_backing_device() -> str | None:
    """What the cryptroot mapping is actually backed by, read-only.

    Returns the underlying device path reported by 'cryptsetup status',
    None when there is no mapping at all, and '' when a mapping node
    exists but its backing device cannot be determined -- the last case
    is never collapsed into 'no mapping', so callers fail closed on it
    instead of trusting a device node they cannot prove is theirs."""
    if not os.path.exists(MAPPER_DEV):
        return None
    result = run_command(["cryptsetup", "status", MAPPER_NAME], capture_output=True)
    if result.returncode != 0:
        return ""
    match = re.search(r"^\s*device:\s+(\S+)", result.stdout or "", re.MULTILINE)
    return match.group(1) if match else ""


def mapper_is_ours(ctx, backing: str | None = None) -> bool:
    """True only when the existing mapping provably maps ctx['p2'].

    Pass the already-resolved backing device to avoid re-running
    'cryptsetup status'."""
    if backing is None:
        backing = mapper_backing_device()
    if not backing:
        return False
    return os.path.realpath(backing) == os.path.realpath(ctx["p2"])


def open_mapper(ctx, passphrase: str | None) -> None:
    # the marker in ctx only says the disk is LUKS now; the mapper itself
    # may still be closed (it is right after the encrypt stage), so check
    # the real device node before deciding there is nothing to do
    if os.path.exists(MAPPER_DEV):
        # an existing /dev/mapper/cryptroot is not necessarily ours: prove
        # it maps this run's target partition before reusing it, so a
        # foreign or unidentifiable mapping is never used
        backing = mapper_backing_device()
        if not mapper_is_ours(ctx, backing):
            detail = (
                f"it maps {backing}"
                if backing
                else "its backing device cannot be determined"
            )
            die(
                f"{MAPPER_DEV} already exists but is not backed by "
                f"{ctx['p2']} ({detail}); refusing to use a mapping this "
                "tool does not own."
            )
        ctx["mapper"] = MAPPER_NAME
        return
    for attempt in range(3):
        if passphrase is None:
            passphrase = read_passfile()
        if passphrase is None:
            passphrase = ask_passphrase("LUKS passphrase: ")
        write_passfile(passphrase)
        result = run_command(
            ["cryptsetup", "open", "--key-file", PASSFILE, ctx["p2"], MAPPER_NAME],
            capture_output=True,
        )
        if result.returncode == 0:
            ctx["mapper"] = MAPPER_NAME
            return
        print("Wrong passphrase or open failed, try again.")
        passphrase = None
        # forget the rejected passphrase so the next attempt prompts again
        drop_passfile()
    die("could not open the LUKS container after 3 attempts")


def close_mapper(ctx) -> None:
    """Close the mapping this run opened, after proving it is ours.

    Only a mapping proven to be backed by ctx['p2'] is ever closed; a
    foreign or unidentifiable one aborts the run instead of being torn
    down, and a marker left behind by a container that is no longer
    mapped simply clears. On a real close failure ctx['mapper'] stays
    set, so cleanup() can retry."""
    if not ctx.get("mapper"):
        return
    backing = mapper_backing_device()
    if backing is None:
        # nothing mapped any more; there is nothing to close
        ctx["mapper"] = None
        return
    if not mapper_is_ours(ctx, backing):
        detail = (
            f"it maps {backing}"
            if backing
            else "its backing device cannot be determined"
        )
        die(
            f"refusing to close {MAPPER_NAME}: {detail}, not {ctx['p2']}; "
            "this mapping does not belong to this run."
        )
    result = run_command(["cryptsetup", "close", MAPPER_NAME], capture_output=True)
    if result.returncode != 0:
        output = (result.stderr or "").strip() or (result.stdout or "").strip()
        detail = ": " + output.splitlines()[-1] if output else ""
        die(f"could not close {MAPPER_NAME}{detail}; refusing to continue.")
    ctx["mapper"] = None


# ---------------------------------------------------------------- prepare ---


def check_no_existing_rd_luks() -> None:
    """Refuse to prepare when the GRUB config already carries rd.luks.

    This tool writes its own rd.luks.name=<uuid>=cryptroot parameter in
    the config stage; an rd.luks token left over from an earlier or
    foreign setup would survive the edit and be resurrected by
    grub-mkconfig alongside the new one, breaking the boot handover.
    Read-only scan of the two files grub-mkconfig reads, ignoring blank
    and comment lines; these files are never modified here."""
    token = re.compile(r"\brd\.luks\b")
    offending = []
    for path in ("/etc/default/grub", "/boot/grub/grub.cfg"):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                lines = handle.read().splitlines()
        except OSError:
            continue
        for lineno, line in enumerate(lines, start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if token.search(stripped):
                offending.append(f"{path}:{lineno}")
    if offending:
        die(
            "rd.luks kernel parameter already present in the GRUB "
            "configuration (" + ", ".join(offending) + "); remove it and "
            "regenerate grub.cfg before running prepare again -- this "
            "check does not modify the configuration."
        )


def prepare(args: argparse.Namespace) -> None:
    require_root()

    # The strict offline-target gate is deliberately NOT run here: on the
    # installed system the very root being checked is mounted by
    # definition. live() enforces it where the conversion happens.

    result = run_required(["findmnt", "-no", "SOURCE", "/"], capture_output=True)
    source = result.stdout.strip()
    # btrfs reports /dev/node[/@subvol]
    part = source.split("[", 1)[0]
    if not part.startswith("/dev/"):
        die(
            f"System not on a plain block device (found {source}) "
            "Not an installed system?"
        )

    if part.startswith("/dev/mapper/"):
        die("The system appears to be already encrypted. Cannot continue.")

    fstype = device_fstype(part)
    if fstype == "crypto_LUKS":
        die("System partition is already LUKS encrypted; Nothing to do.")
    if fstype != "btrfs":
        die(
            f"System filesystem is {fstype}, expected btrfs. Cannot operate on other filesystems."
        )

    disk = lsblk_value(part, "PKNAME")
    if not disk:
        die(f"Cannot determine the parent disk of {part}.")
    disk = "/dev/" + disk

    esp_source = run_command(
        ["findmnt", "-no", "SOURCE", "/boot/efi"], capture_output=True
    )
    if esp_source.returncode != 0:
        die("/boot/efi is not mounted; expected a vfat ESP there.")
    esp_part = esp_source.stdout.strip().split("[", 1)[0]
    if device_fstype(esp_part) not in ("vfat", "fat32"):
        die(f"ESP {esp_part} is not a FAT filesystem.")

    # The endorsed target layout, proven before anything is planned:
    # exactly one FAT/ESP and exactly one btrfs on this disk, no other
    # data partition, and the mounted ESP/root really are those two --
    # otherwise the live phase could prune the wrong ESP.
    validate_target_layout(disk, esp_part, part)

    target, boot_name = grub_target_for_machine()
    for module in ("cryptodisk.mod", "luks2.mod", "pbkdf2.mod"):
        if not os.path.isfile(f"/usr/lib/grub/{target}/{module}"):
            die(
                f"grub module {module} missing from /usr/lib/grub/{target}/; "
                "The installed grub build cannot unlock LUKS."
            )

    require_bins("installed system")

    # do_chroot() runs 'dracut-rebuild' inside this target, so the
    # installed system has to ship it; it is checked here, not in
    # REQUIRED_BINS, because the live ISO itself need not provide it
    if shutil.which("dracut-rebuild") is None:
        die(
            "missing required command on the installed system: dracut-rebuild "
            "(the initramfs rebuild during conversion runs it in the chroot)."
            " A Beryllium OS dracut is REQUIRED."
        )

    # A multi-device btrfs would be edited through a device this tool
    # never sees; prove (read-only) that the root filesystem is
    # single-device and that its only device is the root partition.
    require_single_device_btrfs(part, part, "root btrfs")

    # an rd.luks parameter already present in the installed GRUB config
    # would be duplicated by the conversion; refuse before anything is
    # planned (read-only check on the installed system)
    check_no_existing_rd_luks()

    usage = run_required(
        ["btrfs", "filesystem", "usage", "-b", "/"], capture_output=True
    ).stdout
    candidates = []
    match = re.search(r"Free \(estimated\):\s+(\d+)", usage)
    if match:
        candidates.append(int(match.group(1)))
    match = re.search(r"Unallocated:\s+(\d+)", usage)
    if match:
        candidates.append(int(match.group(1)))
    if not candidates:
        die(
            "Could not parse 'btrfs filesystem usage' output; "
            "Cannot verify shrink headroom."
        )
    free = min(candidates)
    if free < MIN_FREE_BYTES:
        die(
            f"Only {human_size(free)} free/unallocated on root; "
            f"Need at least {human_size(MIN_FREE_BYTES)} to vacate the "
            f"{REDUCE_SIZE} tail safely."
        )

    subvol_list = run_required(
        ["btrfs", "subvolume", "list", "/"], capture_output=True
    ).stdout
    subvols = [line.split()[-1] for line in subvol_list.splitlines() if line.strip()]
    if "@" not in subvols:
        die("btrfs subvolume '@' not found; not the endorsed Beryllium layout.")
    missing_subvols = [
        s
        for s in ("@home", "@log", "@cache", "@pkg", "@swap", "@.snapshots")
        if s not in subvols
    ]
    if missing_subvols:
        print("Warning: missing expected subvolumes: " + ", ".join(missing_subvols))

    if not args.i_have_a_backup:
        print("\nWARNING: encryption converts the entire partition in place.")
        print("A full disk backup is mandatory; a failure mid-conversion WILL")
        print("DESTROY the filesystem beyond recovery.")
        print()
        print("A FULL image backup is recommended.")
        if not ask_yes_no(
            "Do you have a verified full backup of this system?", default=False
        ):
            die("No backup acknowledged; prepare aborted.")
    else:
        print("Backup acknowledgement given via --i-have-a-backup.")

    serial = lsblk_value(disk, "SERIAL") or ""
    ptuuid = lsblk_value(disk, "PTUUID")
    if not ptuuid:
        die(f"Disk {disk} has no partition table UUID (PTUUID); aborting.")

    btrfs_uuid = lsblk_value(part, "UUID")
    esp_uuid = lsblk_value(esp_part, "UUID")

    handoff = {
        "tool": "beryllium-fde",
        "phase": "prepared",
        "stage_completed": -1,
        "disk": disk,
        "disk_serial": serial,
        "ptuuid": ptuuid,
        "part": part,
        "esp_part": esp_part,
        "esp_uuid": esp_uuid,
        "btrfs_uuid": btrfs_uuid,
        "grub_target": target,
        "boot_name": boot_name,
        "created": os.popen("date -Is").read().strip(),
    }
    write_handoff("/", handoff)

    print("\nPreparation complete. The system passed every safety check.")
    print(f"Handoff written to {HANDOFF_FILE}")
    print("\nNext steps:")
    print("  1. If you haven't already, BACKUP THIS MACHINE FULLY.")
    print("  2. Boot the latest Beryllium OS ISO on this machine.")
    print("  3. Do not continue with the Bakery install.")
    print("  4. Run 'beryllium-fde live' from the live environment.")
    print("     It will locate this disk via the handoff file and resume this process.")
    print("  5. After it finishes and you reboot, only GRUB will ask for a")
    print("     passphrase; the system decrypts itself afterwards.")


# ------------------------------------------------------------------ live ----


def find_live_disk(tree: list) -> set[str]:
    live_sources = []
    for point in ("/run/miso", "/"):
        result = run_command(["findmnt", "-no", "SOURCE", point], capture_output=True)
        if result.returncode == 0 and result.stdout.strip():
            live_sources.append(result.stdout.strip().split("[", 1)[0])

    def disk_of_node(name: str) -> str | None:
        for disk in tree:
            if disk.get("name") == name and disk.get("type") == "disk":
                return disk["name"]
            for child in disk.get("children", []) or []:
                if child.get("name") == name:
                    return disk["name"]
        return None

    live_disks = set()
    for source in live_sources:
        name = os.path.basename(source)
        disk = disk_of_node(name)
        if disk:
            live_disks.add(disk)
    return live_disks


def select_disk(tree: list, live_disks: set[str]) -> dict:
    disks = [d for d in tree if d.get("type") == "disk" and d["name"] not in live_disks]
    if not disks:
        die("no candidate disks found (only the live medium is present?).")

    print("Disks available for encryption:\n")
    for index, disk in enumerate(disks, start=1):
        serial = disk.get("serial") or "-"
        model = (disk.get("model") or "-").strip()
        size = human_size(disk.get("size") or 0)
        print(f"  {index}) {disk['name']}  {size:>9}  {model}  (serial {serial})")

    while True:
        answer = ask(
            f"\nSelect the system disk for encryption [1-{len(disks)}]: "
        ).strip()
        if answer.isdigit() and 1 <= int(answer) <= len(disks):
            return disks[int(answer) - 1]
        print("Invalid selection.")


def validate_layout(
    disk: dict,
) -> tuple[dict, dict]:
    """Resolve exactly one ESP and exactly one root on the target disk.

    The live phase prunes the ESP down to a single EFI binary, so the ESP
    choice must be unambiguous: exactly one FAT partition, exactly one
    btrfs root -- or the single LUKS container of an interrupted
    conversion -- and no other partition that holds data. Partitions with
    a system/non-data role (BIOS boot, swap and friends) or an unknown
    type stay out of the decision entirely."""
    children = partition_children(disk)
    if len(children) < 2:
        die(
            f"disk {disk['name']} has {len(children)} partition(s); "
            "expected at least 2 (ESP + root)."
        )
    children = sorted(children, key=lambda c: c["name"])

    esp_parts = []
    btrfs_parts = []
    luks_parts = []
    extra = []
    for child in children:
        role = partition_role(child.get("fstype"))
        if role == "esp":
            esp_parts.append(child)
        elif role == "btrfs":
            btrfs_parts.append(child)
        elif role == "luks":
            luks_parts.append(child)
        elif role == "data":
            extra.append(f"{child['name']} ({child.get('fstype')})")

    if not esp_parts:
        die(f"no vfat ESP partition found on {disk['name']}.")
    if len(esp_parts) > 1:
        names = ", ".join(child["name"] for child in esp_parts)
        die(
            f"{len(esp_parts)} FAT/ESP partitions on {disk['name']} ({names}); "
            "the ESP would be ambiguous, refusing to choose one to prune."
        )
    if extra:
        die(
            f"disk {disk['name']} has extra data partition(s): "
            + ", ".join(extra)
            + "; the endorsed layout is exactly one ESP and one btrfs root."
        )
    if len(btrfs_parts) > 1:
        names = ", ".join(child["name"] for child in btrfs_parts)
        die(
            f"{len(btrfs_parts)} btrfs partitions on {disk['name']} ({names}); "
            "refusing to choose one as the root."
        )
    if len(luks_parts) > 1:
        names = ", ".join(child["name"] for child in luks_parts)
        die(
            f"{len(luks_parts)} LUKS partitions on {disk['name']} ({names}); "
            "refusing to choose one as the root."
        )
    if btrfs_parts and luks_parts:
        die(
            f"disk {disk['name']} holds both a btrfs partition "
            f"({btrfs_parts[0]['name']}) and a LUKS container "
            f"({luks_parts[0]['name']}); refusing to choose the root."
        )
    if btrfs_parts:
        return esp_parts[0], btrfs_parts[0]
    if luks_parts:
        return esp_parts[0], luks_parts[0]
    die(
        f"no plain btrfs root partition found on {disk['name']} "
        "(and no LUKS container either)."
    )
    raise AssertionError("unreachable: die() exits the process")


def do_tail_room(ctx) -> None:
    mount_root(ctx)
    print(f"Shrinking btrfs by {TAIL_SHRINK} to vacate the device tail...")
    result = run_command(
        ["btrfs", "filesystem", "resize", "-" + TAIL_SHRINK, ROOT_MNT],
        capture_output=True,
    )
    if result.returncode != 0:
        if result.stderr:
            print(result.stderr.strip())
        umount_root(ctx)
        die(
            "could not shrink the filesystem. Try balancing first "
            "(btrfs balance start <mount>) or free up space, then re-run "
            "this tool; nothing else was changed."
        )
    umount_root(ctx)


# Tri-state result for reencrypt_pending(): only a successful metadata
# read proves the device is clear; anything else is "unknown" and must be
# treated as possibly pending (fail-closed) by every resume loop.
REENCRYPT_CLEAR = "clear"
REENCRYPT_PENDING = "pending"
REENCRYPT_UNKNOWN = "unknown"


def reencrypt_pending(device: str) -> str:
    """One of REENCRYPT_CLEAR, REENCRYPT_PENDING or REENCRYPT_UNKNOWN:
    whether the LUKS2 metadata records an initialized but unfinished
    reencryption, and whether that could be determined at all.

    Only a successful luksDump plus successfully parsed metadata with no
    reencrypt requirement flags and no 'in-reencryption' segment flags
    reports REENCRYPT_CLEAR. Any command failure, missing JSON or parse
    error reports REENCRYPT_UNKNOWN, which keeps every resume loop below
    from treating an unreadable device as 'nothing to resume' and
    declaring a possibly unfinished conversion done.

    cryptsetup records the pending operation as a requirement flag
    (offline-reencrypt / online-reencrypt[-v2|-v3]) in the config section,
    plus an 'in-reencryption' flag on the segments; both are wiped once the
    conversion completes."""
    result = run_command(
        ["cryptsetup", "luksDump", "--dump-json-metadata", device],
        capture_output=True,
    )
    if result.returncode != 0:
        return REENCRYPT_UNKNOWN
    output = result.stdout or ""
    start = output.find("{")
    end = output.rfind("}")
    if start < 0 or end <= start:
        return REENCRYPT_UNKNOWN
    try:
        metadata = json.loads(output[start : end + 1])
    except ValueError:
        return REENCRYPT_UNKNOWN

    def strings_of(node: object) -> list[str]:
        if isinstance(node, str):
            return [node]
        if isinstance(node, list):
            return [item for entry in node for item in strings_of(entry)]
        if isinstance(node, dict):
            return [item for entry in node.values() for item in strings_of(entry)]
        return []

    config = metadata.get("config")
    requirements = config.get("requirements") if isinstance(config, dict) else None
    if requirements is not None and any(
        "reencrypt" in name for name in strings_of(requirements)
    ):
        return REENCRYPT_PENDING
    segments = metadata.get("segments")
    if isinstance(segments, dict):
        for segment in segments.values():
            flags = segment.get("flags") if isinstance(segment, dict) else None
            if flags and "in-reencryption" in flags:
                return REENCRYPT_PENDING
    return REENCRYPT_CLEAR


def ensure_passfile(device: str) -> None:
    """Make sure PASSFILE holds a passphrase that unlocks DEVICE.

    Reuses the file an earlier step already wrote when it still works, so
    nobody is prompted twice, and validates it with --test-passphrase so a
    wrong passphrase is retried instead of feeding a resume loop that can
    never succeed."""
    for _ in range(3):
        if read_passfile() is None:
            write_passfile(ask_passphrase("LUKS passphrase: "))
        result = run_command(
            ["cryptsetup", "open", "--test-passphrase", "--key-file", PASSFILE, device],
            capture_output=True,
        )
        if result.returncode == 0:
            return
        print("Wrong passphrase or unlock failed, try again.")
        drop_passfile()
    die("could not unlock the LUKS container after 3 attempts")


def resume_reencrypt(device: str) -> bool:
    """Force an interrupted in-place reencryption to run to completion.

    Returns True when the loop ended with metadata proving the reencryption
    finished (or when recovery was attempted on an unreadable state), and
    False when a successful metadata read proves there is nothing to
    resume. An unreadable state is never treated as 'nothing to resume':
    the loop keeps retrying until a successful metadata read settles it."""
    state = reencrypt_pending(device)
    if state == REENCRYPT_CLEAR:
        return False
    if state == REENCRYPT_PENDING:
        print(f"Unfinished reencryption detected on {device}; resuming it now.")
    else:
        print(
            f"Could not read the reencryption state of {device}; "
            "treating it as unfinished and recovering."
        )
    ensure_passfile(device)
    attempt = 0
    while True:
        state = reencrypt_pending(device)
        if state == REENCRYPT_CLEAR:
            break
        attempt += 1
        result = None
        if state == REENCRYPT_PENDING:
            result = run_command(
                [
                    "cryptsetup",
                    "reencrypt",
                    "--resume-only",
                    "--key-file",
                    PASSFILE,
                    device,
                ],
                capture_output=True,
            )
            if result.returncode == 0:
                continue
        if attempt == 1 or attempt % 10 == 0:
            if state == REENCRYPT_PENDING and result is not None:
                output = (result.stderr or "").strip() or (result.stdout or "").strip()
                if output:
                    print(output.splitlines()[-1])
                # a hard cut (crash, power loss) can leave the reencryption
                # journal needing recovery before it can carry on
                print(
                    f"Reencryption still unfinished (attempt {attempt}); "
                    "trying cryptsetup repair, then waiting and retrying..."
                )
            else:
                print(
                    f"Reencryption state still unreadable (attempt {attempt}); "
                    "trying cryptsetup repair, then waiting and retrying..."
                )
        run_command(
            ["cryptsetup", "repair", "--key-file", PASSFILE, device],
            capture_output=True,
        )
        time.sleep(RETRY_DELAY)
    print("Reencryption finished.")
    return True


def do_encrypt(ctx) -> None:
    umount_root(ctx)
    # State can go stale between the initial gate and here (an automount,
    # a stray swap or mapping could have appeared while earlier stages
    # ran): re-verify the target is still completely offline at the start
    # of the stage that rewrites the partition.
    require_target_offline(ctx)
    print(f"\nEncrypting {ctx['p2']} in place (LUKS2, pbkdf2 keyslot for GRUB).")
    print("This moves every byte of the partition and takes a while.")
    print("It can be interrupted; this tool resumes it automatically.")

    while True:
        passphrase = ask_passphrase("New LUKS passphrase (this is what GRUB asks): ")
        if not passphrase:
            # the only rejection: anything non-empty that cryptsetup
            # accepts is a valid passphrase -- no minimum length
            print("Passphrase must not be empty.")
            continue
        if len(passphrase) < 8:
            # non-blocking hint only
            print("Warning: short passphrase; it is the only secret GRUB checks.")
        repeat = ask_passphrase("Repeat passphrase: ")
        if passphrase == repeat:
            break
        print("Passphrases do not match, try again.")

    write_passfile(passphrase)

    cmd = [
        "cryptsetup",
        "reencrypt",
        "--encrypt",
        "--type",
        "luks2",
        "--pbkdf",
        "pbkdf2",
        "--reduce-device-size",
        REDUCE_SIZE,
        "--new-keyfile",
        PASSFILE,
        ctx["p2"],
    ]
    print("Running: " + " ".join(cmd))
    interactive_used = False
    interrupted = False
    try:
        failed = run_command(cmd).returncode != 0
    except KeyboardInterrupt:
        interrupted = True
        failed = True
        print("\nReencryption interrupted; resuming it to completion instead.")

    if failed and not interrupted:
        # older cryptsetup builds may not know --new-keyfile; fall back to
        # letting cryptsetup prompt for the passphrase itself
        interactive_used = True
        print("Falling back to cryptsetup's own passphrase prompt...")
        interactive = [
            "cryptsetup",
            "reencrypt",
            "--encrypt",
            "--type",
            "luks2",
            "--pbkdf",
            "pbkdf2",
            "--reduce-device-size",
            REDUCE_SIZE,
            ctx["p2"],
        ]
        try:
            failed = run_command(interactive).returncode != 0
        except KeyboardInterrupt:
            interrupted = True
            failed = True
            print("\nReencryption interrupted; resuming it to completion instead.")

    if failed:
        # the conversion died half way through: force the recorded
        # reencryption to run to completion now instead of printing the
        # manual 'cryptsetup reencrypt --resume' instruction
        if not resume_reencrypt(ctx["p2"]):
            die(
                "reencrypt failed and the LUKS metadata records no interrupted "
                "reencryption to resume; nothing else was changed. Re-run this "
                "tool to start the conversion again."
            )
        # resume validated PASSFILE against the container, so the remaining
        # stages can keep using the passphrase file as it is
    elif interactive_used:
        # interactive path: passphrase known only to cryptsetup; keep a copy
        # for later stages by asking again (still rejecting only empties)
        while True:
            passphrase = ask_passphrase(
                "Re-enter the passphrase (needed for the remaining stages): "
            )
            if passphrase:
                break
            print("Passphrase must not be empty.")
        write_passfile(passphrase)

    luks_uuid = run_required(
        ["cryptsetup", "luksUUID", ctx["p2"]], capture_output=True
    ).stdout.strip()
    ctx["handoff"]["luks_uuid"] = luks_uuid
    ctx["mapper"] = MAPPER_NAME  # marker: device is now LUKS
    print(f"Encryption complete. LUKS UUID: {luks_uuid}")


def do_reclaim(ctx) -> None:
    open_mapper(ctx, None)
    mount_root(ctx)
    print("Growing the filesystem back over the header area...")
    run_required(["btrfs", "filesystem", "resize", "1:max", ROOT_MNT])


def slot_present(device: str, slot: int) -> bool:
    """True when LUKS metadata lists SLOT as an active keyslot.

    Read-only: parses the JSON metadata from 'cryptsetup luksDump'."""
    result = run_command(
        ["cryptsetup", "luksDump", "--dump-json-metadata", device],
        capture_output=True,
    )
    if result.returncode != 0:
        return False
    output = result.stdout or ""
    start = output.find("{")
    end = output.rfind("}")
    if start < 0 or end <= start:
        return False
    try:
        metadata = json.loads(output[start : end + 1])
    except ValueError:
        return False
    return str(slot) in (metadata.get("keyslots") or {})


def luks_keyslots(device: str) -> list[str]:
    """The active keyslots of DEVICE as a sorted list of slot strings.

    Dies when the LUKS metadata cannot be read or parsed: an unreadable
    layout must never be modified."""
    result = run_command(
        ["cryptsetup", "luksDump", "--dump-json-metadata", device],
        capture_output=True,
    )
    if result.returncode != 0:
        output = (result.stderr or "").strip() or (result.stdout or "").strip()
        die(
            "cannot read the LUKS metadata of "
            + device
            + (" (" + output.splitlines()[-1] + ")" if output else "")
            + "; refusing to enroll a keyslot."
        )
    output = result.stdout or ""
    start = output.find("{")
    end = output.rfind("}")
    if start < 0 or end <= start:
        die(
            "cannot parse the LUKS metadata of "
            + device
            + "; refusing to enroll a keyslot."
        )
    try:
        metadata = json.loads(output[start : end + 1])
    except ValueError:
        die(
            "cannot parse the LUKS metadata of "
            + device
            + "; refusing to enroll a keyslot."
        )
    return sorted(metadata.get("keyslots") or {}, key=int)


def require_only_slot0(device: str) -> None:
    """Die unless DEVICE's LUKS2 metadata holds exactly one keyslot: slot 0.

    Hard precondition for adding a NEW slot 1: the endorsed container
    carries only the user passphrase in slot 0, so anything else means
    this run cannot add slot 1 safely. A container that already has
    slot 1 is the already-enrolled state and is handled by
    reconcile_keyfile() instead of being rejected here."""
    keyslots = luks_keyslots(device)
    if keyslots != ["0"]:
        die(
            f"{device} holds keyslot(s) "
            + (", ".join(keyslots) if keyslots else "none")
            + "; expected exactly one keyslot, slot 0, before a new "
            "keyfile slot can be enrolled."
        )


def require_no_unexpected_slots(device: str) -> None:
    """Die unless DEVICE's keyslots are exactly {0} or {0, 1}.

    Slot 0 must hold the user passphrase and slot 1 is this tool's
    keyfile slot; any other slot is an unexpected layout this run must
    never touch. A present slot 1 is the already-enrolled state, not an
    error -- it is reconciled against the keyfile copies, not rejected."""
    keyslots = luks_keyslots(device)
    if "0" not in keyslots:
        die(
            f"{device} has no slot 0 (keyslots: "
            + (", ".join(keyslots) if keyslots else "none")
            + "); the user passphrase slot must exist before a keyfile "
            "can be enrolled."
        )
    unexpected = [slot for slot in keyslots if slot not in ("0", "1")]
    if unexpected:
        die(
            f"{device} holds unexpected keyslot(s) "
            + ", ".join(unexpected)
            + "; only slots 0 (passphrase) and 1 (keyfile) are expected. "
            "Refusing to touch this layout."
        )


def test_key_slot(device: str, keyfile: str, slot: int) -> bool:
    """True when KEYFILE opens DEVICE's keyslot SLOT (read-only check)."""
    result = run_command(
        [
            "cryptsetup",
            "open",
            "--test-passphrase",
            "--key-slot",
            str(slot),
            "--key-file",
            keyfile,
            device,
        ],
        capture_output=True,
    )
    return result.returncode == 0


def enroll_key_slot(ctx, keyfile: str) -> None:
    """Make slot 1 hold exactly the 64 bytes of KEYFILE.

    Re-enrollment, not key generation: the new keyslot is added from the
    chosen candidate keyfile, authorized with slot 0's passphrase file.
    An existing but unusable slot 1 is killed first -- it cannot be kept,
    because nothing on disk is known to unlock it. Any keyslot other
    than 0 or 1 aborts instead of being overwritten."""
    require_no_unexpected_slots(ctx["p2"])
    ensure_passfile(ctx["p2"])
    if slot_present(ctx["p2"], 1):
        print("Slot 1 holds an unusable key; replacing it with the canonical keyfile.")
        run_required(
            [
                "cryptsetup",
                "luksKillSlot",
                ctx["p2"],
                "1",
                "--key-file",
                PASSFILE,
            ]
        )
    run_required(
        [
            "cryptsetup",
            "luksAddKey",
            "--key-slot",
            "1",
            ctx["p2"],
            keyfile,
            "--key-file",
            PASSFILE,
        ]
    )
    print("Slot 1 now holds the canonical keyfile (argon2id).")


def write_key_copy(path: str, blob: bytes) -> None:
    """Atomically write the 64-byte blob to PATH, mode 0600, durable.

    The bytes go to a temporary file in the same directory, are fsynced,
    renamed over PATH and the directory is fsynced too, so a crash or
    power loss can never leave a truncated or half-written keyfile
    behind -- the durable installed copy especially must survive."""
    directory = os.path.dirname(path) or "."
    tmp_path = os.path.join(directory, "." + os.path.basename(path) + ".tmp")
    try:
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(blob)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
        dir_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def read_key_copy(path: str) -> bytes | None:
    """The 64-byte key stored at PATH, or None when absent/wrong size."""
    try:
        with open(path, "rb") as handle:
            blob = handle.read()
    except OSError:
        return None
    if len(blob) != 64:
        return None
    return blob


def reconcile_keyfile(ctx) -> None:
    """Reconcile the live KEYFILE and the installed copy with LUKS slot 1.

    The live copy under /run and the durable copy inside the encrypted
    root may disagree (a restarted run, an interrupted stage). Neither is
    trusted blindly: when they differ, each candidate is tested against
    slot 1 with a read-only --test-passphrase and the one that actually
    unlocks the container becomes the canonical key, overwriting the
    other copy with it. When neither candidate works -- or the agreed
    key no longer opens slot 1 -- slot 1 is re-enrolled from the
    installed copy (preferred) or the live copy, authorized by slot 0's
    passphrase via ensure_passfile(). The key is never regenerated here.

    Invariant afterwards: slot 1 and the durable installed copy hold
    exactly the same 64 bytes."""
    mount_root(ctx)
    installed_path = ctx["root_mnt"] + KEYFILE_INSTALLED
    live_blob = read_key_copy(KEYFILE)
    installed_blob = read_key_copy(installed_path)
    if live_blob is None and installed_blob is None:
        return
    if live_blob is not None and live_blob == installed_blob:
        # the copies agree: they are the canonical key unless slot 1
        # disagrees, which is tested below just like a differing pair
        working = [(KEYFILE, live_blob)]
        print("Live and installed keyfiles are identical.")
    else:
        working = []
        for label, path, blob in (
            ("installed copy " + installed_path, installed_path, installed_blob),
            ("live copy " + KEYFILE, KEYFILE, live_blob),
        ):
            if blob is None:
                continue
            if test_key_slot(ctx["p2"], path, 1):
                working.append((path, blob))
                print(f"{label} opens LUKS slot 1.")
        if working:
            # the installed copy is checked first, so it wins whenever
            # both candidates would unlock the slot
            path, blob = working[0]
            print(f"Adopting {path} as the canonical keyfile.")
            if blob != live_blob:
                write_key_copy(KEYFILE, blob)
            if blob != installed_blob:
                write_key_copy(installed_path, blob)
            return
        if not working:
            if not slot_present(ctx["p2"], 1):
                # nothing is enrolled yet, so "neither works" just means
                # the enrollment stage has not run; keep the copies as
                # they are and let that stage enroll the live copy
                return
            # neither candidate unlocks an existing slot 1: re-enroll from the
            # installed copy when it exists, otherwise the live copy. The
            # durable installed copy is made correct BEFORE the slot is
            # re-enrolled, so the invariant never has to be repaired by
            # rewriting the durable copy after luksAddKey.
            if installed_blob is not None:
                canonical = installed_blob
                print("Neither keyfile opens slot 1; re-enrolling the installed copy.")
            elif live_blob is not None:
                canonical = live_blob
                print("Neither keyfile opens slot 1; re-enrolling the live copy.")
            else:
                # both copies are gone: nothing to re-enroll from
                return
            write_key_copy(KEYFILE, canonical)
            if read_key_copy(installed_path) != canonical:
                write_key_copy(installed_path, canonical)
            enroll_key_slot(ctx, installed_path)
            return

    # single agreed candidate: it must actually open slot 1
    path, blob = working[0]
    if test_key_slot(ctx["p2"], path, 1):
        return
    if not slot_present(ctx["p2"], 1):
        # nothing enrolled yet: the enrollment stage will add this key
        return
    print("The keyfile does not open slot 1; re-enrolling it.")
    enroll_key_slot(ctx, path)


def install_keyfile(ctx) -> str:
    """Install the 64-byte key into the encrypted root, mode 0600.

    Called while the target root is mounted, right after generation (and
    again before enrollment): the live copy under /run dies with cleanup()
    on every run, so this copy inside the encrypted root is the durable
    one that later runs recover. The durable copy is replaced only while
    slot 1 is absent; once slot 1 exists it is never 'refreshed' with
    different bytes -- a disagreement can only be resolved through
    reconcile_keyfile() against the container."""
    with open(KEYFILE, "rb") as handle:
        blob = handle.read()
    if len(blob) != 64:
        die(f"keyfile {KEYFILE} is {len(blob)} bytes, expected exactly 64.")
    mount_root(ctx)
    installed = ctx["root_mnt"] + KEYFILE_INSTALLED
    if read_key_copy(installed) == blob:
        # provably identical bytes: nothing to rewrite
        return installed
    if slot_present(ctx["p2"], 1):
        # slot 1 already holds the enrolled key: which copy is canonical
        # is decided against the container, never by overwriting the
        # durable copy with this run's live bytes
        reconcile_keyfile(ctx)
        if read_key_copy(installed) is None:
            die(f"installed keyfile {installed} missing after reconciliation.")
        return installed
    os.makedirs(os.path.dirname(installed), exist_ok=True)
    write_key_copy(installed, blob)
    return installed


def ensure_keyfile(ctx) -> str | None:
    """Make the live KEYFILE available for this run; return it or None.

    Prefers the /run copy. When cleanup() already deleted it (a restarted
    run resuming after the keygen stage), it is restored byte-for-byte
    from the copy installed into the encrypted root -- the key is never
    regenerated, because slot 1 must keep holding exactly these 64 bytes.
    Returns None when no keyfile has been generated yet."""
    if os.path.isfile(KEYFILE):
        with open(KEYFILE, "rb") as handle:
            if len(handle.read()) == 64:
                return KEYFILE
    mount_root(ctx)
    installed = ctx["root_mnt"] + KEYFILE_INSTALLED
    if not os.path.isfile(installed):
        return None
    with open(installed, "rb") as handle:
        blob = handle.read()
    if len(blob) != 64:
        die(f"installed keyfile {installed} is {len(blob)} bytes, expected 64.")
    write_key_copy(KEYFILE, blob)
    print("Recovered keyfile from " + installed + " into " + KEYFILE + ".")
    return KEYFILE


def do_keygen(ctx) -> None:
    if ensure_keyfile(ctx) is None:
        write_key_copy(KEYFILE, os.urandom(64))
        print("Generated 64-byte keyfile at " + KEYFILE)
    else:
        print("Keyfile already present; reusing it (never regenerated).")
    installed = install_keyfile(ctx)
    print(f"Keyfile installed at {installed} (0600).")


def do_keyfile_notice(ctx) -> None:
    """Informational pause -- no scanning, listing, copying or hashing.

    Backing the keyfile up externally is the operator's own call; this
    stage only shows where the key lives and waits, so an interruption
    here resumes cleanly on the persisted installed copy."""
    if ensure_keyfile(ctx) is None:
        die("no keyfile generated yet; the keygen stage must run first.")
    print("Generated keyfile (64 bytes):")
    print("  live copy:  " + KEYFILE)
    print("  installed:  " + ctx["root_mnt"] + KEYFILE_INSTALLED)
    print("              (inside the target system: " + KEYFILE_INSTALLED + ")")
    print("")
    print("Copying the keyfile to external media is your responsibility,")
    print("if you want an external backup. This tool does not copy it")
    print("anywhere; keep the installed copy safe -- it unlocks slot 1.")
    press_enter()


def do_enroll(ctx) -> None:
    # the live keyfile may have been deleted by cleanup() when a previous
    # run was interrupted: recover the persisted installed copy instead of
    # ever regenerating a different key
    if ensure_keyfile(ctx) is None:
        die("no keyfile to enroll; the keygen stage must run first.")
    # any keyslot other than 0 or 1 is an unexpected layout; slot 1 being
    # present is the already-enrolled state, handled by reconciliation
    require_no_unexpected_slots(ctx["p2"])
    # the live copy may be stale relative to the durable copy or to what
    # slot 1 really holds: reconcile both copies against the container
    # before enrolling anything (never trusts a 64-byte copy blindly).
    # With slot 1 already present this either keeps the working key or
    # kills slot 1 and re-enrolls the chosen candidate -- never regenerates.
    reconcile_keyfile(ctx)
    ensure_passfile(ctx["p2"])
    if not slot_present(ctx["p2"], 1):
        # adding a NEW slot 1 requires exactly one keyslot, slot 0
        require_only_slot0(ctx["p2"])
        # the durable installed copy must hold the bytes to be enrolled
        # BEFORE luksAddKey, so a crash between the two leaves a
        # consistent state that the next run can simply resume
        installed = install_keyfile(ctx)
        run_required(
            [
                "cryptsetup",
                "luksAddKey",
                "--key-slot",
                "1",
                ctx["p2"],
                KEYFILE,
                "--key-file",
                PASSFILE,
            ]
        )
        print("Keyfile enrolled as slot 1 (argon2id).")
    else:
        # already-enrolled state: the durable installed copy is never
        # rewritten after the slot exists; it is only created when missing
        installed = ctx["root_mnt"] + KEYFILE_INSTALLED
        if read_key_copy(installed) is None:
            installed = install_keyfile(ctx)

    for slot, keyfile in ((0, PASSFILE), (1, installed)):
        result = run_command(
            [
                "cryptsetup",
                "open",
                "--test-passphrase",
                "--key-slot",
                str(slot),
                "--key-file",
                keyfile,
                ctx["p2"],
            ],
            capture_output=True,
        )
        if result.returncode != 0:
            die(f"slot {slot} failed the test-passphrase check; aborting.")
    print("Both keyslots verified (slot 0 passphrase, slot 1 keyfile).")


def do_config(ctx) -> None:
    path = ctx["root_mnt"] + "/etc/default/grub"
    if not os.path.isfile(path):
        die(f"{path} not found; is this really a Beryllium OS install?")
    rd_param = f"rd.luks.name={ctx['handoff']['luks_uuid']}=cryptroot"
    rd_options = "rd.luks.options=discard"

    with open(path, "r", encoding="utf-8") as handle:
        lines = handle.read().splitlines()

    new_lines = []
    have_cryptodisk = False
    have_cmdline = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("GRUB_ENABLE_CRYPTODISK="):
            new_lines.append("GRUB_ENABLE_CRYPTODISK=y")
            have_cryptodisk = True
            continue
        if stripped.startswith("GRUB_CMDLINE_LINUX_DEFAULT="):
            match = re.match(r'GRUB_CMDLINE_LINUX_DEFAULT="(.*)"\s*$', line)
            inner = match.group(1) if match else stripped.split("=", 1)[1].strip('"')
            tokens = [t for t in inner.split() if not t.startswith("rd.luks.")]
            tokens.extend((rd_param, rd_options))
            new_lines.append('GRUB_CMDLINE_LINUX_DEFAULT="' + " ".join(tokens) + '"')
            have_cmdline = True
            continue
        new_lines.append(line)

    if not have_cryptodisk:
        new_lines.append("GRUB_ENABLE_CRYPTODISK=y")
    if not have_cmdline:
        new_lines.append(f'GRUB_CMDLINE_LINUX_DEFAULT="{rd_param} {rd_options}"')

    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(new_lines) + "\n")
    print(f"Updated {path}: cryptodisk enabled, {rd_param} set.")


def do_dracut_conf(ctx) -> None:
    conf_dir = ctx["root_mnt"] + "/etc/dracut.conf.d"
    os.makedirs(conf_dir, exist_ok=True)

    existing_keyfile_line = False
    existing_crypt_module = False
    for name in sorted(os.listdir(conf_dir)):
        if not name.endswith(".conf"):
            continue
        with open(os.path.join(conf_dir, name), "r", encoding="utf-8") as handle:
            content = handle.read()
        if KEYFILE_INSTALLED in content:
            existing_keyfile_line = True
        if re.search(r'add_dracutmodules\+?=\s*"?\s*.*\bcrypt\b', content):
            existing_crypt_module = True

    target_conf = os.path.join(conf_dir, "00-crypto.conf")
    lines = []
    if os.path.isfile(target_conf):
        with open(target_conf, "r", encoding="utf-8") as handle:
            lines = [l for l in handle.read().splitlines() if l.strip()]

    if not existing_crypt_module:
        lines.append('add_dracutmodules+=" crypt btrfs "')
    if not existing_keyfile_line:
        lines.append(f'install_items+=" {KEYFILE_INSTALLED} "')

    with open(target_conf, "w", encoding="utf-8") as handle:
        handle.write("\n".join(dict.fromkeys(lines)) + "\n")
    print(
        f"dracut config ensured in {target_conf} "
        f"(existing confs were scanned for conflicts)."
    )


def do_chroot(ctx) -> None:
    esp_mnt = ctx["root_mnt"] + "/boot/efi"
    os.makedirs(esp_mnt, exist_ok=True)
    if not os.path.ismount(esp_mnt):
        run_required(["mount", ctx["esp_part"], esp_mnt])
        ctx["esp_mounted"] = True

    # every Beryllium OS system ships the dracut-rebuild wrapper; it picks
    # the right kernel(s) and dracut options for this install, so a bare
    # 'dracut -f' would rebuild less than the system itself expects
    print("Regenerating initramfs (dracut-rebuild)...")
    run_required(["arch-chroot", ctx["root_mnt"], "dracut-rebuild"])
    print("Regenerating grub.cfg...")
    run_required(
        ["arch-chroot", ctx["root_mnt"], "grub-mkconfig", "-o", "/boot/grub/grub.cfg"]
    )
    print("Installing GRUB (removable path, embedded cryptomount config)...")
    run_required(
        [
            "arch-chroot",
            ctx["root_mnt"],
            "grub-install",
            "--target=" + ctx["handoff"]["grub_target"],
            "--removable",
        ]
    )


def ensure_esp_mounted(ctx) -> None:
    """Make sure ctx['esp_part'] is mounted at <root>/boot/efi.

    Stages after the chroot stage (ESP prune, verification) assume the ESP
    is mounted, but cleanup() from an interrupted earlier run leaves it
    unmounted, so a resumed run has to mount it again before they touch
    it. When something already occupies that mountpoint, its source must
    be this run's ESP -- never a different device."""
    esp_mnt = ctx["root_mnt"] + "/boot/efi"
    if os.path.ismount(esp_mnt):
        result = run_required(
            ["findmnt", "-no", "SOURCE", esp_mnt], capture_output=True
        )
        mounted = result.stdout.strip().split("[", 1)[0]
        if os.path.realpath(mounted) != os.path.realpath(ctx["esp_part"]):
            die(
                f"{esp_mnt} is mounted from {mounted}, not from "
                f"{ctx['esp_part']}; the ESP mount does not match the "
                "target disk."
            )
        return
    os.makedirs(esp_mnt, exist_ok=True)
    run_required(["mount", ctx["esp_part"], esp_mnt])


def do_prune(ctx) -> None:
    ensure_esp_mounted(ctx)
    boot_name = ctx["handoff"]["boot_name"]
    keep_rel = "EFI/BOOT/" + boot_name
    keep_abs = os.path.join(ROOT_MNT, "boot/efi", keep_rel)
    if not os.path.isfile(keep_abs):
        die(f"{keep_rel} not found on the ESP after grub-install; aborting prune.")

    esp_root = os.path.realpath(ROOT_MNT + "/boot/efi")
    removed = 0
    for dirpath, dirnames, filenames in os.walk(esp_root, topdown=False):
        for filename in filenames:
            full = os.path.join(dirpath, filename)
            rel = os.path.relpath(full, esp_root)
            if rel == keep_rel:
                continue
            if not os.path.realpath(full).startswith(esp_root):
                continue  # never touch anything outside the ESP
            os.unlink(full)
            removed += 1
        for dirname in dirnames:
            full = os.path.join(dirpath, dirname)
            rel = os.path.relpath(full, esp_root)
            if rel == keep_rel or keep_rel.startswith(rel + "/"):
                continue
            if not os.path.realpath(full).startswith(esp_root):
                continue
            os.rmdir(full)
            removed += 1

    remaining = []
    for dirpath, dirnames, filenames in os.walk(esp_root):
        for filename in filenames:
            remaining.append(os.path.relpath(os.path.join(dirpath, filename), esp_root))
    if remaining != [keep_rel]:
        die(f"ESP prune incomplete; still present: {remaining}")
    print(f"ESP reduced to exactly {keep_rel} ({removed} entries removed).")

    # remember whether the embedded cryptomount config made it into the EFI
    with open(keep_abs, "rb") as handle:
        blob = handle.read()
    token = b"cryptomount -u " + ctx["handoff"]["luks_uuid"].encode()
    ctx["efi_has_cryptomount"] = token in blob


def uuid_forms(uuid: str) -> set[str]:
    """Both spellings of a UUID that may legitimately appear on disk.

    cryptsetup prints LUKS UUIDs dashed, while GRUB commonly embeds the
    dashless spelling, so any comparison against generated GRUB content has
    to accept either form instead of one exact raw string."""
    value = (uuid or "").strip().lower()
    return {value, value.replace("-", "")}


def grub_cfg_initramfs_paths(grub_cfg: str) -> list[str]:
    """Every initramfs path grub.cfg asks GRUB to load, in file order.

    Accepts the 'initrd' command with an optional numeric suffix and the
    'initrdefi' alias. A single initrd line may concatenate several
    images -- typically the CPU microcode first and the real initramfs
    after it -- so only paths whose basename identifies an initramfs
    image ('initramfs' or 'initrd', case-insensitive) are returned;
    microcode and other payloads are skipped and never verified as
    initramfs."""
    paths = []
    for line in grub_cfg.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = stripped.split()
        if not re.fullmatch(r"initrd(?:\d*|efi)", fields[0]):
            continue
        for field in fields[1:]:
            field = field.strip("\"'")
            if field.startswith("("):
                # GRUB device prefix, e.g. (hd0,gpt2)/boot/initramfs-*.img
                field = field.split(")", 1)[-1]
            if not field:
                continue
            base = os.path.basename(field).lower()
            if "initramfs" not in base and "initrd" not in base:
                # microcode (intel-ucode.img, amd-ucode.img, ...) or any
                # other payload concatenated onto the initrd line
                continue
            paths.append(field)
    return paths


def resolve_grub_path(root_mnt: str, path: str) -> str:
    """Map a path grub.cfg references onto the mounted target root."""
    if path.startswith("/"):
        return os.path.normpath(root_mnt + path)
    # relative paths are resolved against grub's prefix
    return os.path.normpath(os.path.join(root_mnt, "boot/grub", path))


def dracut_modules_of(lsinitrd_output: str) -> set[str]:
    """Module names lsinitrd listed under its 'dracut modules:' section."""
    modules = set()
    collecting = False
    for line in lsinitrd_output.splitlines():
        if line.strip() == "dracut modules:":
            collecting = True
            continue
        if collecting:
            if line.startswith("="):
                break
            modules.update(line.split())
    return modules


def extract_initramfs_member(image: str, member: str, dest: str) -> bool:
    """Extract MEMBER from the initramfs IMAGE into DEST (binary-safe).

    run_command() captures text, which would corrupt keyfile bytes, so
    lsinitrd's byte stream goes straight into DEST. Returns True only
    when lsinitrd succeeded and wrote a non-empty file."""
    try:
        with open(dest, "wb") as handle:
            result = subprocess.run(
                ["lsinitrd", image, member],
                stdout=handle,
                stderr=subprocess.DEVNULL,
                check=False,
            )
    except OSError:
        return False
    if result.returncode != 0:
        return False
    return os.path.getsize(dest) > 0


def do_verify(ctx) -> None:
    ensure_esp_mounted(ctx)
    failures = []
    root_mnt = ctx["root_mnt"]

    # PASSFILE may be gone (cleanup deleted it) or stale after a resumed
    # run, so make sure it holds a passphrase that unlocks p2 before the
    # slot checks below depend on it.
    ensure_passfile(ctx["p2"])

    result = run_command(
        [
            "cryptsetup",
            "open",
            "--test-passphrase",
            "--key-slot",
            "0",
            "--key-file",
            PASSFILE,
            ctx["p2"],
        ],
        capture_output=True,
    )
    if result.returncode != 0:
        failures.append("slot 0 (passphrase) does not open")

    result = run_command(
        [
            "cryptsetup",
            "open",
            "--test-passphrase",
            "--key-slot",
            "1",
            "--key-file",
            root_mnt + KEYFILE_INSTALLED,
            ctx["p2"],
        ],
        capture_output=True,
    )
    if result.returncode != 0:
        failures.append("slot 1 (keyfile) does not open")

    # Recompute the LUKS UUID here instead of trusting state some earlier
    # stage left in ctx: verification must work on its own when it is
    # resumed as a stage of its own.
    luks_uuid = ""
    result = run_command(["cryptsetup", "luksUUID", ctx["p2"]], capture_output=True)
    if result.returncode == 0:
        luks_uuid = result.stdout.strip()
    if not luks_uuid:
        luks_uuid = ctx["handoff"].get("luks_uuid") or ""
    if not luks_uuid:
        failures.append("cannot determine the LUKS UUID of " + ctx["p2"])

    grub_cfg_path = root_mnt + "/boot/grub/grub.cfg"
    try:
        with open(grub_cfg_path, "r", encoding="utf-8") as handle:
            grub_cfg = handle.read()
    except OSError as error:
        grub_cfg = ""
        failures.append(f"cannot read {grub_cfg_path}: {error}")

    if luks_uuid:
        expected = f"rd.luks.name={luks_uuid}=cryptroot"
        if expected not in grub_cfg:
            failures.append(f"grub.cfg does not contain {expected}")
        if "rd.luks.options=discard" not in grub_cfg:
            failures.append("grub.cfg does not contain rd.luks.options=discard")

    # EVERY initramfs image grub.cfg references has to be there, readable by
    # lsinitrd, and carry the keyfile plus the crypt dracut module -- an
    # image that is missing or unreadable fails the gate on its own.
    key_member = KEYFILE_INSTALLED.lstrip("/")
    # the embedded keyfile bytes are proven against LUKS slot 1 directly;
    # the scratch copy lives only under WORKDIR and is deleted afterwards
    embedded_key_tmp = os.path.join(WORKDIR, "verify-embedded-cryptroot.key")
    os.makedirs(WORKDIR, exist_ok=True)
    try:
        images = []
        for path in grub_cfg_initramfs_paths(grub_cfg):
            image = resolve_grub_path(root_mnt, path)
            if image not in images:
                images.append(image)
        if not images and grub_cfg:
            failures.append("grub.cfg references no initramfs image")
        for image in images:
            name = os.path.relpath(image, root_mnt)
            if not os.path.isfile(image):
                failures.append(f"initramfs {name} referenced by grub.cfg is missing")
                continue
            result = run_command(["lsinitrd", image], capture_output=True)
            if result.returncode != 0:
                failures.append(f"lsinitrd failed on {name}")
                continue
            listing = result.stdout or ""
            modules = dracut_modules_of(listing)
            have_key = key_member in listing
            have_crypt = "crypt" in modules
            if not have_key:
                failures.append(f"keyfile not found in {name}")
            if not have_crypt:
                failures.append(f"crypt dracut module missing from {name}")
            if have_key:
                # pathname presence in the listing is not proof: extract
                # the embedded bytes and prove they unlock slot 1. When
                # extraction is impossible, fail closed instead of
                # trusting the listing.
                if not extract_initramfs_member(image, key_member, embedded_key_tmp):
                    failures.append(
                        f"cannot extract the embedded keyfile from {name}; "
                        "cannot prove it opens LUKS slot 1"
                    )
                elif test_key_slot(ctx["p2"], embedded_key_tmp, 1):
                    print(
                        "Embedded keyfile in "
                        + os.path.basename(image)
                        + " opens LUKS slot 1."
                    )
                else:
                    failures.append(
                        f"embedded keyfile in {name} does not open LUKS slot 1"
                    )
            if have_key and have_crypt:
                print(f"Keyfile present in initramfs: {os.path.basename(image)}")
    finally:
        try:
            os.unlink(embedded_key_tmp)
        except FileNotFoundError:
            pass

    # What GRUB actually generated for unlocking the disk: grub-install
    # writes <grubdir>/<target>/load.cfg (plain text) and embeds that file
    # verbatim into the EFI image, which may hold it compressed -- so the
    # load.cfg content is checked first and the raw ESP binary only as a
    # secondary source, with both UUID spellings accepted in either.
    if luks_uuid:
        forms = uuid_forms(luks_uuid)
        target = ctx["handoff"].get("grub_target") or ""
        load_cfg_path = ""
        if target:
            load_cfg_path = os.path.join(root_mnt, "boot/grub", target, "load.cfg")
        load_cfg = ""
        if load_cfg_path and os.path.isfile(load_cfg_path):
            try:
                with open(load_cfg_path, "r", encoding="utf-8") as handle:
                    load_cfg = handle.read()
            except OSError:
                load_cfg = ""

        # The firmware executes the EFI binary, not load.cfg, so a fresh
        # load.cfg next to a stale BOOT*.EFI must never pass: positive
        # evidence has to come from the binary itself. Only a raw token
        # match inside the EFI blob is accepted; a compressed or otherwise
        # unreadable payload fails closed here -- falling back to
        # load.cfg alone would reintroduce the false pass.
        efi_verified = False
        boot_name = ctx["handoff"].get("boot_name") or ""
        if not boot_name:
            failures.append("handoff does not name the boot EFI binary")
        else:
            efi_path = os.path.join(root_mnt, "boot/efi", "EFI/BOOT", boot_name)
            if not os.path.isfile(efi_path):
                failures.append(f"{boot_name} not found on the ESP")
            else:
                with open(efi_path, "rb") as handle:
                    efi_blob = handle.read()
                efi_verified = any(
                    b"cryptomount -u " + form.encode("ascii") in efi_blob
                    for form in forms
                )
                if not efi_verified:
                    failures.append(
                        "BOOT EFI binary lacks the embedded cryptomount config "
                        f"for LUKS UUID {luks_uuid}; it may be stale or "
                        "compressed -- it cannot be positively verified, so "
                        "verification fails closed (load.cfg alone is never "
                        "sufficient)."
                    )

        # load.cfg is an additional consistency signal only: when the EFI
        # binary is verified but the freshly readable load.cfg disagrees
        # with it, something is stale and must be resolved before reboot.
        if efi_verified and load_cfg_path:
            if load_cfg and not any(
                f"cryptomount -u {form}" in load_cfg for form in forms
            ):
                failures.append(
                    f"{load_cfg_path} lacks the cryptomount token for LUKS "
                    f"UUID {luks_uuid} although the EFI binary carries it; "
                    "the pair is inconsistent."
                )

    result = run_command(
        ["arch-chroot", root_mnt, "grub-script-check", "/boot/grub/grub.cfg"],
        capture_output=True,
    )
    if result.returncode != 0:
        failures.append("grub-script-check rejected grub.cfg")

    if failures:
        for failure in failures:
            print("FAIL: " + failure)
        die("verification gate failed; the system may not boot until fixed.")
    print("Verification gate: all checks passed.")


def cleanup(ctx) -> None:
    """Release everything this run opened.

    Every unmount is retried until it succeeds: an ignored umount failure
    here used to let the work directory be deleted while the target
    filesystem was still mounted under it. When the mount table itself
    cannot be read, nothing can be proven unmounted, so the work
    directory is never removed -- and a mapping that is not provably this
    run's is warned about and left alone instead of being closed."""
    root_mnt = ctx.get("root_mnt") or ROOT_MNT
    try:
        targets = mounted_under(WORKDIR)
        if targets is None:
            # fail closed on an unreadable mount table: only the mounts
            # this run can make are known by path, and umount_retry falls
            # back to a stat-based check while the table is unreadable
            print(
                "Warning: cannot read the mount table; unmounting only the "
                "known paths under " + WORKDIR + "."
            )
            for target in (root_mnt + "/boot/efi", root_mnt):
                umount_retry(target)
        else:
            # deepest first, so nested mounts (ESP, chroot binds) go before
            # their parents; umount_retry never gives up
            for target in targets:
                umount_retry(target)
        if ctx.get("mapper"):
            backing = mapper_backing_device()
            if backing is None:
                pass  # nothing mapped any more; nothing to close
            elif not mapper_is_ours(ctx, backing):
                print(
                    f"Warning: {MAPPER_DEV} is not backed by {ctx['p2']}; "
                    "leaving this foreign mapping alone."
                )
            else:
                result = run_command(
                    ["cryptsetup", "close", MAPPER_NAME], capture_output=True
                )
                if result.returncode != 0:
                    detail = (result.stderr or "").strip().splitlines()
                    detail = ": " + detail[-1] if detail else ""
                    print(f"Warning: could not close {MAPPER_NAME}{detail}")
    finally:
        leftovers = mounted_under(WORKDIR)
        if leftovers is None:
            # fail closed: the mount table is unreadable, so there is no
            # positive evidence that nothing is mounted under WORKDIR
            print("Warning: mount table unreadable; not removing " + WORKDIR + ".")
        elif leftovers:
            # never recurse into a mounted tree with rmtree()
            print(
                "Warning: leaving work directory in place, still mounted: "
                + ", ".join(leftovers)
            )
        else:
            shutil.rmtree(WORKDIR, ignore_errors=True)


def recover_stale_state(ctx) -> None:
    """Tear down state a previous run of THIS tool left behind.

    A previous live run killed by SIGKILL (or a signal that never reached
    its cleanup) can leave mounts under WORKDIR and an open cryptroot
    mapping behind; require_target_offline() would then reject the tool's
    own leftovers as target activity and block the automatic resume.

    Only this tool's own namespace is touched: mounts under WORKDIR are
    unmounted deepest-first and the mapping named cryptroot is closed
    only when it is provably backed by this run's target partition --
    the same proof cleanup() demands. A foreign or unidentifiable
    mapping is left alone (fail closed), exactly like cleanup() does;
    require_target_offline() runs afterwards and still refuses to
    convert while anything genuinely foreign holds the target."""
    targets = mounted_under(WORKDIR)
    if targets is None:
        print(
            "Warning: cannot read the mount table; not recovering mounts "
            "under " + WORKDIR + "."
        )
    else:
        for target in targets:
            umount_retry(target)

    if os.path.exists(MAPPER_DEV):
        backing = mapper_backing_device()
        if backing is None:
            pass  # nothing mapped any more; nothing to close
        elif not mapper_is_ours(ctx, backing):
            detail = (
                f"it maps {backing}"
                if backing
                else "its backing device cannot be determined"
            )
            print(
                f"Warning: {MAPPER_DEV} is not backed by {ctx['p2']} "
                f"({detail}); leaving this foreign mapping alone."
            )
        else:
            result = run_command(
                ["cryptsetup", "close", MAPPER_NAME], capture_output=True
            )
            if result.returncode != 0:
                detail = (result.stderr or "").strip().splitlines()
                detail = ": " + detail[-1] if detail else ""
                print(f"Warning: could not close {MAPPER_NAME}{detail}")

    leftovers = mounted_under(WORKDIR)
    if leftovers is None:
        # fail closed: the mount table is unreadable, so there is no
        # positive evidence that nothing is mounted under WORKDIR
        print("Warning: mount table unreadable; not removing " + WORKDIR + ".")
    elif leftovers:
        print(
            "Warning: leaving work directory in place, still mounted: "
            + ", ".join(leftovers)
        )
    else:
        shutil.rmtree(WORKDIR, ignore_errors=True)


def acquire_live_lock() -> int:
    """Take the global single-instance lock for the destructive live phase.

    An exclusive, non-blocking flock on LOCK_FILE -- a file outside WORKDIR
    so cleanup() cannot delete it -- makes concurrent invocations impossible
    for the whole run: two instances could otherwise both pass the
    target-offline gate and then interfere destructively. The fd is simply
    left open and dropped only when the process exits (every live-phase
    exit path does), which releases the lock; nothing is released manually.

    Returns the held fd (the caller keeps it referenced for the run)."""
    try:
        fd = os.open(LOCK_FILE, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    except OSError as error:
        die(f"cannot create the lock file {LOCK_FILE}: {error}")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        die(
            "another beryllium-fde live run is already active (lock held at "
            f"{LOCK_FILE}); refusing to start a second instance."
        )
    return fd


def verify_handoff_identity(
    handoff: dict,
    disk: dict,
    esp: dict,
    root: dict,
    root_uuid: str | None,
    what: str,
) -> None:
    """Prove the handoff describes exactly the disk/partitions selected.

    Cloned disks can share filesystem UUIDs, and a corrupted handoff must
    never steer a destructive run, so every recorded identifier is
    compared against the live selection: partition-table UUID of the
    disk, UUID of the ESP, UUID of the root filesystem (pass the mapper
    UUID in the LUKS branch), and -- when the handoff carries a serial --
    the disk's serial. A missing live serial is not silently accepted
    either: if the handoff recorded one, absence on this machine is
    treated as a mismatch and aborts the run."""
    live_ptuuid = disk.get("ptuuid")
    if not live_ptuuid or handoff["ptuuid"] != live_ptuuid:
        die(
            f"handoff partition table UUID {handoff['ptuuid']!r} does not "
            f"match the {what} {disk['name']}"
            + (f" (PTUUID {live_ptuuid!r})" if live_ptuuid else " (PTUUID unknown)")
            + "; wrong disk or corrupted handoff."
        )
    live_serial = disk.get("serial")
    if handoff.get("disk_serial"):
        if not live_serial:
            die(
                f"the handoff recorded disk serial {handoff['disk_serial']!r} "
                f"but the serial of {disk['name']} cannot be read; refusing "
                "to continue without proving the identity."
            )
        if handoff["disk_serial"] != live_serial:
            die(
                f"handoff disk serial {handoff['disk_serial']!r} does not "
                f"match the serial of {disk['name']} ({live_serial!r}); "
                "wrong disk."
            )
    live_esp_uuid = esp.get("uuid")
    if not live_esp_uuid or handoff["esp_uuid"] != live_esp_uuid:
        die(
            f"handoff ESP UUID {handoff['esp_uuid']!r} does not match the "
            f"ESP {esp['name']}"
            + (f" (UUID {live_esp_uuid!r})" if live_esp_uuid else " (UUID unknown)")
            + "; wrong disk or corrupted handoff."
        )
    live_root_uuid = root_uuid
    if not live_root_uuid or handoff["btrfs_uuid"] != live_root_uuid:
        die(
            f"handoff root filesystem UUID {handoff['btrfs_uuid']!r} does not "
            f"match the root partition {root['name']}"
            + (f" (UUID {live_root_uuid!r})" if live_root_uuid else " (UUID unknown)")
            + "; wrong disk or corrupted handoff."
        )


def live(args: argparse.Namespace) -> None:
    require_root()
    # single-instance lock, taken before anything else touches the target:
    # held until process exit, so cleanup and signal paths stay covered
    lock_fd = acquire_live_lock()

    # SIGTERM and SIGHUP must lead to the same teardown as an
    # interruption instead of leaving mounts and the mapper behind in
    # this live boot. The handler only raises, so no filesystem work
    # happens in signal context: the exception unwinds into the existing
    # try/except/finally below (or, before it exists, out of live()),
    # where cleanup() runs exactly as for KeyboardInterrupt.
    def _signal_to_interrupt(signum, frame):
        raise KeyboardInterrupt

    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, _signal_to_interrupt)

    tree = lsblk_tree()
    live_disks = find_live_disk(tree)
    disk = select_disk(tree, live_disks)

    disk_dev = "/dev/" + disk["name"]
    esp, root = validate_layout(disk)
    if esp is None or root is None:
        die("layout validation did not return both partitions.")

    ctx = {
        "disk": disk_dev,
        "target_devices": [disk_dev]
        + ["/dev/" + p["name"] for p in partition_children(disk)],
        "target_uuids": {
            (p.get("uuid") or "").lower(): "/dev/" + p["name"]
            for p in partition_children(disk)
            if p.get("uuid")
        },
        "esp_part": "/dev/" + esp["name"],
        "p2": "/dev/" + root["name"],
        "mapper": None,
        "esp_mounted": False,
        "handoff": {},
        "root_mnt": ROOT_MNT,
    }
    # Before the offline gate, clear anything a previous run of this tool
    # left behind when it was killed: its own mounts under WORKDIR and a
    # cryptroot mapping proven to be backed by this run's target
    # partition. Foreign state is left alone; the gate below still
    # refuses to convert while the target is held by anything else.
    recover_stale_state(ctx)
    # Two read-only gates, both run BEFORE the first mount or unlock
    # below: the target must be completely offline (nothing mounted,
    # swapped or mapped from it), and the filesystem to convert must be
    # single-device with its one device being the target root partition.
    # A resumed LUKS conversion cannot be inspected until the container
    # is unlocked, so its single-device check runs in that branch below.
    require_target_offline(ctx)
    if root.get("fstype") != "crypto_LUKS":
        require_single_device_btrfs(ctx["p2"], ctx["p2"], "root btrfs")
    # From here on this run owns mounts, mappers and passphrase files, so
    # every failure path must go through cleanup() -- hence the try block
    # starts before the first mount/unlock instead of after it.
    try:
        if root.get("fstype") == "crypto_LUKS":
            # mid-conversion state: the container exists, unlock it to reach
            # the handoff file and continue where the previous run stopped
            result = run_command(
                ["cryptsetup", "isLuks", ctx["p2"]], capture_output=True
            )
            if result.returncode != 0:
                die(f"{ctx['p2']} reports crypto_LUKS but isLuks failed.")
            ctx["luks_uuid"] = run_required(
                ["cryptsetup", "luksUUID", ctx["p2"]], capture_output=True
            ).stdout.strip()
            print(f"{ctx['p2']} is already a LUKS2 container ({ctx['luks_uuid']}).")
            print("This looks like an interrupted conversion; resuming it.")
            # Finish a half-done reencryption FIRST: while a reencryption
            # is recorded as pending, cryptsetup's offline-reencrypt
            # requirement can refuse to activate the container at all, so
            # opening the mapper first would make this forced resume never
            # run. The resume needs the underlying device to itself, so it
            # happens before anything maps or mounts it.
            resume_reencrypt(ctx["p2"])
            # The filesystem lives inside the container now, so it can
            # only be inspected unlocked: unlock it, prove it is
            # single-device (its one device is this container on
            # ctx['p2'] by construction) -- and keep that verified mapping
            # open, because mount_root() and the handoff reading below
            # need it.
            open_mapper(ctx, None)
            require_single_device_btrfs(MAPPER_DEV, None, "root btrfs")
            ctx["mapper"] = MAPPER_NAME
            mount_root(ctx)
            ctx["handoff"] = read_handoff(ROOT_MNT)
            verify_handoff_identity(
                ctx["handoff"],
                disk,
                esp,
                root,
                lsblk_value(MAPPER_DEV, "UUID"),
                "LUKS-resume target disk",
            )
            # a recorded container UUID must still be this container's:
            # overwriting it here would silently re-target later stages
            recorded_luks = ctx["handoff"].get("luks_uuid")
            if recorded_luks and recorded_luks != ctx["luks_uuid"]:
                die(
                    f"handoff luks_uuid {recorded_luks!r} does not match the "
                    f"actual container UUID {ctx['luks_uuid']!r}; "
                    "corrupted handoff or wrong disk."
                )
            if ctx["handoff"].get("stage_completed", -1) >= ST_DONE:
                die(
                    "handoff records stage_completed="
                    f"{ctx['handoff'].get('stage_completed')}; there is no "
                    "work left to resume; refusing to run any stage again."
                )
            ctx["handoff"]["luks_uuid"] = ctx["luks_uuid"]
            stage = max(ctx["handoff"].get("stage_completed", -1) + 1, ST_RECLAIM)
            print(f"Resuming at stage {stage + 1}.")
        else:
            mount_root(ctx)
            ctx["handoff"] = read_handoff(ROOT_MNT)
            verify_handoff_identity(
                ctx["handoff"],
                disk,
                esp,
                root,
                root.get("uuid"),
                "target disk",
            )
            if ctx["handoff"].get("stage_completed", -1) >= ST_ENCRYPT:
                die(
                    "handoff records stage_completed="
                    f"{ctx['handoff'].get('stage_completed')}, past the "
                    "encrypt stage, but the disk is still plaintext; a "
                    "successful encrypt stage would have made it LUKS. "
                    "Refusing to resume from an inconsistent handoff."
                )
            stage = ctx["handoff"].get("stage_completed", -1) + 1
            if stage > ST_BACKUP:
                print(f"Handoff found; resuming at stage {stage + 1}.")

        require_bins("live environment")

        stages = [
            (ST_BACKUP, "backup gate", do_backup_gate),
            (ST_TAIL, "tail room", do_tail_room),
            (ST_ENCRYPT, "encrypt", do_encrypt),
            (ST_RECLAIM, "reclaim", do_reclaim),
            (ST_KEYGEN, "keyfile generation", do_keygen),
            (ST_KEYNOTICE, "keyfile backup notice", do_keyfile_notice),
            (ST_ENROLL, "keyfile enrollment", do_enroll),
            (ST_CONFIG, "grub config", do_config),
            (ST_DRACUTCONF, "dracut config", do_dracut_conf),
            (ST_CHROOT, "chroot rebuild", do_chroot),
            (ST_PRUNE, "ESP prune", do_prune),
            (ST_VERIFY, "verification", do_verify),
        ]

        for number, label, function in stages:
            if number < stage:
                continue
            print(f"\n=== Stage {number + 1}/{len(stages)}: {label} ===")
            function(ctx)
            if number != ST_VERIFY:
                # persisted immediately, before the next stage can start
                set_stage(ctx, number)
        set_stage(ctx, ST_DONE)
        ctx["handoff"]["phase"] = "encrypted"
        persist_handoff(ctx)

        print("\nDone. This system is now fully encrypted:")
        print("  - ESP holds only the GRUB EFI binary")
        print("  - everything else lives inside LUKS2")
        print("  - one passphrase prompt at GRUB, silent initramfs unlock")
        print(
            "\nReboot to verify. Remove the live medium. Keep the keyfile backup (if you actually bothered copying it) safe."
        )
    except KeyboardInterrupt:
        print("\nInterrupted during operations.")
        if ctx["handoff"] and ctx["handoff"].get("stage_completed", -1) >= ST_ENCRYPT:
            print("The disk is encrypted but setup may be incomplete.")
            print("Boot the live ISO again and re-run 'beryllium-fde live' to resume.")
        sys.exit(130)
    finally:
        cleanup(ctx)


def do_backup_gate(ctx) -> None:
    print("The next stages rewrite the entire root partition.")
    if not ask_yes_no(
        "Do you have a verified full backup of this system?", default=False
    ):
        die("no backup confirmed; nothing was changed.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Full disk encryption for Beryllium OS: in-place LUKS2 "
        "conversion driven from a live ISO."
    )
    parser.add_argument(
        "mode",
        choices=["prepare", "live"],
        help="'prepare' runs on the installed system (read-only audit + "
        "handoff); 'live' runs from the live ISO and performs the conversion",
    )
    parser.add_argument(
        "--i-have-a-backup",
        action="store_true",
        help="prepare: skip the interactive backup acknowledgement",
    )
    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(0)
    args = parser.parse_args()

    if args.mode == "prepare":
        prepare(args)
    else:
        live(args)


if __name__ == "__main__":
    main()
