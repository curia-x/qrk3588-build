#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 ninozhang001@gmail.com
"""QRK3588 workflow CLI, shared by the sourced Bash commands and CI."""
import argparse
from pathlib import Path
import signal
import subprocess
import sys

from qrkbuild.context import Context
from qrkbuild.workflows import Workflows

BUILD_DIR = Path(__file__).resolve().parent
COMPONENTS = ("uboot", "kernel", "rootfs")
COMMANDS = [
    "build_all", "build_atf", "build_bl1", "build_fip", "build_quest_os", "install_quest_os",
    "clean_quest_os", "build_toolchain", "build_sdk", "build_kvm_selftests", "build_kernel_compile_commands",
    "build_linux_upgrade_package", "build_kernel_upgrade_package", "build_dtb_upgrade_package",
    "build_fip_upgrade_package", "pack_rootfs_upgrade_package", "prepare_nfs_rootfs",
    "prepare_nfs_rootfs_clean", "upgrade", "upgrade_check", "upgrade_boot", "upgrade_kernel",
    "upgrade_dtb", "upgrade_fip", "upgrade_rootfs", "run_bl1", "run_fip",
    "bl1_upgrade_fip", "download_bl1_maskrom", "install_package", "check_package",
] + [action + "_" + name for name in COMPONENTS
     for action in ("build", "defconfig", "menuconfig", "clean")] + [
         "prepare_uboot_config", "prepare_kernel_config", "prepare_rootfs_config"]


def dispatch(c, w, command, args):
    # Validate arity and relevant options before acquiring a lock or doing any work.
    maximum = 2 if command in ("build_all", "upgrade", "upgrade_check", "install_package", "check_package") else (
        1 if command in ("build_atf", "build_bl1", "build_fip", "upgrade_fip", "run_bl1",
                         "run_fip", "bl1_upgrade_fip", "download_bl1_maskrom",
                         "pack_rootfs_upgrade_package", "upgrade_rootfs", "build_fip_upgrade_package") else 0)
    if len(args) > maximum:
        raise ValueError(f"too many arguments for {command}")
    mode_commands = {"build_all", "build_atf", "build_bl1", "build_fip", "upgrade",
                     "upgrade_check", "upgrade_fip", "build_fip_upgrade_package", "run_bl1", "run_fip"}
    mode = args[0] if args and command in mode_commands else c.mode
    rootfs = args[1] if len(args) == 2 else None
    if command in mode_commands:
        c.validate(mode, images=command in ("build_all", "upgrade", "upgrade_check"),
                   partial=command in ("upgrade_fip", "build_fip_upgrade_package"))
    if command.startswith("upgrade_") and command != "upgrade_check":
        c.validate(mode, partial=True)
    if command in ("install_package", "check_package"):
        if len(args) != 2 or args[1] not in ("apply", "recover"):
            raise ValueError(f"{command} requires an exact package path and apply|recover")
    with c.locked():
        p = w.parts
        if command in ("install_package", "check_package"):
            return w.transport.transfer("check" if command == "check_package" else args[1],
                                        Path(args[0]), args[1] if command == "check_package" else "")
        if command in ("build_all", "upgrade", "upgrade_check"):
            if command == "build_all":
                return w.images(mode, rootfs)
            return w.upgrade("complete", mode, rootfs, check=command == "upgrade_check")
        if command in ("build_atf", "build_bl1", "build_fip"):
            return p.atf(mode, {"build_atf": "all", "build_bl1": "bl1", "build_fip": "fip"}[command])
        if command in ("build_quest_os", "install_quest_os", "clean_quest_os"):
            return p.quest_os(command.split("_")[0])
        if command == "build_toolchain":
            return p.toolchain()
        if command == "build_sdk":
            return p.sdk()
        if command == "build_kvm_selftests":
            return p.kvm_tests()
        if command == "build_kernel_compile_commands":
            return p.compile_commands()
        for component in COMPONENTS:
            if command == "build_" + component:
                return getattr(p, command)()
            for action in ("defconfig", "menuconfig", "clean"):
                if command == action + "_" + component:
                    return p.config(component, action)
            if command == "prepare_" + component + "_config":
                return p.prepare(component)
        if command in ("prepare_nfs_rootfs", "prepare_nfs_rootfs_clean"):
            return p.nfs(clean=command.endswith("_clean"))
        packages = {"build_linux_upgrade_package": "linux", "build_kernel_upgrade_package": "kernel",
                    "build_dtb_upgrade_package": "dtb", "build_fip_upgrade_package": "fip",
                    "pack_rootfs_upgrade_package": "rootfs"}
        if command in packages:
            # pack_rootfs is the explicit host-only existing-image interface.
            image = args[0] if args and command == "pack_rootfs_upgrade_package" else None
            if command == "pack_rootfs_upgrade_package" and image is None:
                image = c.build / "out/rootfs.ext4"
            return w.package(packages[command], mode, image)
        if command.startswith("upgrade_"):
            kind = command.removeprefix("upgrade_")
            image = args[0] if args and kind == "rootfs" else None
            return w.upgrade("linux" if kind == "boot" else kind, mode, image)
        if command == "bl1_upgrade_fip":
            return w.transport.send_fip(Path(args[0]) if args else c.fip)
        if command == "download_bl1_maskrom":
            if not args:
                raise ValueError("download_bl1_maskrom requires a BL1 image")
            return w.transport.maskrom(Path(args[0]))
        if command == "run_bl1":
            p.atf(mode, "bl1")
            return w.transport.maskrom(c.tf / "out/qrk3588_bl1.bin")
        if command == "run_fip":
            return w.run_fip(mode)
    raise ValueError("unknown command")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("arguments", nargs="*")
    options = parser.parse_args(argv)
    previous = signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    try:
        c = Context(BUILD_DIR)
        w = Workflows(c, BUILD_DIR / "scripts/qrk3588_transport.sh")
        dispatch(c, w, options.command, options.arguments)
        return 0
    except KeyboardInterrupt:
        return 130
    except (ValueError, OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        print(f"error: {error}", file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError):
            return error.returncode if error.returncode > 0 else 128 - error.returncode
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    sys.exit(main())
