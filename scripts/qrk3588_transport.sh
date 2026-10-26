#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (c) 2026 ninozhang001@gmail.com
# Internal process backend. Python owns builds, configuration and workspace locking.

qrk_require_artifact() {
	if [ ! -f "$2" ] || [ ! -r "$2" ] || [ ! -s "$2" ]; then
		echo "error: $1 artifact is missing, unreadable or empty: $2" >&2
		return 1
	fi
}
wait_uart_pattern() {
	local pattern=$1 check_status=${2:-yes}
	local uart_input=${QRKUPDATE_READ_FD:-3}
	local deadline=$((SECONDS + QRKUPDATE_TIMEOUT)) line='' character
	local admission=no target=${QRKUPDATE_CHECK_ACTION:-}
	while [ "$SECONDS" -lt "$deadline" ]; do
		if IFS= read -r -N 1 -t 1 character <&"$uart_input"; then
			printf '%s' "$character"
			if [[ "$character" == $'\r' || "$character" == $'\n' ]]; then
				# Require complete protocol lines, including a zero admission code.
				if [[ -n "$target" && "$line" == "qrk_upgrade: ${target}_admission=PASS (0)" ]]; then
					admission=yes
				fi
				if [[ "$check_status" == yes && "$line" =~ ^QRK_UPGRADE_STATUS=([0-9]+)$ ]]; then
					if [ "${BASH_REMATCH[1]}" != 0 ]; then
						printf '\nerror: U-Boot upgrade failed (status %s)\n' "${BASH_REMATCH[1]}" >&2
						return 1
					fi
					if [[ "$pattern" == QRK_UPGRADE_STATUS=0 ]]; then
						if [[ -n "$target" && "$admission" != yes ]]; then
							echo "error: $target admission missing or blocked" >&2
							return 1
						fi
						return 0
					fi
				fi
				line=''
			else
				line+=$character
				# Console prompts and XMODEM requests need no terminating newline.
				if [[ "$pattern" != QRK_UPGRADE_STATUS=0 ]]; then
					case "$line" in *"$pattern"*) return 0;; esac
				fi
			fi
		fi
	done
	echo "error: timed out waiting for: $pattern" >&2
	return 1
}

send_uart_command() {
	local command=$1 character i
	# Pace console input: the board may use a polling UART with a small FIFO.
	for ((i = 0; i < ${#command}; i++)); do
		character=${command:i:1}
		printf '%s' "$character" >&3 || return 1
		sleep 0.002
	done
	printf '\r' >&3
}

qrk_wait_uboot_console() {
	local uart_input=${QRKUPDATE_READ_FD:-3}
	local deadline=$((SECONDS + QRKUPDATE_TIMEOUT)) line='' character
	local probing=no ready=no

	echo "Upgrade image ready. Waiting for U-Boot; power-cycle the board now if it is running Linux."
	# Cancel partial input if already at the console, without repeating a command.
	printf '\003' >&3 || return 1
	while [ "$SECONDS" -lt "$deadline" ]; do
		if ! IFS= read -r -N 1 -t 1 character <&"$uart_input"; then
			continue
		fi
		printf '%s' "$character"
		if [[ "$character" == $'\r' || "$character" == $'\n' ]]; then
			# Match a result line, not the echoed probe command.
			if [[ "$probing" == yes && "$line" == QRK_UART_READY ]]; then
				ready=yes
			fi
			line=''
			continue
		fi
		line+=$character
		if [[ "$line" == *'Hit any key to stop autoboot:'* ]]; then
			printf '\003' >&3 || return 1
			probing=no
			ready=no
			line=''
		elif [[ "$line" == '=> ' ]]; then
			if [[ "$ready" == yes ]]; then
				return 0
			fi
			send_uart_command 'echo QRK_UART_READY' || return 1
			probing=yes
			line=''
		fi
		# Bound memory while observing arbitrary boot output.
		if (( ${#line} > 256 )); then
			line=${line: -256}
		fi
	done
	echo "error: U-Boot console unavailable; power-cycle the board while the upgrade script is waiting" >&2
	return 1
}

qrk_upgrade_via_console() (
	# Transfer request and selected console device.
	local action=$1 image=$2
	local transport=${3:-xmodem} locator=${4:-}
	local uart=$QRKUPDATE_UART

	# Reader lifetime and saved terminal state for the EXIT cleanup.
	local QRKUPDATE_READ_FD=3
	local uart_reader_pid='' uart_state

	# Validation scratch value and transfer result (failure until confirmed).
	local address status=1

	qrk_require_artifact upgrade "$image" || return 1
	if [ "$transport" = xmodem ]; then
		command -v sx >/dev/null 2>&1 || { echo "error: sx is required" >&2; return 1; }
	fi
	for address in "$QRKUPDATE_SERVERIP" "$QRKUPDATE_IPADDR"; do
		if [[ -n "$address" && ! "$address" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
			echo "error: network addresses must be IPv4 literals" >&2
			return 1
		fi
	done
	uart_state=$(stty -F "$uart" -g) || return 1
	trap 'if [[ -n "$uart_reader_pid" ]]; then
		kill "$uart_reader_pid" 2>/dev/null || :
		wait "$uart_reader_pid" 2>/dev/null || :
	fi
	exec 4<&-; exec 3>&-; stty -F "$uart" "$uart_state"' EXIT
	trap 'exit 130' INT
	trap 'exit 143' TERM
	stty -F "$uart" 1500000 raw -echo -ixon -ixoff -crtscts || return 1
	exec 3<>"$uart" || { stty -F "$uart" "$uart_state"; return 1; }
	if [ "$transport" = tftp ]; then
		# Bash read changes tty settings for each character. Keep the UART
		# raw and drain bursts continuously; parse a pipe instead of a tty.
		exec 4< <(cat <&3)
		uart_reader_pid=$!
		QRKUPDATE_READ_FD=4
	fi
	qrk_wait_uboot_console || return 1
	if [ "$transport" = tftp ]; then
		if [ -n "$QRKUPDATE_SERVERIP" ]; then
			send_uart_command "setenv serverip $QRKUPDATE_SERVERIP" || return 1
			wait_uart_pattern '=> ' || return 1
		fi
		if [ -n "$QRKUPDATE_IPADDR" ]; then
			send_uart_command "setenv ipaddr $QRKUPDATE_IPADDR" || return 1
			wait_uart_pattern '=> ' || return 1
		fi
		send_uart_command "qrk_upgrade $action tftp $locator; echo QRK_UPGRADE_STATUS=\$?" || return 1
		wait_uart_pattern 'QRK_UPGRADE_STATUS=0' && status=0
	else
		send_uart_command "qrk_upgrade $action xmodem; echo QRK_UPGRADE_STATUS=\$?" || return 1
		if wait_uart_pattern 'Send upgrade image via XMODEM' &&
		   sx -X -k -b "$image" <&3 >&3; then
			wait_uart_pattern 'QRK_UPGRADE_STATUS=0' && status=0
		fi
	fi
	return "$status"
)

qrk_upgrade_transfer() {
	local action=$1 image=$2

	# TFTP publication state; XMODEM consumes the input image directly.
	local tftp_dir filename temporary
	case "$QRKUPDATE_TRANSPORT" in
	xmodem) qrk_upgrade_via_console "$action" "$image" xmodem ;;
	tftp)
		qrk_require_artifact upgrade "$image" || return 1
		tftp_dir=${QRKUPDATE_TFTP_DIR:-$(dirname -- "$image")}
		if [ ! -d "$tftp_dir" ] || [ ! -w "$tftp_dir" ]; then
			echo "error: TFTP root must exist and be writable: $tftp_dir" >&2
			return 1
		fi
		filename=${image##*/}
		[[ "$filename" =~ ^[a-zA-Z0-9_-]+\.upg$ ]] || return 1
		if [ "$image" -ef "$tftp_dir/$filename" ]; then
			chmod 644 "$image" || return 1
		else
			temporary=$(mktemp "$tftp_dir/.qrk-upgrade.XXXXXX") || return 1
			if ! cp "$image" "$temporary" || ! chmod 644 "$temporary" ||
			   ! mv -f "$temporary" "$tftp_dir/$filename"; then
				rm -f "$temporary"
				return 1
			fi
		fi
		echo "TFTP upgrade image: $tftp_dir/$filename"
		qrk_upgrade_via_console "$action" "$image" tftp "$filename"
		;;
	*) echo "error: QRKUPDATE_TRANSPORT must be tftp or xmodem" >&2; return 1 ;;
	esac
}

qrk_bl1_require_fip() {
	qrk_require_artifact FIP "$fip_bin" || return 1
	command -v sx >/dev/null 2>&1 || {
		echo "error: sx is required" >&2
		return 1
	}
}

# Send an already prepared FIP; builds must finish before opening the UART.
qrk_bl1_send_fip() (
	local uart=$QRKUPDATE_UART
	local uart_state status

	qrk_bl1_require_fip || return 1
	uart_state=$(stty -F "$uart" -g) || return 1
	trap 'stty -F "$uart" "$uart_state"' EXIT
	trap 'exit 130' INT
	trap 'exit 143' TERM
	stty -F "$uart" 1500000 raw -echo -ixon -ixoff -crtscts || return 1
	printf 'Sending %s over %s at 1500000 baud with 1 KiB XMODEM blocks\n' \
		"$fip_bin" "$uart"
	sx -X -k -b "$fip_bin" < "$uart" > "$uart"
	status=$?
	return "$status"
)

# Sourcing is supported only for host transport tests.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    # Let the active console/sender subshell finish its tty restoration on TERM.
    trap 'wait; exit 143' TERM
    trap 'wait; exit 130' INT
	case "${1:-}" in
		transfer)
			[[ $# == 4 && "$2" =~ ^(apply|recover|check)$ ]] || exit 2
			QRKUPDATE_CHECK_ACTION=$4
			if [[ "$2" == check ]]; then
				[[ "$4" =~ ^(apply|recover)$ ]] || exit 2
			else
				[[ -z "$4" ]] || exit 2
			fi
			qrk_upgrade_transfer "$2" "$3"
			;;
		send-fip)
			[[ $# == 2 ]] || exit 2
			fip_bin=$2
			qrk_bl1_send_fip
			;;
		*) echo "error: internal transport command required" >&2; exit 2 ;;
	esac
fi
