# SPDX-License-Identifier: GPL-2.0-or-later
"""Install independent GCC/GDB prefixes using native package installation rules."""
import os
from pathlib import Path
import shlex
import shutil
import tempfile


TARGET = "aarch64-linux-gnu"
COMPONENTS = ("gcc", "gdb")

# These fields occupy the same positions in ELF32 and ELF64 headers.
ELF_MAGIC = b"\x7fELF"
ELF_HEADER_PREFIX_SIZE = 20
ELF_BYTE_ORDER_OFFSET = 5
ELF_FILE_TYPE_FIELD = slice(16, 18)
ELF_MACHINE_FIELD = slice(18, 20)
ELF_RELOCATABLE = 1


def install(context):
    build = _read_buildroot_variables(context)
    host = Path(build["HOST_DIR"]).resolve()
    environment = dict(context.env, PATH=str(host / "bin") + os.pathsep + context.env["PATH"])
    for name in ("LD_LIBRARY_PATH", "PYTHONHOME", "PYTHONPATH"):
        environment.pop(name, None)

    context.toolchain.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".install.", dir=context.toolchain)).resolve()
    try:
        _install_gcc(context, build, stage / "gcc", environment)
        _install_gdb(context, build, stage / "gdb", environment)

        for component in COMPONENTS:
            prefix = stage / component
            destination = context.toolchain / component
            _prepare_runtime(context, prefix, host)
            _rewrite_text_paths(prefix, {host: destination, prefix: destination})

        # Metadata is final before verification; publication only moves directories.
        verify(context, stage, environment)
        backup = _publish_components(stage, context.toolchain, context.toolchain_out)
    except BaseException:
        print("Failed installation retained for inspection: " + str(stage), flush=True)
        raise
    else:
        shutil.rmtree(stage)
        print("Installed independent GCC/GDB; previous installation: " + str(backup), flush=True)


def _read_buildroot_variables(context):
    names = ["HOST_DIR", "STAGING_DIR", "HOST_BINUTILS_DIR", "HOST_GCC_FINAL_DIR",
             "HOST_GDB_DIR", "HOST_GCC_FINAL_MAKE_OPTS"]
    output = context.run([
        "make", "-s", "-C", context.buildroot,
        "O=" + str(context.toolchain_out), "GNU_TARGET_NAME=" + TARGET,
        "printvars", "VARS=" + " ".join(names),
    ], capture=True)
    variables = {}
    for line in output.decode().splitlines():
        if "=" in line:
            name, value = line.split("=", 1)
            variables[name] = value
    return variables


def _install_gcc(context, build, prefix, environment):
    _native_install(context, Path(build["HOST_BINUTILS_DIR"]), prefix, environment)
    compiler_build = Path(build["HOST_GCC_FINAL_DIR"]) / "build"
    compiler_options = shlex.split(build["HOST_GCC_FINAL_MAKE_OPTS"])
    _native_install(context, compiler_build, prefix, environment, compiler_options)
    sysroot = prefix / TARGET / "sysroot"
    shutil.copytree(build["STAGING_DIR"], sysroot, symlinks=True)


def _install_gdb(context, build, prefix, environment):
    debugger_build = Path(build["HOST_GDB_DIR"]) / "build"
    _native_install(context, debugger_build, prefix, environment)
    # GDB only relocates its default debug path when this directory exists.
    (prefix / "lib/debug").mkdir(parents=True, exist_ok=True)
    host_library = Path(build["HOST_DIR"]) / "lib"
    for library in host_library.glob("python3.*"):
        if library.is_dir():
            shutil.copytree(library, prefix / "lib" / library.name, symlinks=True)


def _native_install(context, build_directory, prefix, environment, make_options=()):
    context.run([
        "make", "-C", build_directory, *make_options,
        "prefix=" + str(prefix), "exec_prefix=" + str(prefix),
        "MAKEINFO=true", "install",
    ], env=environment)


def _elf_header(file):
    with file.open("rb") as stream:
        header = stream.read(ELF_HEADER_PREFIX_SIZE)
    if len(header) < ELF_HEADER_PREFIX_SIZE or not header.startswith(ELF_MAGIC):
        return None
    byte_order = "little" if header[ELF_BYTE_ORDER_OFFSET] == 1 else "big"
    file_type = int.from_bytes(header[ELF_FILE_TYPE_FIELD], byte_order)
    machine = int.from_bytes(header[ELF_MACHINE_FIELD], byte_order)
    return file_type, machine


def _prepare_runtime(context, prefix, host):
    """Copy referenced host libraries, then delegate relocation to Buildroot."""
    patch = host / "bin/patchelf"
    _, host_machine = _elf_header(patch)
    library = prefix / "lib"
    library.mkdir(exist_ok=True)
    pending = []

    for directory, subdirectories, files in os.walk(prefix):
        if Path(directory) == prefix / TARGET / "sysroot":
            subdirectories.clear()
            continue
        for name in files:
            file = Path(directory) / name
            if file.is_symlink():
                continue
            header = _elf_header(file)
            if header is None:
                continue
            file_type, machine = header
            if machine != host_machine or file_type == ELF_RELOCATABLE:
                continue
            # Binutils installs hardlinks whose different paths need different RPATHs.
            if file.stat().st_nlink > 1:
                temporary = file.with_name(file.name + ".unlinked")
                shutil.copy2(file, temporary)
                temporary.replace(file)
            pending.append(file)

    seen = set()
    while pending:
        file = pending.pop()
        if file in seen:
            continue
        seen.add(file)
        # Static executables can be rejected by patchelf and need no runtime libraries.
        output = context.run([patch, "--print-needed", file], capture=True, allowed=(0, 1))
        needed_libraries = output.decode().splitlines()
        for name in needed_libraries:
            source = host / "lib" / name
            destination = library / name
            if source.is_file() and not destination.exists():
                shutil.copy2(source.resolve(), destination)
                pending.append(destination)
        if needed_libraries:
            context.run([patch, "--set-rpath", str(library), file])

    # Native fix-rpath scans ELF files, excludes the sysroot and produces $ORIGIN paths.
    environment = dict(context.env, HOST_DIR=str(prefix),
                       STAGING_DIR=str(prefix / TARGET / "sysroot"),
                       PATCHELF=str(patch), PARALLEL_JOBS="1")
    context.run([context.buildroot / "support/scripts/fix-rpath", "host"], env=environment)


def _rewrite_text_paths(prefix, replacements):
    """Finalize host metadata without changing binary files, links or the sysroot."""
    for directory, subdirectories, files in os.walk(prefix):
        if Path(directory) == prefix / TARGET / "sysroot":
            subdirectories.clear()
            continue
        for name in files:
            file = Path(directory) / name
            if file.is_symlink():
                continue
            original = file.read_bytes()
            if b"\0" in original:
                continue
            updated = original
            for source, destination in replacements.items():
                updated = updated.replace(str(source).encode(), str(destination).encode())
            if updated != original:
                file.write_bytes(updated)


def _publish_components(stage, destination, output):
    """Publish independently: a failure restores the component being replaced.

    A previously published sibling remains installed; this is not a pair transaction.
    """
    backup = Path(tempfile.mkdtemp(prefix="toolchain-backup.", dir=output))
    for component in COMPONENTS:
        installed = destination / component
        previous = backup / component
        if installed.exists():
            installed.rename(previous)
        try:
            (stage / component).rename(installed)
        except BaseException:
            if previous.exists():
                previous.rename(installed)
            raise
    return backup


def verify(context, root, environment):
    gcc_prefix = root / "gcc"
    gdb_prefix = root / "gdb"
    gcc = gcc_prefix / "bin" / (TARGET + "-gcc")
    gdb = gdb_prefix / "bin" / (TARGET + "-gdb")
    output = context.run([gcc, "-print-sysroot"], capture=True, env=environment)
    if not Path(output.decode().strip()).resolve().is_relative_to(gcc_prefix):
        raise ValueError("installed GCC refers to a sysroot outside its prefix")

    with tempfile.TemporaryDirectory(prefix="toolchain-probe.", dir=context.toolchain_out) as work:
        source = Path(work) / "probe.c"
        binary = Path(work) / "probe"
        source.write_text("""#include <stdio.h>
int main(void) { return puts("toolchain"); }
""")
        context.run([gcc, "-O2", "-flto", "-static", source, "-o", binary], env=environment)
        commands = Path(work) / "check-gdb.gdb"
        commands.write_text(f"""show architecture
show data-directory
show debug-file-directory
python
import sys
import gdb.printing
import ctypes
import zlib
import pyexpat

expected_prefix = {str(gdb_prefix)!r}
assert sys.prefix == expected_prefix
for module in (ctypes, zlib, pyexpat, gdb.printing):
    assert module.__file__.startswith(expected_prefix + "/")
end
""")
        output = context.run([gdb, "-nx", "--batch", "-x", commands, binary],
                             capture=True, env=environment).decode()
        for directory in (gdb_prefix / "share/gdb", gdb_prefix / "lib/debug"):
            if str(directory) not in output:
                raise ValueError("GDB data/debug path escaped its installation prefix")

        output = context.run([gcc, "-print-prog-name=cc1"], capture=True, env=environment)
        cc1 = Path(output.decode().strip())
        _verify_libraries(context, cc1, gcc_prefix, environment)
        _verify_libraries(context, gdb, gdb_prefix, environment)


def _verify_libraries(context, program, prefix, environment):
    output = context.run(["ldd", program], capture=True, env=environment).decode()
    if "not found" in output:
        raise ValueError("installed host library is missing: " + output)
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 3 or fields[1] != "=>":
            continue
        name, resolved_path = fields[0], Path(fields[2])
        if (prefix / "lib" / name).exists() and not resolved_path.resolve().is_relative_to(prefix):
            raise ValueError("installed host library resolved outside its component: " + line)
