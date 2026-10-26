#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 ninozhang001@gmail.com

# Loading defines conveniences only. Resolve symlinked entries without changing cwd.
QRK_BUILD_ENTRY_DIR=$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]:-$0}")") || return 1

_qrk_build_command() (
    # Preserve supported non-exported caller settings without changing the parent shell.
    local name
    for name in ROOT_DIR BUILD_SYSTEM_DIR TOOLS_DIR TOOLCHAIN_DIR TOOLCHAIN_PREFIX TOOLCHAIN_BUILD_OUT TF_DIR UBOOT_DIR KERNEL_DIR BUILDROOT_DIR \
        APPS_DIR QUEST_OS_DIR UBOOT_OUT KERNEL_OUT BUILDROOT_OUT UBOOT_CROSS_COMPILE \
        KERNEL_CROSS_COMPILE TF_CROSS_COMPILE CROSS_COMPILE QRK_BUILD_JOBS QRK_BUILD_MODE \
        QRK_ATF_SECURE_BOOT QRK_SECURE_OS QRK_SECUREBOOT_KEYS QRK_BL33 QRK_IMAGES_OUT \
        QRK_VERITYSETUP QRK_BUILD_LOCK_FD QRKUPDATE_TRANSPORT QRKUPDATE_UART QRKUPDATE_TIMEOUT \
        QRKUPDATE_TFTP_DIR QRKUPDATE_SERVERIP QRKUPDATE_IPADDR pack_bl1_rom rom_downloader fip_bin; do
        if [[ -v "$name" ]]; then export "$name"; fi
    done
    exec python3 "$QRK_BUILD_ENTRY_DIR/build.py" "$@"
)

# Names are static project constants; arguments are forwarded as an argv array.
for _qrk_entry in build_all build_atf build_bl1 build_fip build_uboot build_kernel build_rootfs \
    defconfig_uboot defconfig_kernel defconfig_rootfs menuconfig_uboot menuconfig_kernel \
    menuconfig_rootfs clean_uboot clean_kernel clean_rootfs prepare_uboot_config \
    prepare_kernel_config prepare_rootfs_config build_quest_os install_quest_os clean_quest_os \
    build_toolchain build_sdk build_kvm_selftests build_kernel_compile_commands build_linux_upgrade_package \
    build_kernel_upgrade_package build_dtb_upgrade_package build_fip_upgrade_package \
    pack_rootfs_upgrade_package prepare_nfs_rootfs prepare_nfs_rootfs_clean upgrade upgrade_check \
    upgrade_boot upgrade_kernel upgrade_dtb upgrade_fip upgrade_rootfs run_bl1 run_fip \
    bl1_upgrade_fip download_bl1_maskrom; do
    eval "$_qrk_entry() { _qrk_build_command $_qrk_entry \"\$@\"; }"
done
unset _qrk_entry
