# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 ninozhang001@gmail.com
"""Invalidate only the local guest package; Buildroot owns its dependencies."""
import json
from pathlib import Path

from tools.qrk3588_delivery import digest_file, source_identity, write_json
from .artifacts import require

PACKAGE = "quest_os"


def inputs(c):
    source = c.quest / "QuestOS"
    excluded = [source / name for name in
                ("build", "include/asm/generated", "thirdparty/tf-a", "third_party/optee_os")]
    identity = source_identity(source, excluded)
    # Unused guest TF-A/OP-TEE trees do not affect this raw guest image.
    identity["submodules"] = [line for line in identity["submodules"]
                             if line.split()[1] not in ("thirdparty/tf-a", "third_party/optee_os")
                             and not line.split()[1].startswith(("thirdparty/tf-a/", "third_party/optee_os/"))]
    files = [c.brout / ".config", c.quest / "quest-os.mk", c.quest / "Config.in",
             c.quest / "quest_os.sh", c.quest / "configs/rootfs.fragment",
             c.apps / "external.mk",
             c.toolchain / "gcc/bin" / (c.toolchain_prefix + "-gcc")]
    return dict(schema=1, source=identity,
                files={str(path): digest_file(require(path)) for path in files})


def outputs(directory, target):
    return [directory / "build/quest_os_pack_all.bin",
            target / "usr/share/quest_os/quest_os.bin", target / "usr/bin/quest_os",
            target / "usr/share/quest_os/licenses/LICENSE",
            target / "usr/share/quest_os/licenses/THIRD_PARTY_NOTICES.md"]


def update(parts, action):
    c = parts.c
    state = c.brout / ".qrkbuild-quest-os.json"
    if action == "clean":
        state.unlink(missing_ok=True)
        parts.make("rootfs", PACKAGE + "-dirclean")
        return
    if action not in ("build", "install"):
        raise ValueError("unknown Quest OS action")
    report = parts.make("rootfs", "-s", "--no-print-directory", "printvars",
                        "VARS=QUEST_OS_DIR TARGET_DIR", capture=True).decode()
    variables = dict(line.split("=", 1) for line in report.splitlines() if "=" in line)
    directory, target = Path(variables["QUEST_OS_DIR"]), Path(variables["TARGET_DIR"])
    current = inputs(c)
    try:
        previous = json.loads(state.read_text())
    except (OSError, ValueError):
        previous = {}
    artifacts = outputs(directory, target)
    stamps = [directory / ".stamp_built", directory / ".stamp_target_installed"]
    reusable = (isinstance(previous, dict) and previous.get("inputs") == current
                and all(path.is_file() for path in stamps + artifacts)
                and previous.get("outputs") == {str(path): digest_file(path) for path in artifacts})
    if not reusable:
        # A partial/failed build must never validate an older cache record.
        state.unlink(missing_ok=True)
        # A clean package also handles deletions, older source mtimes and changed
        # flags that the guest's own Make rules do not fully track.
        if directory.exists():
            parts.make("rootfs", PACKAGE + "-dirclean")
        print("Quest OS inputs or outputs changed; rebuilding the local package.", flush=True)
    else:
        print("Quest OS inputs unchanged; reusing the built package.", flush=True)
    parts.make("rootfs", "-j" + c.jobs,
               PACKAGE + "-reinstall" if reusable and action == "install" else PACKAGE)
    if inputs(c) != current:
        state.unlink(missing_ok=True)
        raise ValueError("Quest OS inputs changed during the build; retry with stable inputs")
    if not all(path.is_file() for path in stamps):
        raise ValueError("Quest OS build did not produce its native Buildroot stamps")
    write_json(state, dict(inputs=current,
                           outputs={str(path): digest_file(require(path)) for path in artifacts}))
