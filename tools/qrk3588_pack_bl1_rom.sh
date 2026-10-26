#!/usr/bin/env bash

set -euo pipefail

ROM=
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
OUT_DIR=${OUT_DIR:-"$SCRIPT_DIR/../out"}
OUTPUT_NAME=qrk3588_bl1.bin

usage() {
    cat <<EOF
Usage: $0 BL1.bin [--out DIR]

Stage a QRK3588 BL1 ROM image for Maskrom download.

Options:
  BL1.bin     freshly built TF-A BL1 ROM image
  --out DIR   output directory; default: $OUT_DIR
EOF
}

fail() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --out) OUT_DIR=${2:?missing argument for --out}; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        -*) usage >&2; fail "unknown option: $1" ;;
        *)
            [[ -z "$ROM" ]] || fail "only one BL1 ROM file may be specified"
            ROM=$1
            shift
            ;;
    esac
done

[[ -n "$ROM" ]] || fail "BL1 ROM file is required"
[[ -f "$ROM" ]] || fail "ROM file not found: $ROM"
[[ -s "$ROM" ]] || fail "ROM file is empty: $ROM"

mkdir -p "$OUT_DIR"
OUT_DIR=$(cd "$OUT_DIR" && pwd)
temporary=$(mktemp "$OUT_DIR/.${OUTPUT_NAME}.XXXXXX")
cleanup() {
    rm -f "$temporary"
}
trap cleanup EXIT

cp "$ROM" "$temporary"
mv -f "$temporary" "$OUT_DIR/$OUTPUT_NAME"
trap - EXIT

printf 'ready: %s\n' "$OUT_DIR/$OUTPUT_NAME"
