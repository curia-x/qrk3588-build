#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 ninozhang001@gmail.com
"""Atomically publish a sibling directory while retaining the replaced tree."""
import argparse
import ctypes
from datetime import datetime, timezone
import fcntl
import os
from pathlib import Path
import stat
import sys
import uuid


def sync_tree(directory):
    # Do not follow links or open devices in an extracted root filesystem.
    for parent, _, files in os.walk(directory):
        for name in files:
            item = Path(parent) / name
            if stat.S_ISREG(item.lstat().st_mode):
                with item.open("rb") as stream:
                    os.fsync(stream.fileno())
    for parent, _, _ in os.walk(directory, topdown=False):
        sync_directory(Path(parent))


def sync_directory(directory):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def rename_atomic(source, destination, flags):
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        renameat2 = libc.renameat2
    except AttributeError as error:
        raise OSError("host libc does not provide renameat2; current directory is preserved") from error
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    if renameat2(-100, os.fsencode(source), -100, os.fsencode(destination), flags):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), str(destination))


def publish(staged, destination):
    staged = Path(os.path.abspath(staged))
    destination = Path(os.path.abspath(destination))
    if staged.is_symlink() or destination.is_symlink():
        raise ValueError("staged and destination paths must not be symlinks")
    staged = staged.parent.resolve() / staged.name
    destination = destination.parent.resolve() / destination.name
    if staged == destination or staged in destination.parents or destination in staged.parents:
        raise ValueError("staged and destination paths must not overlap")
    if staged.parent != destination.parent:
        raise ValueError("staged directory must be a sibling of the destination")
    if not staged.is_dir():
        raise ValueError("staged path must be an existing directory")
    if destination.exists() and not destination.is_dir():
        raise ValueError("destination must be a directory")
    lock = destination.parent / f".{destination.name}.publish.lock"
    # O_NOFOLLOW also prevents an unexpected link from redirecting the lock.
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
            raise ValueError("destination changed to a symlink or non-directory")
        sync_tree(staged)
        if not destination.exists():
            rename_atomic(staged, destination, 1)  # RENAME_NOREPLACE
            previous = None
        else:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            previous = destination.parent / f".{destination.name}.previous-{stamp}-{uuid.uuid4().hex}"
            rename_atomic(staged, destination, 2)  # RENAME_EXCHANGE
            # The old tree is already safe at staged, even if archival rename fails.
            try:
                rename_atomic(staged, previous, 1)
            except OSError as error:
                print(f"warning: previous directory retained at {staged}: {error}", file=sys.stderr)
                previous = staged
        try:
            sync_directory(destination.parent)
        except OSError as error:
            # Publication already succeeded; preserve its truthful success status.
            print(f"warning: published directory needs parent sync: {error}", file=sys.stderr)
        return previous
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("staged", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    try:
        previous = publish(args.staged, args.destination)
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(previous or "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
