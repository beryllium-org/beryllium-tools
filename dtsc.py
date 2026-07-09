#!/usr/bin/env python3

"""
This script is part of Beryllium-Tools, a fork of BredOS-Tools, licenced under the GPL-3.0 licence.

Bill Sideris <bill88t@feline.gr>
"""

import os
import sys
import argparse
import tempfile
import subprocess


def fdt_hash_from_proc(output_file: str, mode: str = "dts") -> None:
    """
    Decompile live-running device tree from /proc/
    """
    try:
        subprocess.check_output(
            [
                "dtc",
                "-I",
                "fs",
                "-O",
                mode,
                "-@",
                "-q",
                "/proc/device-tree",
                "-o",
                output_file,
            ],
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        pass


def decomp_dtb(input_file: str, output_file: str) -> None:
    try:
        subprocess.check_output(
            ["dtc", "-I", "dtb", input_file, "-O", "dts", "-q", "-o", output_file],
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        pass


def preprocess_dts(input_file: str, temp_file: str, include: str, kernel: str) -> bool:
    """
    Preprocess the DTS file using the C preprocessor.
    """
    linux_include_path = kernel
    if not linux_include_path:
        linux_include_path = next(
            (
                os.path.join("/usr/src", d)
                for d in os.listdir("/usr/src")
                if "linux" in d and os.path.isdir(os.path.join("/usr/src", d))
            ),
            None,
        )
    if not linux_include_path:
        print(
            "Error: Could not find a valid Linux source include directory.",
            file=sys.stderr,
        )
        return False
    else:
        print("Using kernel: " + linux_include_path)

    cmd = [
        "cpp",
        "-nostdinc",
        "-I",
        os.path.join(linux_include_path, "include"),
    ]

    if include is not None:
        cmd += ["-I", include]

    cmd += [
        "-undef",
        "-x",
        "assembler-with-cpp",
        input_file,
        temp_file,
    ]

    print("Preprocessing...")
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError:
        print("Error: Preprocessing FAILED!", file=sys.stderr)
        return False
    return True


def compile_dts(temp_file: str, output_file: str, include: str = None) -> bool:
    """
    Compile the preprocessed DTS file into a DTBO or DTB.
    """
    cmd = ["dtc"]
    if include is not None:
        cmd += ["-i", include]
    cmd += ["-I", "dts", "-@", "-O", "dtb", temp_file, "-o", output_file]
    print("Compiling...")
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError:
        print("Error: Compiling FAILED!", file=sys.stderr)
        return False
    return True


def check_dependencies() -> None:
    """
    Verify required tools are available on the system.
    """

    def is_executable_in_path(cmd: str) -> bool:
        paths = os.environ.get("PATH", "").split(os.pathsep)
        for path in paths:
            full_path = os.path.join(path, cmd)
            if os.path.isfile(full_path) and os.access(full_path, os.X_OK):
                return True
        return False

    if not is_executable_in_path("dtc"):
        print(
            "Error: The 'dtc' command is not installed or not in PATH.", file=sys.stderr
        )
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="(De)Compile a Device Tree Source / Blob file.",
        epilog="Example: dtsc my_device_tree.dts -o output.dtbo",
    )
    parser.add_argument("input", nargs="?", help="Input file")
    parser.add_argument(
        "-o",
        "--output",
        help="Output file (Default: Follows filename)",
        default=None,
    )
    parser.add_argument(
        "-i",
        "--include",
        help="Source of additional device tree files (optional)",
        default=None,
    )

    parser.add_argument(
        "-k",
        "--kernel",
        help="Manualy specify a kernel source path (default: autodetect)",
        default=None,
    )

    args = parser.parse_args()

    # Print help and exit if no arguments are provided
    if not args.input:
        parser.print_help()
        exit(1)

    # Verify required tools
    check_dependencies()

    input_file = args.input
    if input_file.lower() != "system":
        if not input_file.endswith((".dts", ".dtb", ".dtbo")):
            print("Input file must be .dts, .dtb, or .dtbo")
            exit(1)

    if args.output and input_file.rsplit(".", 1)[-1] == args.output.rsplit(".", 1)[-1]:
        print("Input and output file extensions must differ.")
        exit(1)

    output_file = args.output or (
        "system.dts"
        if input_file.lower() == "system"
        else input_file.rsplit(".", 1)[0]
        + (".dtb" if input_file.endswith(".dts") else ".dts")
    )
    include = args.include
    kernel = args.kernel

    if input_file.lower() != "system" and not os.path.isfile(input_file):
        print(f"Error: Input file '{input_file}' does not exist.", file=sys.stderr)
        exit(1)

    if not output_file.endswith(".dts"):
        if input_file.lower() == "system":
            fdt_hash_from_proc(output_file, "dtb")
        else:
            with tempfile.NamedTemporaryFile(delete=False) as temp:
                temp_file = temp.name

            try:
                if not preprocess_dts(input_file, temp_file, include, kernel):
                    exit(1)
                if not compile_dts(temp_file, output_file, include):
                    exit(1)
            finally:
                if os.path.exists(temp_file):
                    os.remove(temp_file)

            print(
                f"Device Tree Blob {'Overlay ' if output_file.endswith('.dtbo') else ''}successfully created: {output_file}"
            )
    else:
        if input_file.lower() == "system":
            fdt_hash_from_proc(output_file)
        else:
            decomp_dtb(input_file, output_file)


if __name__ == "__main__":
    main()
