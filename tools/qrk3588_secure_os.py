#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 ninozhang001@gmail.com
"""Prepare signed Linux FIT and a rootfs with an appended dm-verity tree."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import re
import sys
import struct


def run(*args):
    result = subprocess.run([str(a) for a in args], text=True,
                            stdout=subprocess.PIPE, check=False)
    print(result.stdout, end="", flush=True)
    # e2fsck returns 1 after correcting an offline filesystem.
    allowed = (0, 1) if str(args[0]) == "e2fsck" else (0,)
    if result.returncode not in allowed:
        raise subprocess.CalledProcessError(result.returncode, args)
    return result.stdout


def _verify_signing_key(keys):
    public = subprocess.check_output([
        "openssl", "pkey", "-passin", "pass:", "-in", str(keys / "os.key"),
        "-pubout"])
    certificate_public = subprocess.check_output([
        "openssl", "x509", "-in", str(keys / "os.crt"), "-pubkey", "-noout"])
    if public != certificate_public:
        raise ValueError("OS certificate does not match signing key")
    description = subprocess.check_output([
        "openssl", "rsa", "-pubin", "-text", "-noout"], input=public, text=False)
    if description.splitlines()[0] != b"Public-Key: (3072 bit)":
        raise ValueError("OS signing key must be RSA-3072")


def _prepare_rootfs(rootfs_image, output):
    root = output / "rootfs.verity"
    shutil.copyfile(rootfs_image, root)
    # Leave space for the SHA-256 tree in the existing 128 MiB slot.
    run("e2fsck", "-fy", root)
    run("resize2fs", root, "120M")
    with root.open("r+b") as stream:
        stream.truncate(120 * 1024 * 1024)
    for name in ("inittab", "fstab"):
        target = output / name
        run("debugfs", "-R", f"dump /etc/{name} {target}", root)
        text = target.read_text()
        if name == "inittab":
            text = text.replace("::sysinit:/bin/mount -o remount,rw /", "# Root remains read-only under dm-verity")
        else:
            text = re.sub(r"^/dev/root\s+/\s+\S+\s+\S+\s+\d+\s+\d+$",
                          "/dev/root / ext4 ro,noauto 0 0", text, flags=re.M)
            text += "tmpfs /var/lib tmpfs mode=0755,nosuid,nodev 0 0\n"
        target.write_text(text)
        run("debugfs", "-w", "-R", f"rm /etc/{name}", root)
        run("debugfs", "-w", "-R", f"write {target} /etc/{name}", root)
    run("e2fsck", "-fy", root)
    with root.open("rb") as stream:
        stream.seek(1024)
        superblock = stream.read(1024)
    if struct.unpack_from("<H", superblock, 56)[0] != 0xef53:
        raise ValueError("root filesystem is not ext4")
    data_block_size = 1024 << struct.unpack_from("<I", superblock, 24)[0]
    if data_block_size not in (1024, 2048, 4096):
        raise ValueError("unsupported ext4 block size")

    return root, data_block_size


def _create_verity_tree(veritysetup, root, data_block_size):
    # dm-verity exports this logical block size; ext4 cannot mount a larger one.
    hash_block_size = 4096
    data_size = root.stat().st_size
    blocks = data_size // data_block_size
    hash_start = data_size // hash_block_size
    salt = os.urandom(32).hex()
    report = run(veritysetup, "format", "--no-superblock", "--format=1",
                 f"--data-block-size={data_block_size}", f"--hash-block-size={hash_block_size}",
                 f"--data-blocks={blocks}", f"--hash-offset={data_size}",
                 f"--salt={salt}", root, root)
    roothash = next(line.split(":", 1)[1].strip() for line in report.splitlines()
                    if line.startswith("Root hash:"))
    if root.stat().st_size > 128 * 1024 * 1024:
        raise ValueError("rootfs plus verity tree exceeds slot")
    run(veritysetup, "verify", "--no-superblock", "--format=1",
        f"--data-block-size={data_block_size}", f"--hash-block-size={hash_block_size}",
        f"--data-blocks={blocks}", f"--hash-offset={data_size}",
        f"--salt={salt}", root, root, roothash)

    return dict(data_blocks=blocks, hash_start=hash_start, data_block_size=data_block_size,
                hash_block_size=hash_block_size, salt=salt, root_hash=roothash,
                data_size=data_size)


def _build_bootargs(verity):
    data_size = verity["data_size"]
    data_block_size = verity["data_block_size"]
    hash_block_size = verity["hash_block_size"]
    blocks = verity["data_blocks"]
    hash_start = verity["hash_start"]
    roothash = verity["root_hash"]
    salt = verity["salt"]
    return ('earlycon=uart8250,mmio32,0xfeb50000 console=ttyS2,1500000 '
            'root=/dev/dm-0 ro rootwait rootfstype=ext4 '
            'dm-mod.waitfor=/dev/mmcblk0p3 '
            f'dm-mod.create="vroot,,0,ro,0 {data_size // 512} verity 1 '
            f'/dev/mmcblk0p3 /dev/mmcblk0p3 {data_block_size} {hash_block_size} {blocks} {hash_start} '
            f'sha256 {roothash} {salt} 2 panic_on_corruption panic_on_error"')


def _write_fit_description(output):
    its = output / "linux.its"
    its.write_text(r'''/dts-v1/;
/ {
    description = "QRK3588 authenticated Linux and verity root";
    #address-cells = <1>;
    images {
        kernel {
            description = "Linux";
            data = /incbin/("Image");
            type = "kernel"; arch = "arm64"; os = "linux";
            compression = "none"; load = <0x01400000>; entry = <0x01400000>;
            hash { algo = "sha256"; };
        };
        fdt {
            description = "Linux DTB and authenticated boot parameters";
            data = /incbin/("linux.dtb");
            type = "flat_dt"; arch = "arm64"; compression = "none";
            hash { algo = "sha256"; };
        };
    };
    configurations {
        default = "conf-secure";
        conf-secure {
            kernel = "kernel"; fdt = "fdt";
            signature {
                algo = "sha256,rsa3072";
                key-name-hint = "os";
                sign-images = "kernel", "fdt";
            };
        };
    };
};
''')
    return its


def _build_signed_fit(args, output, bootargs):
    dtb = output / "linux.dtb"
    shutil.copyfile(args.dtb, dtb)
    run("fdtput", "-p", "-t", "s", dtb, "/chosen", "bootargs", bootargs)
    kernel = output / "Image"
    shutil.copyfile(args.kernel, kernel)
    its = _write_fit_description(output)
    control = output / "control.dtb"
    shutil.copyfile(args.control_dtb, control)
    fit = output / "linux.itb"
    run(args.mkimage, "-f", its, "-k", args.keys.resolve(), "-K", control,
        "-r", fit)
    run(args.mkimage.parent / "fit_check_sign", "-f", fit, "-k", control,
        "-c", "conf-secure")
    if run("fdtget", "-t", "s", control, "/signature/key-os", "required").strip() != "conf":
        raise ValueError("required OS configuration key was not embedded")

    return dtb, fit, control, kernel


def _assemble_bl33_with_control_dtb(output, uboot_nodtb, control_dtb):
    # ATF will authenticate this assembled BL33, including the required OS key.
    bl33 = output / "u-boot.bin"
    bl33.write_bytes(uboot_nodtb.read_bytes() + control_dtb.read_bytes())
    return bl33


def prepare(args):
    _verify_signing_key(args.keys)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)

    root, data_block_size = _prepare_rootfs(args.rootfs, output)
    verity = _create_verity_tree(args.veritysetup, root, data_block_size)
    bootargs = _build_bootargs(verity)
    dtb, fit, control, kernel = _build_signed_fit(args, output, bootargs)
    bl33 = _assemble_bl33_with_control_dtb(output, args.uboot_nodtb, control)

    metadata = dict(data_blocks=verity["data_blocks"], hash_start=verity["hash_start"],
                    data_block_size=data_block_size, hash_block_size=verity["hash_block_size"],
                    salt=verity["salt"], root_hash=verity["root_hash"], bootargs=bootargs,
                    sha256={p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                            for p in (root, dtb, fit, control, bl33, kernel)})
    (output / "manifest.json").write_text(json.dumps(metadata, indent=2) + "\n")
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("rootfs", "kernel", "dtb", "control-dtb", "uboot-nodtb",
                 "mkimage", "keys", "veritysetup", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    try:
        prepare(parser.parse_args())
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)
