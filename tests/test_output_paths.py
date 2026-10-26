#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Exercise build/publication routing without running cross compilers or a board."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

BUILD = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUILD))
from qrkbuild.context import Context
from qrkbuild.workflows import Workflows

spec = importlib.util.spec_from_file_location("build_cli", BUILD / "build.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


class OutputPathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def context(self, **env):
        return Context(BUILD, dict(ROOT_DIR=str(self.root), TOOLS_DIR=str(BUILD / "tools"),
                                   PATH=os.environ["PATH"], QRK_BUILD_JOBS="1", **env))

    def write(self, path, data=b"test output"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def test_complete_builds_publish_under_out(self):
        for signature, mode in ((signature, mode) for signature in ("0", "1")
                                for mode in ("release", "debug")):
            with self.subTest(signature=signature, mode=mode):
                c = self.context(QRK_FIT_SIGNATURE=signature)
                w = Workflows(c, BUILD / "scripts/qrk3588_transport.sh")
                c.kernel.mkdir(exist_ok=True)
                self.write(c.uboot / "configs/qrk3588_secure.config")
                self.write(c.uboot / "configs/qrk3588_secure_debug.config")
                self.write(c.kernel / "arch/arm64/configs/qrk3588_secure.config")
                real_run, real_tool = c.run, c.tool
                native_outputs = []

                def run(argv, **kwargs):
                    args = [str(arg) for arg in argv]
                    if args[0] == "e2fsck" or args[0].endswith("/merge_config.sh"):
                        return 0
                    if args[0] != "make":
                        return real_run(argv, **kwargs)
                    option = next(arg for arg in args if arg.startswith(("O=", "BUILD_PLAT=")))
                    out = Path(option.split("=", 1)[1])
                    self.assertTrue(out.is_relative_to(c.out), args)
                    native_outputs.append(out)
                    environment = (b"CONFIG_ENV_IS_IN_MMC=y\nCONFIG_ENV_REDUNDANT=y\n"
                                   if signature == "1" and mode == "debug"
                                   else b"CONFIG_ENV_IS_NOWHERE=y\n")
                    self.write(out / ".config", b"CONFIG_KVM=y\nCONFIG_CMDLINE=y\n"
                               b"CONFIG_CMD_BOOTM=y\nCONFIG_CMD_BOOTI=y\n"
                               b"CONFIG_QRK3588_FIT_SIGNATURE=y\n" + environment)
                    source = Path(args[args.index("-C") + 1])
                    names = {
                        c.uboot: ["u-boot.bin"],
                        c.kernel: ["arch/arm64/boot/Image", "arch/arm64/boot/dts/quest/qrk3588.dtb",
                                   "compile_commands.json"],
                        c.buildroot: ["images/rootfs.ext4", "images/rootfs.tar"],
                        c.tf: ["bl1.bin", "bl2.bin", "bl31.bin", "fip.bin"],
                    }[source]
                    for name in names:
                        self.write(out / name, bytes(512))
                    return 0

                def tool(name, *args, **kwargs):
                    if name != "qrk3588_secure_os.py":
                        return real_tool(name, *args, **kwargs)
                    stage = args[args.index("--output") + 1]
                    for name in ("linux.itb", "control.dtb", "manifest.json", "linux.its"):
                        self.write(stage / name)
                    return 0

                with patch.object(c, "run", side_effect=run), \
                        patch.object(c, "tool", side_effect=tool), \
                        patch.object(w.parts, "check_keys"):
                    with c.locked():
                        package = w.images(mode)
                self.assertEqual(package, self.root / "out/images" / mode / "complete.upg")
                real_tool("qrk3588_pack_upgrade.py", "--check", package)
                self.assertTrue((c.out / "rootfs.ext4").is_file())
                self.assertTrue((c.out / "rootfs.tar").is_file())
                self.assertTrue(any(p.is_relative_to(c.out / "arm-trusted-firmware")
                                    for p in native_outputs))
                self.assertEqual((c.kernel / "compile_commands.json").resolve(),
                                 (c.out / (f"linux/qrk3588-secure/{mode}" if signature == "1"
                                           else "linux/qrk3588") / "compile_commands.json"))
                self.assertFalse((c.build / "out").exists())

    def test_existing_rootfs_packaging_uses_relocated_image(self):
        c = self.context()
        w = Workflows(c, BUILD / "scripts/qrk3588_transport.sh")
        self.write(c.out / "rootfs.ext4", bytes(512))
        with patch.object(w.artifacts, "check_rootfs"):
            cli.dispatch(c, w, "pack_rootfs_upgrade_package", [])
        package = c.out / "upgrade/rootfs.upg"
        c.tool("qrk3588_pack_upgrade.py", "--check", package)
        self.assertFalse((self.root / "build/out").exists())

    def test_run_bl1_downloads_relocated_rom(self):
        c = self.context()
        w = Workflows(c, BUILD / "scripts/qrk3588_transport.sh")
        with patch.object(w.parts, "atf"), patch.object(w.transport, "maskrom") as maskrom:
            cli.dispatch(c, w, "run_bl1", [])
        maskrom.assert_called_once_with(self.root / "out/qrk3588_bl1.bin")

    def test_output_overrides_and_toolchain_selection(self):
        c = self.context(OUT_DIR=str(self.root / "custom"),
                         KERNEL_OUT=str(self.root / "kernel-custom"))
        self.assertEqual(c.kout, self.root / "kernel-custom")
        self.assertTrue(c.brout.is_relative_to(c.out))
        self.assertTrue(c.toolchain_out.is_relative_to(c.out))
        self.assertEqual(c.toolchain, self.root / "toolchain")
        self.assertFalse(c.toolchain.exists())
        c = self.context(TOOLCHAIN_DIR=str(self.root / "custom-toolchain"))
        self.assertEqual(c.toolchain, self.root / "custom-toolchain")


if __name__ == "__main__":
    unittest.main()
