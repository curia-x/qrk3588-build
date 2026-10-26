#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 ninozhang001@gmail.com
"""Prepare a signed Linux FIT with authenticated boot parameters."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys


def run(*args):
    result = subprocess.run([str(a) for a in args], text=True,
                            stdout=subprocess.PIPE, check=False)
    print(result.stdout, end="", flush=True)
    if result.returncode:
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


def _build_bootargs():
    return ('earlycon=uart8250,mmio32,0xfeb50000 console=ttyS2,1500000 '
            'root=/dev/mmcblk0p3 rw rootwait rootfstype=ext4')


def _write_fit_description(output):
    its = output / "linux.its"
    its.write_text(r'''/dts-v1/;
/ {
    description = "QRK3588 authenticated Linux";
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

    bootargs = _build_bootargs()
    dtb, fit, control, kernel = _build_signed_fit(args, output, bootargs)
    bl33 = _assemble_bl33_with_control_dtb(output, args.uboot_nodtb, control)

    metadata = dict(bootargs=bootargs, rootfs_authenticated=False,
                    sha256={p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                            for p in (dtb, fit, control, bl33, kernel)})
    (output / "manifest.json").write_text(json.dumps(metadata, indent=2) + "\n")
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("kernel", "dtb", "control-dtb", "uboot-nodtb",
                 "mkimage", "keys", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    try:
        prepare(parser.parse_args())
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)
