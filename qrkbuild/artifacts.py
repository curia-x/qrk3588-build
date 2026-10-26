# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 ninozhang001@gmail.com
"""Artifact checks and reuse of the existing identity and publication tools."""
import hashlib
import os
from pathlib import Path
import shutil
import tempfile


def require(path):
    path = Path(path)
    if not path.is_file() or not os.access(path, os.R_OK) or path.stat().st_size == 0:
        raise ValueError(f"artifact is missing, unreadable or empty: {path}")
    return path


def publish_file(source, destination):
    require(source)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=destination.name + ".", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as target, Path(source).open("rb") as data:
            shutil.copyfileobj(data, target)
            target.flush()
            os.fsync(target.fileno())
            os.fchmod(target.fileno(), 0o644)
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)


def check_symbols(config, names, prefix="CONFIG_"):
    lines = set(Path(config).read_text().splitlines())
    for name in names:
        if f"{prefix}{name}=y" not in lines:
            raise ValueError(f"{prefix}{name} missing in {config}")


class Artifacts:
    def __init__(self, context):
        self.c = context

    def check_rootfs(self, image):
        image = require(image)
        size = image.stat().st_size
        if size > 128 * 1024 * 1024 or size % 512:
            raise ValueError("rootfs.ext4 must be <=128 MiB and 512-byte aligned")
        self.c.run(["e2fsck", "-fn", image], allowed=(0, 1))

    def signing_inputs(self, os_auth=False):
        keys = []
        if self.c.auth:
            keys = [(name, self.c.keys / f"{name}.pem")
                    for name in ("rot", "trusted-world", "non-trusted-world", "bl31", "bl33")]
        if os_auth:
            keys.append(("os", self.c.keys / "os.key"))
        return keys

    def begin(self, directory, *, inputs=(), keys=(), excludes=()):
        c = self.c
        directory.mkdir(parents=True, exist_ok=True)
        args = ["begin", "--state", directory / "build-inputs.json"]
        sources = [("build", c.build), ("tf-a", c.tf), ("u-boot", c.uboot),
                   ("linux", c.kernel), ("buildroot", c.buildroot)]
        if (c.apps / ".git").exists():
            sources.append(("apps", c.apps))
        for name, path in sources:
            args += ["--source", name, path]
        for path in [c.build / "out", c.tf / "out", c.tf / "build",
                     c.tf / "compile_commands.json", c.uboot / "build",
                     c.kernel / "build", c.kernel / "compile_commands.json",
                     c.buildroot / "output", c.ubout, c.kout, c.brout, *excludes]:
            args += ["--exclude", path]
        for name, path in inputs:
            args += ["--input", name, path]
        for name, path in keys:
            args += ["--key", name, path]
        c.tool("qrk3588_delivery.py", *args)

    def complete(self, directory, mode, *, target="all", ub=None, kernel=None, verity=""):
        args = ["complete", "--directory", directory, "--mode", mode,
                "--profile", "images" if ub is not None else "atf", "--target", target]
        if ub is not None:
            args += ["--uboot-output", ub, "--kernel-output", kernel, "--veritysetup", verity]
        self.c.tool("qrk3588_delivery.py", *args)

    def stage(self, destination):
        result = self.c.tool("qrk3588_delivery.py", "stage", destination, capture=True)
        return Path(os.fsdecode(result).strip())

    def publish(self, stage, destination):
        previous = self.c.tool("qrk3588_publish_directory.py", stage, destination, capture=True)
        if previous.strip():
            print("Previous image set retained: " + os.fsdecode(previous).strip(), flush=True)

    def pack(self, output, **components):
        args = ["--output", output]
        for name, path in components.items():
            args += ["--" + name, require(path)]
        self.c.tool("qrk3588_pack_upgrade.py", *args)
        return require(output)

    def checksums(self, stage):
        names = ["Image", "linux.dtb", "rootfs.ext4", "u-boot.bin", "bl1.bin",
                 "fip.bin", "complete.upg", "build-identity.json"]
        if self.c.os_auth:
            names += ["linux.itb", "rootfs.verity", "control.dtb", "manifest.json",
                      "linux.its", "inittab", "fstab"]
        lines = []
        for name in names:
            digest = hashlib.sha256()
            with require(stage / name).open("rb") as data:
                for block in iter(lambda: data.read(1024 * 1024), b""):
                    digest.update(block)
            lines.append(f"{digest.hexdigest()}  {name}\n")
        (stage / "artifacts.sha256").write_text("".join(lines))
        (stage / "build-inputs.json").unlink()
