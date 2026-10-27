#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 ninozhang001@gmail.com
"""Capture build inputs and verify them before publishing firmware or image sets."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import uuid


def digest_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def command(args):
    return subprocess.check_output([str(a) for a in args], stderr=subprocess.PIPE)


def excluded(path, exclusions):
    if any(part in ("keys", ".aws", ".ssh") for part in path.parts):
        return True
    if path.suffix.lower() in (".pem", ".key", ".p12", ".pfx"):
        return True
    absolute = Path(os.path.abspath(path))
    return any(absolute == item or item in absolute.parents for item in exclusions)


def source_identity(directory, exclusions):
    directory = Path(directory).resolve()
    records = command(["git", "-C", directory, "status", "--porcelain=v1", "-z", "--untracked-files=all"]).split(b"\0")
    changes = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        status = record[:2].decode("ascii")
        relative = os.fsdecode(record[3:])
        original = None
        if "R" in status or "C" in status:
            original = os.fsdecode(records[index])
            index += 1
        path = directory / relative
        if excluded(path, exclusions):
            continue
        entry = dict(path=relative, status=status)
        if original and not excluded(directory / original, exclusions):
            entry["original_path"] = original
        if path.is_symlink():
            entry["symlink_sha256"] = hashlib.sha256(os.fsencode(os.readlink(path))).hexdigest()
        elif path.is_file():
            entry["sha256"] = digest_file(path)
        elif path.is_dir():
            # Hash dirty dependency content too; status alone misses subsequent edits.
            entry["identity"] = source_identity(path, exclusions)
        changes.append(entry)
    changes.sort(key=lambda item: item["path"])
    encoded = json.dumps(changes, sort_keys=True).encode()
    return dict(path=str(directory), head=command(["git", "-C", directory, "rev-parse", "HEAD"]).decode().strip(),
                dirty=bool(changes), dirty_sha256=hashlib.sha256(encoded).hexdigest(), changes=changes,
                submodules=command(["git", "-C", directory, "submodule", "status", "--recursive"]).decode().splitlines(),
                key_paths_excluded=True)


def public_key_identity(path):
    # OpenSSL loads a signing key, but only its public DER enters this process.
    public = command(["openssl", "pkey", "-passin", "pass:", "-in", path, "-pubout", "-outform", "DER"])
    return dict(public_der_sha256=hashlib.sha256(public).hexdigest())


def read_inputs(inputs):
    return {name: dict(path=os.path.abspath(path), resolved_path=str(Path(path).resolve()), sha256=digest_file(path)) for name, path in inputs}


def write_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def begin(args):
    exclusions = [Path(os.path.abspath(path)) for path in args.exclude]
    exclusions.extend(Path(path).resolve().parent for _, path in args.key)
    state = dict(schema=1, sources={name: source_identity(path, exclusions) for name, path in args.source},
                 inputs=read_inputs(args.input),
                 public_keys={name: public_key_identity(path) for name, path in args.key},
                 key_references={name: str(Path(path).resolve()) for name, path in args.key},
                 exclusions=[str(path) for path in exclusions])
    write_json(args.state, state)


def tool_identity(executable):
    resolved = shutil.which(executable)
    if not resolved:
        raise ValueError(f"required build tool not found: {executable}")
    path = Path(resolved).resolve()
    name = path.name
    version_args = {"openssl": ["version"], "mkimage": ["-V"], "e2fsck": ["-V"],
                    "resize2fs": ["-V"], "debugfs": ["-V"], "fit_check_sign": []}.get(name, ["--version"])
    metadata = dict(path=str(path), sha256=digest_file(path))
    if version_args:
        result = subprocess.run([resolved, *version_args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
        metadata.update(version=result.stdout.decode(errors="replace")[:4096].strip(), version_status=result.returncode)
    return metadata


def finish(args):
    state = json.loads(args.state.read_text())
    exclusions = [Path(path) for path in state["exclusions"]]
    current = {name: source_identity(source["path"], exclusions) for name, source in state["sources"].items()}
    if current != state["sources"]:
        differences = []
        for name, before in state["sources"].items():
            after = current[name]
            if before == after:
                continue
            fields = [field for field in ("head", "submodules") if before[field] != after[field]]
            old = {item["path"]: item for item in before["changes"]}
            new = {item["path"]: item for item in after["changes"]}
            paths = sorted(path for path in old.keys() | new.keys() if old.get(path) != new.get(path))
            differences.append(f"{name}: " + ", ".join(fields + paths))
        raise ValueError("source tree changed during the build; staged artifacts are not published\n"
                         + "\n".join(differences))
    if read_inputs([(name, item["path"]) for name, item in state["inputs"].items()]) != state["inputs"]:
        raise ValueError("input image changed during the build; staged artifacts are not published")
    public = {name: public_key_identity(path) for name, path in state["key_references"].items()}
    if public != state["public_keys"]:
        raise ValueError("signing key changed during the build; staged artifacts are not published")
    configurations = {}
    for name, path in args.config:
        configurations[name] = dict(path=str(Path(path).resolve()), sha256=digest_file(path))
    for name, path in args.optional_config:
        path = Path(path)
        configurations[name] = dict(path=str(path.resolve()), sha256=digest_file(path)) if path.is_file() else dict(path=str(path.resolve()), status="not present")
    identity = dict(schema=1, profile=args.profile, mode=args.mode, completed_utc=datetime.now(timezone.utc).isoformat(),
                    sources=state["sources"], inputs=state["inputs"], public_keys=public,
                    configurations=configurations, tools={name: tool_identity(executable) for name, executable in args.tool},
                    options=dict(args.option), source_date_epoch=os.environ.get("SOURCE_DATE_EPOCH"),
                    byte_reproducible=False, reproducibility_note="verity salt and signing timestamps are not normalized")
    write_json(args.output, identity)


def complete(args):
    """Record the actual configuration/tools for standalone ATF or any image set."""
    directory = args.directory
    state = directory / "build-inputs.json"
    inputs = json.loads(state.read_text())
    image_set = args.profile == "images"
    auth = os.environ.get("QRK_ATF_SECURE_BOOT", "1") if image_set else "1"
    os_auth = image_set and os.environ.get("QRK_SECURE_OS", "0") == "1"
    if image_set:
        if args.uboot_output is None or args.kernel_output is None:
            raise ValueError("images completion requires U-Boot/kernel outputs")
        if os_auth and not args.veritysetup:
            raise ValueError("OS authentication completion requires the verity tool")
        tf_source = inputs["sources"].get("tf-a")
        if tf_source is None:
            raise ValueError("build input state has no TF-A source identity")
        platform = "qrk3588-secure" if auth == "1" else "qrk3588"
        tf = Path(tf_source["path"]) / "build" / platform / args.mode
    else:
        tf = directory

    config = {"tf-a": tf / "config.h"}
    optional = {}
    tools = {
        "tf-gcc": (os.environ.get("TF_CROSS_COMPILE") or os.environ.get("CROSS_COMPILE") or "aarch64-linux-gnu-") + "gcc",
        "make": "make", "python": "python3",
    }
    options = dict(jobs=os.environ.get("QRK_BUILD_JOBS") or "4", PLAT="qrk3588",
                   DEBUG="1" if args.mode == "debug" else "0",
                   TRUSTED_BOARD_BOOT=auth, GENERATE_COT=auth)
    if auth == "1":
        config["tf-root-public-key"] = tf / "qrk3588_rotpk.h"
        tools["openssl"] = "openssl"
        options.update(COT="tbbr", KEY_ALG="rsa", KEY_SIZE="3072", HASH_ALG="sha256",
                       TFW_NVCTR_VAL="0", NTFW_NVCTR_VAL="0")
    if image_set or args.target != "bl1":
        tools["fiptool"] = tf / "tools/fiptool/fiptool"
        if auth == "1":
            tools["cert-create"] = tf / "tools/cert_create/cert_create"
    if image_set:
        ub, kernel = args.uboot_output, args.kernel_output
        config.update({"u-boot": ub / ".config", "linux": kernel / ".config"})
        buildroot = os.environ.get("BUILDROOT_OUT")
        if not buildroot:
            raise ValueError("BUILDROOT_OUT is required for images completion")
        optional["buildroot"] = Path(buildroot) / ".config"
        options["QRK_SECURE_OS"] = "1" if os_auth else "0"
        tools.update({
            "uboot-gcc": (os.environ.get("UBOOT_CROSS_COMPILE") or "aarch64-linux-gnu-") + "gcc",
            "kernel-gcc": (os.environ.get("KERNEL_CROSS_COMPILE") or "aarch64-linux-gnu-") + "gcc",
            "e2fsck": "e2fsck",
        })
        if os_auth:
            tools.update({
                **{name: name for name in ("dtc", "fdtget", "fdtput", "resize2fs", "debugfs")},
                "veritysetup": args.veritysetup, "mkimage": ub / "tools/mkimage",
                "fit-check-sign": ub / "tools/fit_check_sign",
            })
    else:
        options["target"] = args.target
    finish(SimpleNamespace(state=state, output=directory / "build-identity.json",
                           profile="images" if image_set else "secureboot", mode=args.mode,
                           config=list(config.items()), optional_config=list(optional.items()),
                           tool=list(tools.items()), option=list(options.items())))


def stage(args):
    destination = Path(os.path.abspath(args.destination))
    if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
        raise ValueError("image output must be a directory, not a symlink")
    destination.parent.mkdir(parents=True, exist_ok=True)
    parent = destination.parent.resolve()
    staged = tempfile.mkdtemp(prefix=f".{destination.name}.stage-", dir=parent)
    # Published artifacts contain public keys and images, never signing keys.
    os.chmod(staged, 0o755)
    print(staged)


def invalidate(args):
    path = args.path
    if path.is_symlink():
        raise ValueError("success identity must not be a symlink")
    if path.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        previous = path.with_name(f".{path.stem}.previous-{stamp}-{uuid.uuid4().hex}{path.suffix}")
        path.rename(previous)
        print(f"Previous build identity retained: {previous}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("begin")
    start.add_argument("--state", type=Path, required=True)
    for name in ("source", "input", "key"):
        start.add_argument("--" + name, nargs=2, action="append", default=[])
    start.add_argument("--exclude", action="append", default=[])
    end = commands.add_parser("finish")
    end.add_argument("--state", type=Path, required=True)
    end.add_argument("--output", type=Path, required=True)
    end.add_argument("--profile", required=True)
    end.add_argument("--mode", required=True)
    for name in ("config", "tool", "option"):
        end.add_argument("--" + name, nargs=2, action="append", default=[])
    end.add_argument("--optional-config", nargs=2, action="append", default=[])
    profile = commands.add_parser("complete", help="finish a standalone ATF build or image set")
    profile.add_argument("--profile", choices=("atf", "images"), required=True)
    profile.add_argument("--directory", type=Path, required=True)
    profile.add_argument("--mode", choices=("debug", "release"), required=True)
    profile.add_argument("--target", choices=("bl1", "fip", "all"), default="all")
    profile.add_argument("--uboot-output", type=Path)
    profile.add_argument("--kernel-output", type=Path)
    profile.add_argument("--veritysetup")
    invalidation = commands.add_parser("invalidate")
    invalidation.add_argument("path", type=Path)
    staging = commands.add_parser("stage")
    staging.add_argument("destination", type=Path)
    args = parser.parse_args()
    try:
        {"begin": begin, "finish": finish, "complete": complete, "stage": stage, "invalidate": invalidate}[args.command](args)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
