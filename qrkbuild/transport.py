# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 ninozhang001@gmail.com
"""One process interface to the existing Shell UART/TFTP/MaskROM backends."""
import ipaddress

from .artifacts import require


class Transport:
    def __init__(self, context, backend):
        self.c = context
        self.backend = backend

    def transfer(self, action, image, check_action=""):
        require(image)
        timeout = self.c.env["QRKUPDATE_TIMEOUT"]
        if not timeout.isdecimal() or int(timeout) < 1:
            raise ValueError("QRKUPDATE_TIMEOUT must be a positive integer")
        if self.c.env["QRKUPDATE_TRANSPORT"] not in ("tftp", "xmodem"):
            raise ValueError("QRKUPDATE_TRANSPORT must be tftp or xmodem")
        for key in ("QRKUPDATE_SERVERIP", "QRKUPDATE_IPADDR"):
            if self.c.env[key]:
                ipaddress.IPv4Address(self.c.env[key])
        if action not in ("apply", "recover", "check") or (
                action == "check" and check_action not in ("apply", "recover")):
            raise ValueError("transfer requires an explicit action and check target")
        self.c.tool("qrk3588_pack_upgrade.py", "--check", image)
        print(f"Upgrade action: {action}; target: {check_action or action}; package: {image}", flush=True)
        self.c.run(["bash", self.backend, "transfer", action, image, check_action])

    def send_fip(self, image):
        self.c.run(["bash", self.backend, "send-fip", require(image)])

    def maskrom(self, image):
        self.c.run([self.c.downloader, "--rom", require(image)])
