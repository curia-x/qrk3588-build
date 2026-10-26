#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 ninozhang001@gmail.com
"""Create one self-contained QRK3588 upgrade image."""

import argparse
import hashlib
import os
from pathlib import Path
import struct
import tempfile


COMPONENTS = {"rootfs": 0, "kernel": 1, "dtb": 3, "fip": 2}
HEADER_SIZE = 512


def pack(output, inputs):
    if not 1 <= len(inputs) <= 4:
        raise ValueError("one to four components are required")
    entries = []
    offset = HEADER_SIZE
    for name, path in inputs:
        size = path.stat().st_size
        if size <= 0:
            raise ValueError(f"{name}: empty input")
        if name == "kernel" and size > 120 * 1024 * 1024:
            raise ValueError("kernel must be <=120 MiB")
        if name == "dtb" and size > 2 * 1024 * 1024:
            raise ValueError("DTB must be <=2 MiB")
        if name == "rootfs" and (size > 128 * 1024 * 1024 or size % 512):
            raise ValueError("rootfs must be <=128 MiB and 512-byte aligned")
        if name == "fip" and size > 20 * 1024 * 1024:
            raise ValueError("FIP must be <=20 MiB")
        entries.append((name, path, offset, size))
        offset += (size + 511) & ~511
    header = bytearray(HEADER_SIZE)
    struct.pack_into("<8sIIQ", header, 0, b"QRKUPG01", HEADER_SIZE,
                     len(entries), offset)
    header[24:38] = b"qrk3588:raw-v1"
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".qrk-upgrade-", dir=output.parent)
    try:
        with os.fdopen(fd, "w+b") as target:
            target.write(header)
            for index, (name, path, start, size) in enumerate(entries):
                digest = hashlib.sha256()
                copied = 0
                with path.open("rb") as source:
                    while block := source.read(1024 * 1024):
                        copied += len(block)
                        digest.update(block)
                        target.write(block)
                if copied != size:
                    raise ValueError(f"{name}: input size changed during packing")
                target.write(bytes((-size) % 512))
                struct.pack_into("<IIQQ32s", header, 64 + index * 56,
                                 COMPONENTS[name], 0, start, size, digest.digest())
            target.seek(0)
            target.write(header)
        os.replace(temporary, output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)



def validate(path):
    """Validate the emitted container and payload identities before transport.

    Board admission and signature/boot-policy enforcement remain separate.
    """
    path = Path(path)
    size = path.stat().st_size
    limits = {0: 128 * 1024 * 1024, 1: 120 * 1024 * 1024,
              2: 20 * 1024 * 1024, 3: 2 * 1024 * 1024}
    if size < HEADER_SIZE or size > 288 * 1024 * 1024:
        raise ValueError("invalid upgrade package size")
    with path.open("rb") as data:
        header = data.read(HEADER_SIZE)
        magic, header_size, count, total = struct.unpack_from("<8sIIQ", header)
        if (magic, header_size, total) != (b"QRKUPG01", HEADER_SIZE, size) or not 1 <= count <= 4:
            raise ValueError("invalid upgrade header")
        if header[24:38] != b"qrk3588:raw-v1" or any(header[38:64]) or any(header[64 + 56 * count:]):
            raise ValueError("invalid upgrade layout or reserved header")
        seen, ranges = set(), []
        for index in range(count):
            kind, flags, offset, length, expected = struct.unpack_from("<IIQQ32s", header, 64 + 56 * index)
            if kind not in limits or kind in seen or flags or not 0 < length <= limits[kind]:
                raise ValueError("invalid upgrade component")
            end = offset + ((length + 511) & ~511)
            if offset < HEADER_SIZE or offset % 512 or end > size or (kind == 0 and length % 512):
                raise ValueError("invalid upgrade component bounds")
            seen.add(kind)
            ranges.append((offset, end))
            data.seek(offset)
            digest, remaining = hashlib.sha256(), length
            while remaining:
                block = data.read(min(1024 * 1024, remaining))
                if not block:
                    raise ValueError("truncated upgrade payload")
                digest.update(block)
                remaining -= len(block)
            if digest.digest() != expected or any(data.read((-length) % 512)):
                raise ValueError("upgrade payload hash or padding mismatch")
        previous = HEADER_SIZE
        for begin, end in sorted(ranges):
            if begin != previous:
                raise ValueError("upgrade payload ranges overlap or leave gaps")
            previous = end
        if previous != size:
            raise ValueError("upgrade package has trailing bytes")
    return seen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--output", type=Path)
    operation.add_argument("--check", type=Path)
    for name in COMPONENTS:
        parser.add_argument(f"--{name}", type=Path)
    args = parser.parse_args()
    inputs = [(name, getattr(args, name)) for name in COMPONENTS
              if getattr(args, name) is not None]
    try:
        if args.check is not None:
            if inputs:
                raise ValueError("--check does not accept component inputs")
            validate(args.check)
        else:
            pack(args.output, inputs)
    except (ValueError, OSError) as error:
        parser.exit(1, f"error: {error}\n")


if __name__ == "__main__":
    main()
