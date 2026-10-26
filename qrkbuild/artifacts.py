# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 ninozhang001@gmail.com
"""Artifact validation, staging and atomic publication."""
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

    def stage(self, destination):
        destination = Path(os.path.abspath(destination))
        if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
            raise ValueError("image output must be a directory, not a symlink")
        destination.parent.mkdir(parents=True, exist_ok=True)
        staged = Path(tempfile.mkdtemp(prefix=f".{destination.name}.stage-",
                                      dir=destination.parent.resolve()))
        staged.chmod(0o755)
        return staged

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
                 "fip.bin", "complete.upg"]
        if self.c.fit_signature:
            names += ["linux.itb", "control.dtb", "manifest.json",
                      "linux.its"]
        lines = []
        for name in names:
            digest = hashlib.sha256()
            with require(stage / name).open("rb") as data:
                for block in iter(lambda: data.read(1024 * 1024), b""):
                    digest.update(block)
            lines.append(f"{digest.hexdigest()}  {name}\n")
        (stage / "artifacts.sha256").write_text("".join(lines))
