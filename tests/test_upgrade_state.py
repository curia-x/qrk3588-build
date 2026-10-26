#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 ninozhang001@gmail.com
"""Fault injection against the actual U-Boot upgrade transaction functions."""
import ctypes
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
PREFIX = r"""
#include <stdlib.h>
#include <ctype.h>
#include <stdio.h>
#include <string.h>
#include <stdbool.h>
#include <errno.h>
#include "qrk_upgrade.h"
#define BIT(n) (1U << (n))
#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))
#define ENV_VALID 1
#define ENV_REDUND 2
#define QRK_STAGE_SIZE QRK_UPGRADE_BUFFER_SIZE
#define QRK_BLOCK_SIZE 512
#define ARCH_DMA_MINALIGN 64
#define LMB_MEM_ALLOC_ADDR 0
#define LMB_NONE 0
#define printf(...) ((void)0)
#define puts(s) ((void)(s))
typedef unsigned long phys_addr_t;
typedef unsigned long ulong;
struct blk_desc { u64 lba; unsigned int blksz; };
struct qrk_target { struct blk_desc *desc; };
static struct { int env_valid; } gd_value;
static typeof(gd_value) *gd = &gd_value;
static char state[40], pending[40], component[40], error[40], legacy[80];
static bool have_state, have_pending, have_component;
static unsigned int input_mask, written;
static int fail_component, fail_save, save_count, parse_error;
static char buffer[512], staging[40];
static bool have_staging;
static unsigned long reserved_address, mapped_address, freed_address;
static int reserve_error;
static u32 net_boot_file_size, command_bytes;
static int command_status, range_calls, filesize_reads;
static unsigned long receive_base, receive_capacity, command_capacity, command_initial_size;
static size_t tftp_received, xmodem_received;
typedef struct { int mode; } connection_info_t;
#define xyzModem_xmodem 1
static bool xyz_binary;
static int xmode, xopen, xclose, xterminate, xread, xabort, xwrong_mode;
static int qrk_getchar(void) { return -1; }
static int xyzModem_stream_open(connection_info_t *info, int *err) {
    (void)info; xopen++; xwrong_mode += !xyz_binary;
    *err = xmode == 1; return xmode == 1;
}
static int xyzModem_stream_read(char *data, int size, int *err) {
    (void)size; xwrong_mode += !xyz_binary;
    if (xmode == 2) { *err = 1; return -1; }
    *err = 0;
    if (xmode == 3 || xread++) return 0;
    memcpy(data, "abc", 3); return 3;
}
static void xyzModem_stream_terminate(bool abort, int (*getchar_fn)(void)) {
    (void)getchar_fn; xterminate++; xabort = abort; xwrong_mode += !xyz_binary;
}
static void xyzModem_stream_close(int *err) {
    xclose++; xwrong_mode += !xyz_binary; *err = 0;
}
static void tftp_set_receive_range(unsigned long base, unsigned long capacity) {
    receive_base = base; receive_capacity = capacity; range_calls++;
}
static unsigned long map_to_sysmem(const void *p) { return (unsigned long)p; }
static int run_commandf(const char *format, unsigned long address, const char *path) {
    (void)format; (void)address; (void)path;
    command_capacity = receive_capacity; command_initial_size = net_boot_file_size;
    net_boot_file_size = command_bytes;
    return command_status;
}
static const char *env_get(const char *name) {
    if (!strcmp(name, "filesize")) { filesize_reads++; return "ffffffff"; }
    if (!strcmp(name, "update_addr")) return have_staging ? staging : NULL;
    if (!strcmp(name, "upgrade_state")) return have_state ? state : NULL;
    if (!strcmp(name, "upgrade_pending")) return have_pending ? pending : NULL;
    if (!strcmp(name, "upgrade_component")) return have_component ? component : NULL;
    if (!strcmp(name, "upgrade_error")) return error;
    if (!strcmp(name, "upgrade_plan_id")) return *legacy ? legacy : NULL;
    return NULL;
}
static const char *env_get_default(const char *name) {
    return !strcmp(name, "update_addr") ? "0x40000000" : NULL;
}
static int strict_strtoul(const char *value, unsigned int base, unsigned long *out) {
    char *end; *out = strtoul(value, &end, base);
    return end == value || *end ? -EINVAL : 0;
}
static int env_set(const char *name, const char *value) {
    char *to = NULL;
    if (!strcmp(name, "upgrade_state")) { to = state; have_state = value != NULL; }
    if (!strcmp(name, "upgrade_pending")) { to = pending; have_pending = value != NULL; }
    if (!strcmp(name, "upgrade_component")) { to = component; have_component = value != NULL; }
    if (!strcmp(name, "upgrade_error")) to = error;
    if (!strcmp(name, "upgrade_plan_id")) to = legacy;
    if (!to) abort();
    strcpy(to, value ? value : "");
    return 0;
}
static int env_set_hex(const char *name, unsigned long value) {
    char text[32]; snprintf(text, sizeof(text), "%lx", value); return env_set(name, text);
}
static int env_save(void) {
    if (++save_count == fail_save) return 1;
    gd->env_valid = ENV_VALID; return 0;
}
static int qrk_persist_barrier(struct qrk_target *t) { (void)t; return 0; }
static int lmb_alloc_mem(int kind, int align, phys_addr_t *a, size_t size, int flags) {
    (void)kind; (void)align; (void)size; (void)flags;
    reserved_address = *a; return reserve_error;
}
static void *map_sysmem(unsigned long a, size_t size) { mapped_address = a; (void)size; return buffer; }
static void unmap_sysmem(void *p) { (void)p; }
static void lmb_free(unsigned long a, size_t size, int flags) { freed_address = a; (void)size; (void)flags; }
static int qrk_fetch_image(const char *source, const char *path, void *p, size_t *size) {
    (void)source; (void)path; (void)p; *size = sizeof(buffer); return 0;
}
int qrk_upgrade_image_parse(const void *p, size_t size, bool xmodem, struct qrk_upgrade_image *image) {
    (void)p; (void)size; (void)xmodem;
    if (parse_error) return parse_error;
    memset(image, 0, sizeof(*image));
    for (unsigned int i = 0; i < QRK_UPGRADE_COMPONENT_COUNT; i++)
        if (input_mask & BIT(i)) {
            struct qrk_upgrade_component *c = &image->components[image->component_count++];
            c->id = i; c->size = 512;
        }
    return 0;
}
const char *qrk_upgrade_component_name(enum qrk_upgrade_component_id id) {
    static const char *names[] = {"rootfs", "kernel", "fip", "dtb"}; return names[id];
}
static int qrk_open_target(struct qrk_target *target) {
    static struct blk_desc desc = {.lba = 1ULL << 30, .blksz = 512}; target->desc = &desc; return 0;
}
int qrk_upgrade_target_range(enum qrk_upgrade_component_id id, u64 sectors, u64 *start, u64 *capacity) {
    (void)id; (void)sectors; *start = 0; *capacity = 1ULL << 28; return 0;
}
static int qrk_write_component(struct qrk_target *t, const struct qrk_upgrade_component *c, const void *p) {
    (void)t; (void)p; written |= BIT(c->id); return (int)c->id == fail_component ? -EIO : 0;
}
"""
SUFFIX = r"""
void reset_test(void) {
    have_staging = false; reserve_error = 0;
    reserved_address = mapped_address = freed_address = 0;
    env_set("upgrade_state", "complete"); env_set("upgrade_pending", NULL);
    env_set("upgrade_component", "none"); env_set("upgrade_error", "");
    env_set("upgrade_plan_id", "old-package-hash"); gd->env_valid = ENV_VALID;
}
void metadata(const char *s, const char *p, const char *c, int valid) {
    env_set("upgrade_state", s); env_set("upgrade_pending", p);
    env_set("upgrade_component", c); gd->env_valid = valid;
}
int upgrade_test(unsigned int mask, int bad_component, int bad_save, int bad_parse) {
    input_mask = mask; fail_component = bad_component; fail_save = bad_save;
    parse_error = bad_parse; save_count = 0; written = 0;
    return qrk_run("tftp", "new-package.upg");
}
void stage_test(const char *value) {
    have_staging = value != NULL;
    snprintf(staging, sizeof(staging), "%s", value ? value : "");
}
void reserve_failure_test(int value) { reserve_error = value; }
unsigned long address_test(int which) {
    return which == 0 ? reserved_address : which == 1 ? mapped_address : freed_address;
}
int tftp_test(unsigned int bytes, int status) {
    command_bytes = bytes; command_status = status; net_boot_file_size = 0x12345678;
    range_calls = filesize_reads = 0; tftp_received = 0;
    return qrk_tftp("package.upg", buffer, 128, &tftp_received);
}
unsigned long tftp_value_test(int which) {
    switch (which) {
    case 0: return tftp_received;
    case 1: return command_capacity;
    case 2: return receive_capacity;
    case 3: return receive_base;
    case 4: return filesize_reads;
    case 5: return command_initial_size;
    default: return range_calls;
    }
}
int xmodem_test(int initial_binary, int mode, unsigned int capacity) {
    xyzModem_set_binary(initial_binary);
    xmode = mode; xopen = xclose = xterminate = xread = xabort = xwrong_mode = 0;
    xmodem_received = 0;
    return qrk_xmodem(buffer, capacity, &xmodem_received);
}
unsigned long xmodem_value_test(int which) {
    switch (which) {
    case 0: return xyz_binary;
    case 1: return xopen;
    case 2: return xclose;
    case 3: return xterminate;
    case 4: return xabort;
    case 5: return xwrong_mode;
    default: return xmodem_received;
    }
}
unsigned int pending_test(void) { return qrk_pending_components(); }
unsigned int written_test(void) { return written; }
const char *field_test(const char *name) { return env_get(name); }
"""


class UpgradeStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        directory = Path(cls.temp.name)
        types = directory / 'linux/types.h'
        types.parent.mkdir()
        types.write_text('#include <stdint.h>\n#include <stddef.h>\n#include <stdbool.h>\ntypedef uint8_t u8; typedef uint32_t u32; typedef uint64_t u64;\n')
        (directory / 'qrk_upgrade.h').write_bytes((ROOT / 'u-boot/include/qrk_upgrade.h').read_bytes())
        storage = Path('board/quest/qrk3588_storage.h')
        (directory / storage).parent.mkdir(parents=True, exist_ok=True)
        (directory / storage).write_bytes((ROOT / 'u-boot/include' / storage).read_bytes())
        source = (ROOT / 'u-boot/board/quest/qrk3588/qrk3588_upgrade.c').read_text()
        functions = []
        for name in ('qrk_env_valid', 'qrk_set_state', 'qrk_mark_failed',
                     'qrk_pending_components', 'qrk_component', 'qrk_error_code', 'qrk_tftp', 'qrk_xmodem', 'qrk_run'):
            position = source.index(name + '(')
            start = source.rfind('\nstatic ', 0, position) + 1
            end = source.index('\n}\n', position) + 3
            functions.append(source[start:end])
        harness = directory / 'transaction.c'
        xyz_source = (ROOT / 'u-boot/common/xyzModem.c').read_text()
        xyz_start = xyz_source.index('void xyzModem_set_binary(')
        xyz_end = xyz_source.index('\n}\n', xyz_start) + 3
        hex_source = (ROOT / 'u-boot/include/hexdump.h').read_text()
        hex_start = hex_source.index('static inline int hex_to_bin(')
        hex_end = hex_source.index('\n}\n', hex_start) + 3
        harness.write_text(PREFIX + hex_source[hex_start:hex_end] +
                           xyz_source[xyz_start:xyz_end] +
                           '\n'.join(functions) + SUFFIX)
        library = directory / 'transaction.so'
        subprocess.run(['cc', '-shared', '-fPIC', '-Wall', '-Wextra', '-Werror',
                        '-I', str(directory),
                        str(harness), '-o', str(library)], check=True)
        cls.lib = ctypes.CDLL(str(library))
        cls.lib.metadata.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
        cls.lib.field_test.argtypes = [ctypes.c_char_p]
        cls.lib.field_test.restype = ctypes.c_char_p
        cls.lib.upgrade_test.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_int]
        cls.lib.stage_test.argtypes = [ctypes.c_char_p]
        cls.lib.reserve_failure_test.argtypes = [ctypes.c_int]
        cls.lib.address_test.argtypes = [ctypes.c_int]
        cls.lib.address_test.restype = ctypes.c_ulong
        cls.lib.tftp_test.argtypes = [ctypes.c_uint, ctypes.c_int]
        cls.lib.tftp_value_test.argtypes = [ctypes.c_int]
        cls.lib.tftp_value_test.restype = ctypes.c_ulong
        cls.lib.xmodem_test.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        cls.lib.xmodem_value_test.argtypes = [ctypes.c_int]
        cls.lib.xmodem_value_test.restype = ctypes.c_ulong

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.lib.reset_test()

    def run_package(self, mask, fail=-1, save=0, parse=0):
        return self.lib.upgrade_test(mask, fail, save, parse)

    def check_xmodem_mode(self, mode, capacity, expected, counters):
        for initial in (0, 1):
            with self.subTest(initial_binary=initial):
                self.assertEqual(self.lib.xmodem_test(initial, mode, capacity), expected)
                self.assertEqual([self.lib.xmodem_value_test(i) for i in range(7)],
                                 counters)

    def test_xmodem_success_keeps_selected_binary_mode(self):
        self.check_xmodem_mode(0, 16, 0, [1, 1, 1, 1, 0, 0, 3])

    def test_xmodem_open_failure_keeps_selected_binary_mode(self):
        self.check_xmodem_mode(1, 16, -5, [1, 1, 0, 0, 0, 0, 0])

    def test_xmodem_read_failure_keeps_selected_binary_mode(self):
        self.check_xmodem_mode(2, 16, -5, [1, 1, 1, 1, 1, 0, 0])

    def test_xmodem_overflow_aborts_and_keeps_binary_mode(self):
        self.check_xmodem_mode(0, 2, -5, [1, 1, 1, 1, 1, 0, 0])

    def test_xmodem_empty_stream_keeps_selected_binary_mode(self):
        self.check_xmodem_mode(3, 16, -22, [1, 1, 1, 1, 0, 0, 0])

    def test_tftp_uses_actual_length_and_ignores_stale_filesize(self):
        self.assertEqual(self.lib.tftp_test(17, 0), 0)
        self.assertEqual([self.lib.tftp_value_test(i) for i in range(7)],
                         [17, 128, 0, 0, 0, 0, 2])

    def test_tftp_empty_download_rejects_stale_length(self):
        self.assertEqual(self.lib.tftp_test(0, 0), -22)
        self.assertEqual([self.lib.tftp_value_test(i) for i in range(7)],
                         [0, 128, 0, 0, 0, 0, 2])

    def test_tftp_error_clears_receive_range(self):
        self.assertEqual(self.lib.tftp_test(17, 1), -5)
        self.assertEqual([self.lib.tftp_value_test(i) for i in range(7)],
                         [0, 128, 0, 0, 0, 0, 2])

    def test_staging_uses_compiled_default_when_missing(self):
        self.assertEqual(self.run_package(2), 0)
        self.assertEqual([self.lib.address_test(i) for i in range(3)],
                         [0x40000000] * 3)

    def test_staging_override_is_released_after_rejection(self):
        self.lib.stage_test(b'0x60000000')
        self.assertEqual(self.run_package(2, parse=-74), -74)
        self.assertEqual([self.lib.address_test(i) for i in range(3)],
                         [0x60000000] * 3)
        self.assertEqual(self.lib.written_test(), 0)

    def test_invalid_staging_address_does_not_reserve_or_write(self):
        for address in (b'', b'not-hex', b'0x0', b'0x60000000junk', b'0x60000001'):
            with self.subTest(address=address):
                self.lib.reset_test()
                self.lib.stage_test(address)
                self.assertEqual(self.run_package(2), -22)
                self.assertEqual([self.lib.address_test(i) for i in range(3)],
                                 [0, 0, 0])
                self.assertEqual(self.lib.written_test(), 0)

    def test_unavailable_staging_does_not_map_or_write(self):
        self.lib.stage_test(b'0x60000000')
        self.lib.reserve_failure_test(-12)
        self.assertEqual(self.run_package(2), -12)
        self.assertEqual([self.lib.address_test(i) for i in range(3)],
                         [0x60000000, 0, 0])
        self.assertEqual(self.lib.written_test(), 0)

    def test_pending_mask_accepts_valid_hex_forms(self):
        for mask in range(16):
            for value in (f'{mask:x}', f'{mask:X}', f'0x{mask:x}',
                          f'0X{mask:X}', f'{mask:032x}'):
                with self.subTest(value=value):
                    self.lib.metadata(b'failed', value.encode(), b'none', 1)
                    self.assertEqual(self.lib.pending_test(), mask)

    def test_pending_mask_invalid_text_is_conservative(self):
        for value in (b'', b'0x', b'0X', b'not-hex', b'3z', b'-1',
                      b'+1', b' 1', b'1 ', b'1\n', b'10', b'0x10'):
            with self.subTest(value=value):
                self.lib.metadata(b'failed', value, b'none', 1)
                self.assertEqual(self.lib.pending_test(), 15)

    def test_pending_mask_overflow_cannot_clear_pending_bits(self):
        for value in (b'10000000000000000', b'10000000000000001',
                      b'0x10000000000000000', b'0X10000000000000001',
                      b'10000000000000000000000000000000'):
            with self.subTest(value=value):
                self.lib.metadata(b'failed', value, b'none', 1)
                self.assertEqual(self.lib.pending_test(), 15)

    def test_new_partial_package_replaces_failed_kernel(self):
        self.lib.metadata(b'failed', None, b'kernel', 1)
        self.assertEqual(self.run_package(2), 0)
        self.assertEqual(self.lib.pending_test(), 0)
        self.assertEqual(self.lib.field_test(b'upgrade_state'), b'complete')
        self.assertIsNone(self.lib.field_test(b'upgrade_plan_id'))

    def test_unrelated_success_retains_old_component_failure(self):
        self.lib.metadata(b'failed', None, b'kernel', 1)
        self.assertEqual(self.run_package(8), 0)
        self.assertEqual(self.lib.written_test(), 8)
        self.assertEqual(self.lib.pending_test(), 2)
        self.assertEqual(self.lib.field_test(b'upgrade_state'), b'failed')
        self.assertEqual(self.run_package(2), 0)
        self.assertEqual(self.lib.pending_test(), 0)

    def test_separate_partial_packages_can_repair_multiple_failures(self):
        self.lib.metadata(b'failed', b'3', b'kernel', 1)
        self.assertEqual(self.run_package(1), 0)
        self.assertEqual(self.lib.pending_test(), 2)
        self.assertEqual(self.run_package(2), 0)
        self.assertEqual(self.lib.pending_test(), 0)

    def test_interrupted_package_does_not_require_unwritten_components(self):
        self.assertEqual(self.run_package(15, fail=1), -5)
        self.assertEqual(self.lib.written_test(), 3)
        self.assertEqual(self.lib.pending_test(), 2)
        self.assertEqual(self.run_package(2), 0)
        self.assertEqual(self.lib.pending_test(), 0)

    def test_new_failure_is_added_to_existing_uncertainty(self):
        self.lib.metadata(b'failed', b'2', b'kernel', 1)
        self.assertEqual(self.run_package(8, fail=3), -5)
        self.assertEqual(self.lib.pending_test(), 10)
        self.assertEqual(self.lib.field_test(b'upgrade_component'), b'dtb')

    def test_invalid_environment_allows_a_valid_package(self):
        self.lib.metadata(None, None, None, 0)
        self.assertEqual(self.run_package(2), 0)
        self.assertEqual(self.lib.pending_test(), 13)
        self.assertEqual(self.run_package(13), 0)
        self.assertEqual(self.lib.pending_test(), 0)

    def test_unknown_or_corrupt_state_does_not_reject_updates(self):
        for state, pending in ((b'unknown', None), (b'failed', b'not-hex'),
                               (b'failed', b'10000000000')):
            self.lib.metadata(state, pending, None, 1)
            self.assertEqual(self.run_package(15), 0)
            self.assertEqual(self.lib.pending_test(), 0)

    def test_invalid_package_does_not_write_or_clear_failure(self):
        self.lib.metadata(b'failed', b'2', b'kernel', 1)
        self.assertEqual(self.run_package(8, parse=-74), -74)
        self.assertEqual(self.lib.written_test(), 0)
        self.assertEqual(self.lib.pending_test(), 2)

    def test_metadata_must_persist_before_target_writes(self):
        self.assertEqual(self.run_package(2, save=1), -28)
        self.assertEqual(self.lib.written_test(), 0)

    def test_failed_success_checkpoint_retains_uncertainty(self):
        self.assertEqual(self.run_package(2, save=2), -28)
        self.assertEqual(self.lib.pending_test(), 2)
        self.assertEqual(self.run_package(2), 0)
        self.assertEqual(self.lib.pending_test(), 0)


if __name__ == '__main__':
    unittest.main()
