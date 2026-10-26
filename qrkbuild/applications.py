# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 ninozhang001@gmail.com
"""Application-owned configuration and native Buildroot package operations.

apps/*/app.json declares the package, selection symbol, optional fragments and
expected outputs. Only applications enabled in the rootfs .config contribute
requirements to normal component builds.
"""
import json
from pathlib import Path

from .artifacts import require


def declarations(context):
    apps = []
    for manifest in sorted(context.apps.glob("*/app.json")):
        app = json.loads(manifest.read_text())
        if not isinstance(app, dict) or not all(isinstance(app.get(key), str) and app[key]
                                               for key in ("config", "package")):
            raise ValueError(f"{manifest} requires config and package names")
        app["name"] = manifest.parent.name
        app["directory"] = Path(context.env.get(app.get("directory_env", "")) or manifest.parent).expanduser().absolute()
        apps.append(app)
    return apps


def selected(context, output=None):
    enabled = set(((output or context.brout) / ".config").read_text().splitlines())
    return [app for app in declarations(context) if app["config"] + "=y" in enabled]


def configure(parts, component, output, apps):
    fragments = [require(app["directory"] / app["fragments"][component])
                 for app in apps if component in app.get("fragments", {})]
    if not fragments:
        return
    c = parts.c
    source = c.kernel if component == "kernel" else c.buildroot
    script = source / ("scripts/kconfig/merge_config.sh" if component == "kernel" else "support/kconfig/merge_config.sh")
    env = dict(c.env)
    if component == "rootfs":
        env.update(CONFIG_="BR2_", KCONFIG_CONFIG=str(output / ".config"))

    # Merge all application fragments into the component's .config.
    c.run([script, "-m", "-O", output, output / ".config", *fragments], env=env)
    parts.make(component, "olddefconfig", output=output)
    actual = set((output / ".config").read_text().splitlines())

    # Verify that all application requirements are present in the final .config.
    for fragment in fragments:
        for line in fragment.read_text().splitlines():
            line = line.strip()
            if line.startswith(("CONFIG_", "BR2_")) or (line.startswith("# ") and line.endswith(" is not set")):
                if line not in actual:
                    raise ValueError(f"application requirement {line} from {fragment} missing in {output / '.config'}")


def clean(parts, apps):
    for app in apps:
        parts.make("rootfs", app["package"] + "-dirclean")


def build(parts, apps):
    if not apps:
        return

    # Clean all local sources before Buildroot orders package dependencies.
    clean(parts, apps)

    parts.make("rootfs", "-j" + parts.c.jobs, *(app["package"] for app in apps))
    for app in apps:
        check_outputs(parts, app)


def check_outputs(parts, app):
    variable = app["package"].upper().replace("-", "_") + "_DIR"
    report = parts.make("rootfs", "-s", "--no-print-directory", "printvars",
                        "VARS=" + variable + " TARGET_DIR", capture=True).decode()
    paths = dict(line.split("=", 1) for line in report.splitlines() if "=" in line)
    for name in app.get("build_outputs", []):
        require(Path(paths[variable]) / name)
    for name in app.get("target_outputs", []):
        require(Path(paths["TARGET_DIR"]) / name)
