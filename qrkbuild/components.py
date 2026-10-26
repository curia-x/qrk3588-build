# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 ninozhang001@gmail.com
"""Component-specific native Make/Kconfig contracts."""
import os
from pathlib import Path
import shutil
import tempfile

from .artifacts import check_symbols, publish_file, require


class Components:
    def __init__(self, context):
        self.c = context

    def make(self, component, *args, output=None, interactive=False, capture=False):
        c = self.c
        source, default = {
            "uboot": (c.uboot, c.ubout), "kernel": (c.kernel, c.kout),
            "rootfs": (c.buildroot, c.brout),
        }[component]
        options = ["O=" + str(output or default)]
        if component == "rootfs":
            options += ["BR2_EXTERNAL=" + str(c.apps),
                        "BR2_TOOLCHAIN_EXTERNAL_PATH=" + str(c.toolchain / "gcc"),
                        "BR2_TOOLCHAIN_EXTERNAL_CUSTOM_PREFIX=" + c.toolchain_prefix,
                        "GNU_TARGET_NAME=" + c.toolchain_prefix]
        else:
            prefix = c.env["UBOOT_CROSS_COMPILE" if component == "uboot" else "KERNEL_CROSS_COMPILE"]
            options += ["ARCH=" + ("arm" if component == "uboot" else "arm64"), "CROSS_COMPILE=" + prefix]
        return self.c.run(["make", "-C", source, *options, *args], interactive=interactive, capture=capture)

    def guard_kernel(self):
        if any((self.c.kernel / name).exists() for name in (".config", "include/config/auto.conf")):
            raise ValueError("in-tree Linux build state prevents O= builds; preserve it and clean explicitly")

    def _prepare_config(self, component, output=None, fragment=None):
        """Prepare native base/profile settings without application fragments."""
        c = self.c
        out = output or {"uboot": c.ubout, "kernel": c.kout, "rootfs": c.brout}[component]
        if component == "kernel":
            self.guard_kernel()
        if fragment is not None:
            fragments = fragment if isinstance(fragment, (list, tuple)) else [fragment]
            fragments = [require(item) for item in fragments]
            self.make(component, "qrk3588_defconfig", output=out)
            source = {"uboot": c.uboot, "kernel": c.kernel}[component]
            self.c.run([source / "scripts/kconfig/merge_config.sh", "-m", "-O",
                        out, out / ".config", *fragments])
        self.make(component, "olddefconfig" if (out / ".config").is_file() else "qrk3588_defconfig", output=out)
        return out

    def prepare(self, component, output=None, fragment=None):
        """Prepare the component's final configuration, including application requirements."""
        out = self._prepare_config(component, output, fragment)
        if component in ("kernel", "rootfs"):
            from . import applications
            rootfs = out if component == "rootfs" else self._prepare_config("rootfs")
            apps = applications.selected(self.c, rootfs)
            applications.configure(self, component, out, apps)
        return out

    def config(self, component, action):
        out = {"uboot": self.c.ubout, "kernel": self.c.kout, "rootfs": self.c.brout}[component]
        if action == "defconfig":
            if component == "kernel":
                self.guard_kernel()
            self.make(component, "qrk3588_defconfig")
        elif action == "menuconfig":
            self.prepare(component)
            self.make(component, "menuconfig", interactive=True)
            args = ["savedefconfig"]
            if component == "rootfs":
                args += ["BR2_DEFCONFIG=" + str(out / "defconfig")]
            self.make(component, *args)
        elif action == "clean":
            self.make(component, "clean" if component == "rootfs" else "mrproper")
        else:
            self.prepare(component)

    def build_uboot(self, output=None, secure=False, mode=None):
        out = output or self.c.ubout
        mode = mode or self.c.mode
        self.c.validate(mode)
        fragment = None
        if secure:
            fragment = [self.c.uboot / "configs/qrk3588_secure.config"]
            if mode == "debug":
                fragment.append(self.c.uboot / "configs/qrk3588_secure_debug.config")
        self.prepare("uboot", out, fragment)
        self.make("uboot", "-j" + self.c.jobs, output=out)
        require(out / "u-boot.bin")
        if secure:
            environment = ["ENV_IS_IN_MMC", "ENV_REDUNDANT"] if mode == "debug" else ["ENV_IS_NOWHERE"]
            check_symbols(out / ".config", ["CMDLINE", "CMD_BOOTM", "CMD_BOOTI",
                                            "QRK3588_FIT_SIGNATURE", *environment])
        return out

    def build_kernel(self, output=None, secure=False):
        out = output or self.c.kout
        fragment = self.c.kernel / "arch/arm64/configs/qrk3588_secure.config" if secure else None
        self.prepare("kernel", out, fragment)
        self.make("kernel", "-j" + self.c.jobs, "Image", "dtbs", output=out)
        require(out / "arch/arm64/boot/Image")
        require(out / "arch/arm64/boot/dts/quest/qrk3588.dtb")
        self.compile_commands(out)
        return out

    def compile_commands(self, output=None):
        c = self.c
        out = output or c.kout
        self.make("kernel", "compile_commands.json", output=out)
        link = c.kernel / "compile_commands.json"
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(out / "compile_commands.json")

    def application(self, name, action="build"):
        from . import applications
        if action not in ("build", "clean"):
            raise ValueError("unknown application action")
        self.prepare("rootfs")
        app = next((item for item in applications.declarations(self.c) if item["name"] == name), None)
        if app is None:
            raise ValueError(f"unknown application: {name}")
        if action == "clean":
            return applications.clean(self, [app])
        applications.configure(self, "rootfs", self.c.brout, [app])
        if app["config"] + "=y" not in (self.c.brout / ".config").read_text().splitlines():
            raise ValueError(f"application {name} is not enabled in the rootfs configuration")
        applications.build(self, [app])

    def build_rootfs(self):
        from . import applications
        c = self.c
        self.prepare("rootfs")
        apps = applications.selected(c)
        applications.clean(self, apps)
        self.make("rootfs", "-j" + c.jobs)
        for app in apps:
            applications.check_outputs(self, app)
        ext4 = require(c.brout / "images/rootfs.ext4")
        archive = require(c.brout / "images/rootfs.tar")
        if ext4.stat().st_size > 128 * 1024 * 1024:
            raise ValueError("rootfs.ext4 exceeds the 128 MiB upgrade slot")
        publish_file(ext4, c.out / "rootfs.ext4")
        publish_file(archive, c.out / "rootfs.tar")
        print(f"Root filesystem artifacts: {c.out}/rootfs.{{tar,ext4}}", flush=True)
        return ext4

    def toolchain(self):
        c = self.c
        if c.toolchain_out.resolve() == c.brout.resolve():
            raise ValueError("toolchain and rootfs require separate Buildroot output directories")
        from .toolchain import install
        options = ["O=" + str(c.toolchain_out), "BR2_EXTERNAL=", "GNU_TARGET_NAME=aarch64-linux-gnu"]
        c.run(["make", "-C", c.buildroot, *options, "qrk3588_toolchain_defconfig"])
        c.run(["make", "-C", c.buildroot, *options, "-j" + c.jobs,
               "toolchain", "host-gdb", "host-patchelf"])
        install(c)

    def sdk(self):
        self.prepare("rootfs")
        self.make("rootfs", "-j" + self.c.jobs, "sdk")

    def kvm_tests(self):
        c = self.c
        self.prepare("kernel")
        check_symbols(c.kout / ".config", ["KVM"])
        headers, tests = c.kout / "kvm-headers", c.kout / "kvm-selftests"
        self.make("kernel", "headers_install", "INSTALL_HDR_PATH=" + str(headers))
        tests.mkdir(parents=True, exist_ok=True)
        self.c.run(["make", "-j" + c.jobs, "-C", c.kernel / "tools/testing/selftests/kvm",
                    "ARCH=arm64", "CROSS_COMPILE=" + c.env["KERNEL_CROSS_COMPILE"],
                    "INSTALL_HDR_PATH=" + str(headers), "OUTPUT=" + str(tests) + "/", "LDFLAGS=-static"])
        self.c.run([c.env["KERNEL_CROSS_COMPILE"] + "gcc", "-static", "-O2", "-Wall", "-Wextra",
                    "-Werror", c.build / "tests/kvm-run.c", "-o", tests / "qrk-kvm-run"])

    def nfs(self, clean=False):
        c = self.c
        archive = require(c.brout / "images/rootfs.tar")
        destination = c.brout / "nfs-root"
        stage = Path(tempfile.mkdtemp(prefix=".nfs-root.stage.", dir=c.brout)) if clean else destination
        stage.mkdir(exist_ok=True)
        stage.chmod(0o755)
        prefix = [] if os.geteuid() == 0 else ["sudo"]
        try:
            c.run([*prefix, "tar", "--numeric-owner", "--same-owner", "-xpf", archive, "-C", stage])
            if clean:
                previous = c.run([*prefix, "python3", c.tools / "qrk3588_publish_directory.py",
                                  stage, destination], capture=True)
                if previous.strip():
                    print("Previous NFS root retained: " + os.fsdecode(previous).strip(), flush=True)
        except BaseException:
            if clean:
                print(f"Failed NFS preparation retained: {stage}", flush=True)
            raise
        print(f"NFS root filesystem: {destination}", flush=True)

    def check_keys(self, fit_signature=False):
        c = self.c
        if c.auth:
            for name in ("rot", "trusted-world", "non-trusted-world", "bl31", "bl33"):
                key = c.keys / f"{name}.pem"
                require(key)
                public = c.run(["openssl", "pkey", "-passin", "pass:", "-in", key,
                                "-pubout", "-outform", "DER"], capture=True)
                # Do not pass key bytes in arguments, logs or a shell pipeline.
                text = c.run(["openssl", "rsa", "-pubin", "-inform", "DER",
                              "-text", "-noout"], capture=True, input=public)
                if not text.splitlines() or text.splitlines()[0] != b"Public-Key: (3072 bit)":
                    raise ValueError(f"{key.name} must be an unencrypted RSA-3072 key")
        if fit_signature:
            require(c.keys / "os.key")
            require(c.keys / "os.crt")

    def atf_output(self, mode):
        return self.c.out / "arm-trusted-firmware" / ("qrk3588-secure" if self.c.auth else "qrk3588") / mode

    def atf_compile(self, mode, target, payload):
        c = self.c
        c.validate(mode)
        out = self.atf_output(mode)
        targets = {"bl1": ["bl1"], "fip": ["bl2", "bl31"], "all": ["bl1", "bl2", "bl31"]}[target]
        if target != "bl1":
            require(payload)
        self.check_keys()
        options = []
        if c.auth:
            options = ["COT=tbbr", "KEY_ALG=rsa", "KEY_SIZE=3072", "HASH_ALG=sha256",
                       "TFW_NVCTR_VAL=0", "NTFW_NVCTR_VAL=0"]
            for option, name in [("ROT_KEY", "rot"), ("TRUSTED_WORLD_KEY", "trusted-world"),
                                 ("NON_TRUSTED_WORLD_KEY", "non-trusted-world"),
                                 ("BL31_KEY", "bl31"), ("BL33_KEY", "bl33")]:
                options += [f"{option}={c.keys / (name + '.pem')}"]
            options += ["BL33=" + str(payload)]
        if target != "bl1":
            targets += ["fip" if c.auth else "fiptool"]
        c.run(["make", "-C", c.tf, "-j" + c.jobs, "PLAT=qrk3588",
               "CROSS_COMPILE=" + c.env["TF_CROSS_COMPILE"], "BUILD_PLAT=" + str(out),
               "DEBUG=" + ("1" if mode == "debug" else "0"),
               "TRUSTED_BOARD_BOOT=" + str(int(c.auth)), "GENERATE_COT=" + str(int(c.auth)),
               *options, *targets])
        for name in targets:
            if name in ("bl1", "bl2", "bl31"):
                require(out / (name + ".bin"))
        if target != "bl1" and not c.auth:
            fd, temp = tempfile.mkstemp(prefix=".fip.", dir=out)
            os.close(fd)
            try:
                c.run([out / "tools/fiptool/fiptool", "create", "--tb-fw", out / "bl2.bin",
                       "--soc-fw", out / "bl31.bin", "--nt-fw", payload, temp])
                require(temp)
                os.replace(temp, out / "fip.bin")
            finally:
                Path(temp).unlink(missing_ok=True)
        if target != "bl1":
            require(out / "fip.bin")
        if target != "fip":
            c.run([c.pack_rom, out / "bl1.bin", "--out", out / "rom"])
        return out

    def atf(self, mode, target):
        if target not in ("bl1", "fip"):
            raise ValueError("unknown ATF component")
        c = self.c
        c.validate(mode)
        out = self.atf_output(mode)
        payload = Path(c.env.get("QRK_BL33") or c.ubout / "u-boot.bin")
        self.atf_compile(mode, target, payload)
        if target == "fip":
            publish_file(out / "fip.bin", c.fip)
        else:
            c.run([c.pack_rom, out / "bl1.bin", "--out", c.out])
        print(f"ATF firmware profile: QRK_ATF_SECURE_BOOT={int(c.auth)} ({mode})", flush=True)
        return out
