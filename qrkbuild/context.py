# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 ninozhang001@gmail.com
"""Resolved configuration, subprocess lifetime and the shared workspace lock."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import signal
import subprocess


class Context:
    def __init__(self, build_dir, environment=None):
        self.env = dict(os.environ if environment is None else environment)
        def path(name, default):
            return Path(self.env.get(name) or default).expanduser().absolute()
        self.build = path("BUILD_SYSTEM_DIR", build_dir)
        self.tools = path("TOOLS_DIR", Path(build_dir) / "tools")
        self.root = path("ROOT_DIR", self.build.parent)
        self.toolchain = path("TOOLCHAIN_DIR", self.root / "toolchain")
        self.tf = path("TF_DIR", self.root / "arm-trusted-firmware")
        self.uboot = path("UBOOT_DIR", self.root / "u-boot")
        self.kernel = path("KERNEL_DIR", self.root / "linux")
        self.buildroot = path("BUILDROOT_DIR", self.root / "buildroot")
        self.toolchain_out = path("TOOLCHAIN_BUILD_OUT", self.buildroot / "output/toolchain-aarch64-linux-gnu-install")
        self.apps = path("APPS_DIR", self.root / "apps")
        self.quest = path("QUEST_OS_DIR", self.apps / "quest_os")
        self.ubout = path("UBOOT_OUT", self.uboot / "build/qrk3588")
        self.kout = path("KERNEL_OUT", self.kernel / "build/qrk3588")
        self.brout = path("BUILDROOT_OUT", self.buildroot / "output/qrk3588-rootfs")
        self.images = path("QRK_IMAGES_OUT", self.build / "out/images")
        self.keys = path("QRK_SECUREBOOT_KEYS", self.build / "keys/qrk3588/development")
        self.fip = path("fip_bin", self.tf / "out/qrk3588_fip.bin")
        self.pack_rom = path("pack_bl1_rom", self.tools / "qrk3588_pack_bl1_rom.sh")
        self.downloader = path("rom_downloader", self.tools / "qrk3588-downloader")
        self.mode = self.env.get("QRK_BUILD_MODE") or "release"
        self.toolchain_prefix = self.env.get("TOOLCHAIN_PREFIX") or "aarch64-linux-gnu"
        cross = str(self.toolchain / "gcc/bin" / (self.toolchain_prefix + "-"))
        defaults = {
            "TF_CROSS_COMPILE": self.env.get("CROSS_COMPILE") or cross,
            "UBOOT_CROSS_COMPILE": cross,
            "KERNEL_CROSS_COMPILE": cross,
            "QRK_BUILD_JOBS": str(len(os.sched_getaffinity(0))),
            "QRKUPDATE_TRANSPORT": "tftp", "QRKUPDATE_UART": "/dev/ttyUSB0",
            "QRKUPDATE_TIMEOUT": "900",
        }
        for key, value in defaults.items():
            self.env[key] = self.env.get(key) or value  # set to default if non exist or empty
        # An explicitly empty network option has a defined meaning.
        for key, value in {
            "QRKUPDATE_TFTP_DIR": "/home/book/tftp_share",
            "QRKUPDATE_SERVERIP": "192.168.1.121",
            "QRKUPDATE_IPADDR": "192.168.1.123",
            "QRK_ATF_SECURE_BOOT": "1", "QRK_SECURE_OS": "0",
        }.items():
            self.env.setdefault(key, value)     # set to default if non exist
        self.env["BUILDROOT_OUT"] = str(self.brout)
        self.jobs = self.env["QRK_BUILD_JOBS"]
        if not self.jobs.isdecimal() or int(self.jobs) < 1:
            raise ValueError("QRK_BUILD_JOBS must be a positive integer")
        self.lock_fd = None

    @property
    def auth(self):
        return self.env["QRK_ATF_SECURE_BOOT"] == "1"

    @property
    def os_auth(self):
        return self.env["QRK_SECURE_OS"] == "1"

    def validate(self, mode, images=False, partial=False):
        if mode not in ("release", "debug"):
            raise ValueError("expected release or debug")
        if self.env["QRK_ATF_SECURE_BOOT"] not in ("0", "1"):
            raise ValueError("QRK_ATF_SECURE_BOOT must be 0 or 1")
        if images or partial:
            if self.env["QRK_SECURE_OS"] not in ("0", "1"):
                raise ValueError("QRK_SECURE_OS must be 0 or 1")
            if partial and self.os_auth:
                raise ValueError("OS authentication requires a matched complete image set; partial upgrades are unsupported")
            if images and self.os_auth and (not self.auth or self.env.get("QRK_BL33")):
                raise ValueError("OS authentication requires ATF authentication and its own key-bearing BL33")

    @contextmanager
    def locked(self):
        if self.lock_fd is not None:
            yield
            return
        lock = self.tf / "build/.qrk3588-secure.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        inherited = self.env.get("QRK_BUILD_LOCK_FD")
        owned = not inherited
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600) if owned else int(inherited)
        try:
            try:
                expected, actual = lock.stat(), os.fstat(fd)
            except OSError as error:
                raise ValueError("inherited build lock is unavailable") from error
            if (expected.st_dev, expected.st_ino) != (actual.st_dev, actual.st_ino):
                raise ValueError("inherited build lock does not match this workspace")
            fcntl.flock(fd, fcntl.LOCK_EX)
            self.lock_fd = fd
            yield
        finally:
            self.lock_fd = None
            if owned:
                os.close(fd)

    def run(self, argv, *, cwd=None, capture=False, env=None, interactive=False, timeout=None,
            allowed=(0,), input=None):
        arguments = [str(item) for item in argv]
        process = subprocess.Popen(
            arguments, cwd=cwd or self.root, env=self.env if env is None else env,
            stdout=subprocess.PIPE if capture else None,
            stdin=subprocess.PIPE if input is not None else None,
            start_new_session=not interactive,
            pass_fds=() if self.lock_fd is None else (self.lock_fd,))
        try:
            stdout, _ = process.communicate(input=input, timeout=timeout)
        except BaseException:
            # Shell transports get TERM first so their EXIT traps can restore tty state.
            if interactive:
                process.terminate()
            else:
                self._signal_group(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            finally:
                if interactive:
                    if process.poll() is None:
                        process.kill()
                else:
                    self._signal_group(process.pid, signal.SIGKILL)
                process.wait()
            raise
        if process.returncode not in allowed:
            raise subprocess.CalledProcessError(process.returncode, arguments, output=stdout)
        return stdout if capture else process.returncode

    @staticmethod
    def _signal_group(pid, sig):
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            pass

    def tool(self, name, *args, **kwargs):
        return self.run(["python3", self.tools / name, *args], **kwargs)
