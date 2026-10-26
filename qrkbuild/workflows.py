# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 ninozhang001@gmail.com
"""Explicit build/package/upgrade sequences. The caller holds one workspace lock."""
from pathlib import Path
import shutil

from .artifacts import Artifacts, require
from .components import Components
from .transport import Transport


class Workflows:
    def __init__(self, context, backend):
        self.c = context
        self.parts = Components(context)
        self.artifacts = Artifacts(context)
        self.transport = Transport(context, backend)

    def images(self, mode, rootfs=None):
        c, a, parts = self.c, self.artifacts, self.parts
        c.validate(mode, images=True)
        parts.check_keys(c.fit_signature)
        rootfs = Path(rootfs) if rootfs else None
        if rootfs is not None:
            a.check_rootfs(rootfs)
        destination = c.images / mode
        stage = a.stage(destination)
        ub = c.out / "u-boot/qrk3588-secure" / mode if c.fit_signature else c.ubout
        kernel = c.out / "linux/qrk3588-secure" / mode if c.fit_signature else c.kout
        try:
            parts.build_uboot(ub, c.fit_signature, mode=mode)
            parts.build_kernel(kernel, c.fit_signature)
            if rootfs is None:
                rootfs = parts.build_rootfs()
                a.check_rootfs(rootfs)
            for source, name in [(rootfs, "rootfs.ext4"),
                                 (kernel / "arch/arm64/boot/Image", "Image"),
                                 (kernel / "arch/arm64/boot/dts/quest/qrk3588.dtb", "linux.dtb"),
                                 (Path(c.env.get("QRK_BL33") or ub / "u-boot.bin"), "u-boot.bin")]:
                shutil.copyfile(require(source), stage / name)
            if c.fit_signature:
                c.tool("qrk3588_secure_os.py", "--kernel", kernel / "arch/arm64/boot/Image",
                       "--dtb", kernel / "arch/arm64/boot/dts/quest/qrk3588.dtb",
                       "--control-dtb", ub / "u-boot.dtb", "--uboot-nodtb", ub / "u-boot-nodtb.bin",
                       "--mkimage", ub / "tools/mkimage", "--keys", c.keys,
                       "--output", stage)
            native = parts.atf_compile(mode, "all", stage / "u-boot.bin")
            for name in ("bl1.bin", "fip.bin"):
                shutil.copyfile(require(native / name), stage / name)
            payloads = dict(rootfs=stage / "rootfs.ext4",
                            kernel=stage / ("linux.itb" if c.fit_signature else "Image"), fip=stage / "fip.bin")
            if not c.fit_signature:
                payloads["dtb"] = stage / "linux.dtb"
            a.pack(stage / "complete.upg", **payloads)
            a.checksums(stage)
            a.publish(stage, destination)
        except BaseException:
            print(f"Unpublished image stage retained: {stage}", flush=True)
            raise
        print(f"Image set: {destination}", flush=True)
        return destination / "complete.upg"

    def package(self, kind, mode, rootfs=None):
        c, parts, a = self.c, self.parts, self.artifacts
        c.validate(mode, partial=True)
        output = c.out / "upgrade" / (kind + ".upg")
        if kind in ("linux", "kernel", "dtb"):
            parts.build_kernel()
            payloads = {}
            if kind != "dtb":
                payloads["kernel"] = c.kout / "arch/arm64/boot/Image"
            if kind != "kernel":
                payloads["dtb"] = c.kout / "arch/arm64/boot/dts/quest/qrk3588.dtb"
        elif kind == "fip":
            parts.build_uboot()
            parts.atf(mode, "fip")
            payloads = dict(fip=c.fip)
        elif kind == "rootfs":
            rootfs = Path(rootfs) if rootfs else parts.build_rootfs()
            a.check_rootfs(rootfs)
            payloads = dict(rootfs=rootfs)
        else:
            raise ValueError("unknown package component")
        return a.pack(output, **payloads)

    def upgrade(self, kind, mode, rootfs=None):
        # A failed prepare never reaches this transport. No selection by mtime.
        image = self.images(mode, rootfs) if kind == "complete" else self.package(kind, mode, rootfs)
        self.transport.transfer(image)
        return image

    def run_fip(self, mode):
        parts, c = self.parts, self.c
        parts.atf(mode, "bl1")
        parts.build_uboot()
        parts.atf(mode, "fip")
        require(c.fip)
        require(c.bl1)
        if shutil.which("sx", path=c.env.get("PATH")) is None:
            raise ValueError("sx is required")
        self.transport.maskrom(c.bl1)
        self.transport.send_fip(c.fip)
