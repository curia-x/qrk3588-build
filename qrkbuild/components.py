# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 ninozhang001@gmail.com
"""Component-specific native Make/Kconfig contracts."""
import os
from pathlib import Path
import shutil
import tempfile

from .artifacts import Artifacts, check_symbols, publish_file, require


class Components:
    def __init__(self, context):
        self.c = context
        self.artifacts = Artifacts(context)

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

    def prepare(self, component, output=None, quest=True):
        c = self.c
        out = output or {"uboot": c.ubout, "kernel": c.kout, "rootfs": c.brout}[component]
        if component == "kernel":
            self.guard_kernel()
        self.make(component, "olddefconfig" if (out / ".config").is_file() else "qrk3588_defconfig", output=out)
        if component == "kernel" and quest:
            self.merge_quest("kernel", out)
        return out

    def merge_quest(self, component, output=None):
        c = self.c
        out = output or (c.kout if component == "kernel" else c.brout)
        fragment = require(c.quest / "configs" / ("kernel.fragment" if component == "kernel" else "rootfs.fragment"))
        source = c.kernel if component == "kernel" else c.buildroot
        script = source / ("scripts/kconfig/merge_config.sh" if component == "kernel" else "support/kconfig/merge_config.sh")
        env = dict(c.env)
        if component == "rootfs":
            env.update(CONFIG_="BR2_", KCONFIG_CONFIG=str(out / ".config"))
        c.run([script, "-m", "-O", out, out / ".config", fragment], env=env)
        self.make(component, "olddefconfig", output=out)
        if component == "kernel":
            check_symbols(out / ".config", ["VIRTUALIZATION", "KVM", "EPOLL", "EVENTFD",
                                            "SIGNALFD", "TIMERFD", "SHMEM", "TMPFS"])
        else:
            check_symbols(out / ".config", ["QUEST_OS", "QEMU_SYSTEM_KVM", "QEMU_SYSTEM_TCG",
                                            "QEMU_TARGET_AARCH64"], prefix="BR2_PACKAGE_")

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

    def build_uboot(self, output=None, secure=False):
        out = output or self.c.ubout
        if secure:
            self.make("uboot", "qrk3588_defconfig", output=out)
            self.c.run([self.c.uboot / "scripts/kconfig/merge_config.sh", "-m", "-O", out,
                        out / ".config", self.c.uboot / "configs/qrk3588_secure.config"])
        self.prepare("uboot", out)
        self.make("uboot", "-j" + self.c.jobs, output=out)
        require(out / "u-boot.bin")
        if secure:
            check_symbols(out / ".config", ["CMDLINE", "CMD_BOOTM", "CMD_BOOTI",
                                            "QRK3588_SECURE_OS", "ENV_IS_NOWHERE"])
        return out

    def build_kernel(self, output=None, secure=False):
        out = output or self.c.kout
        self.guard_kernel()
        if secure:
            self.make("kernel", "qrk3588_defconfig", output=out)
            self.c.run([self.c.kernel / "scripts/kconfig/merge_config.sh", "-m", "-O", out,
                        out / ".config", self.c.kernel / "arch/arm64/configs/qrk3588_secure.config"])
        self.prepare("kernel", out)
        if secure:
            check_symbols(out / ".config", ["MD", "BLK_DEV_DM", "DM_INIT", "DM_VERITY",
                                            "CRYPTO_SHA256", "SHMEM", "TMPFS"])
        self.make("kernel", "-j" + self.c.jobs, "Image", "dtbs", output=out)
        require(out / "arch/arm64/boot/Image")
        require(out / "arch/arm64/boot/dts/quest/qrk3588.dtb")
        if not secure:
            self.compile_commands()
        return out

    def compile_commands(self):
        c = self.c
        self.make("kernel", "compile_commands.json")
        link = c.kernel / "compile_commands.json"
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(c.kout / "compile_commands.json")

    def quest_os(self, action="build"):
        self.prepare("rootfs")
        self.merge_quest("rootfs")
        from .quest_os import update
        update(self, action)

    def build_rootfs(self):
        c = self.c
        self.quest_os()
        self.make("rootfs", "-j" + c.jobs)
        ext4 = require(c.brout / "images/rootfs.ext4")
        archive = require(c.brout / "images/rootfs.tar")
        if ext4.stat().st_size > 128 * 1024 * 1024:
            raise ValueError("rootfs.ext4 exceeds the 128 MiB upgrade slot")
        publish_file(ext4, c.build / "out/rootfs.ext4")
        publish_file(archive, c.build / "out/rootfs.tar")
        print(f"Root filesystem artifacts: {c.build}/out/rootfs.{{tar,ext4}}", flush=True)
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

    def check_keys(self, os_auth=False):
        c = self.c
        if c.auth:
            for _, key in self.artifacts.signing_inputs():
                require(key)
                public = c.run(["openssl", "pkey", "-passin", "pass:", "-in", key,
                                "-pubout", "-outform", "DER"], capture=True)
                # Do not pass key bytes in arguments, logs or a shell pipeline.
                text = c.run(["openssl", "rsa", "-pubin", "-inform", "DER",
                              "-text", "-noout"], capture=True, input=public)
                if not text.splitlines() or text.splitlines()[0] != b"Public-Key: (3072 bit)":
                    raise ValueError(f"{key.name} must be an unencrypted RSA-3072 key")
        if os_auth:
            require(c.keys / "os.key")
            require(c.keys / "os.crt")
            if shutil.which(c.env.get("QRK_VERITYSETUP") or "veritysetup", path=c.env.get("PATH")) is None:
                raise ValueError("veritysetup is required")

    def atf_output(self, mode):
        return self.c.tf / "build" / ("qrk3588-secure" if self.c.auth else "qrk3588") / mode

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
        c.tool("qrk3588_delivery.py", "invalidate", out / "build-identity.json")
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

    def atf(self, mode, target="all"):
        c = self.c
        c.validate(mode)
        out = self.atf_output(mode)
        payload = Path(c.env.get("QRK_BL33") or c.ubout / "u-boot.bin")
        inputs = [] if target == "bl1" else [("bl33", require(payload))]
        if c.auth:
            self.check_keys()
            self.artifacts.begin(out, inputs=inputs, keys=self.artifacts.signing_inputs())
        self.atf_compile(mode, target, payload)
        if c.auth:
            self.artifacts.complete(out, mode, target=target)
        if target != "bl1":
            publish_file(out / "fip.bin", c.fip)
        if target != "fip":
            c.run([c.pack_rom, out / "bl1.bin", "--out", c.tf / "out"])
        print(f"ATF firmware profile: QRK_ATF_SECURE_BOOT={int(c.auth)} ({mode})", flush=True)
        return out
