#!/usr/bin/env bash
#
# generate_service.sh — generate (and optionally install) a systemd service
# file for a daemon that only runs during configured 24-hour time windows.
#
# Times are 24-hour HH:MM values supplied in start/end pairs:
#
#     07:00 23:00              -> runs 07:00 .. 23:00
#     07:00 11:00 14:00 23:00  -> runs 07:00 .. 11:00, then 14:00 .. 23:00
#
# The generated unit is always-on: enabled at boot, it idles outside the
# windows, starts the command when a window opens, stops it when the window
# closes, and systemd restarts it (forever, StartLimitIntervalSec=0) if it
# ever crashes.
#
set -euo pipefail

PROGRAM_NAME="$(basename "$0")"
DEFAULT_UNIT_DIR="/etc/systemd/system"
UNIT_DIR="${SYSTEMD_UNIT_DIR:-$DEFAULT_UNIT_DIR}"
POLL_SECONDS="${WINDOW_POLL_SECONDS:-30}"

SYSTEMCTL_EXPLICIT=0
if [ -n "${SYSTEMCTL_CMD+x}" ]; then
  SYSTEMCTL_EXPLICIT=1
fi
SYSTEMCTL_CMD_VALUE="${SYSTEMCTL_CMD-systemctl}"

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

note() {
  printf '%s\n' "$*"
}

usage() {
  cat <<EOF
Usage: $PROGRAM_NAME [OPTIONS] TIME [TIME ...]

Generate a systemd service file for a daemon that runs only during the
given 24-hour windows. The unit starts at boot, idles outside the schedule,
launches your command when a window opens, stops it when the window closes,
and is restarted by systemd after any crash (never rate-limited out of
existence). Times are HH:MM values given in start/end pairs:

  $PROGRAM_NAME -c "./mydaemon" 07:00 23:00
      runs 07:00-23:00

  $PROGRAM_NAME -c "./mydaemon" 07:00 11:00 14:00 23:00
      runs 07:00-11:00, and again 14:00-23:00

An end time of 24:00 means midnight; a window that ends before it starts
(e.g. 22:00 02:00) wraps past midnight; equal start/end means all day.

Options:
  -c, --command CMD       command the daemon executes (required)
  -n, --name NAME         unit name without the .service suffix
                          (default: windowed-daemon)
  -d, --description TEXT  unit description
  -u, --user USER         user the service runs as (default: current user)
  -w, --workdir DIR       working directory of the command (default: the
                          directory this script runs from)
  -f, --times-file FILE   read times from FILE instead of the arguments
                          (whitespace/comma separated, # starts a comment)
  -o, --output FILE       where to write the unit file
                          (default: ./NAME.service)
      --install           after inspection, install/update the unit file in
                          $DEFAULT_UNIT_DIR and enable/restart it (needs root)
  -y, --yes               with --install: do not ask for confirmation
  -h, --help              show this help and exit

Environment:
  SYSTEMD_UNIT_DIR        install destination (default: $DEFAULT_UNIT_DIR);
                          when overridden the systemctl steps are skipped
                          unless SYSTEMCTL_CMD is set explicitly
  SYSTEMCTL_CMD           systemctl command to use, e.g. "systemctl --user";
                          set to "" to skip the systemctl steps entirely
  WINDOW_POLL_SECONDS     how often the gate re-checks the clock (default: 30)

Typical flow:
  $PROGRAM_NAME -c "./mydaemon" 07:00 23:00              # write + inspect
  sudo $PROGRAM_NAME --install -c "./mydaemon" 07:00 23:00   # install/update
EOF
}

# --- argument parsing --------------------------------------------------------

name="windowed-daemon"
description=""
run_user="$(id -un)"
command=""
workdir="$PWD"
output=""
times_file=""
do_install=0
assume_yes=0
times=()

while [ $# -gt 0 ]; do
  case "$1" in
    -h | --help)
      usage
      exit 0
      ;;
    -c | --command)
      [ $# -ge 2 ] || die "$1 needs a value"
      command="$2"
      shift 2
      ;;
    -n | --name)
      [ $# -ge 2 ] || die "$1 needs a value"
      name="${2%.service}"
      shift 2
      ;;
    -d | --description)
      [ $# -ge 2 ] || die "$1 needs a value"
      description="$2"
      shift 2
      ;;
    -u | --user)
      [ $# -ge 2 ] || die "$1 needs a value"
      run_user="$2"
      shift 2
      ;;
    -w | --workdir)
      [ $# -ge 2 ] || die "$1 needs a value"
      workdir="$2"
      shift 2
      ;;
    -f | --times-file)
      [ $# -ge 2 ] || die "$1 needs a value"
      times_file="$2"
      shift 2
      ;;
    -o | --output)
      [ $# -ge 2 ] || die "$1 needs a value"
      output="$2"
      shift 2
      ;;
    --install)
      do_install=1
      shift
      ;;
    -y | --yes)
      assume_yes=1
      shift
      ;;
    --)
      shift
      times+=("$@")
      break
      ;;
    -*)
      die "unknown option: $1 (see --help)"
      ;;
    *)
      times+=("$1")
      shift
      ;;
  esac
done

# --- time collection ---------------------------------------------------------

append_times_from_file() {
  local file="$1" line part
  local parts=()
  [ -r "$file" ] || die "cannot read times file: $file"
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%%#*}"
    line="${line//[][(),;]/ }"
    parts=()
    read -r -a parts <<<"$line" || :
    for part in "${parts[@]}"; do
      times+=("$part")
    done
  done <"$file"
}

if [ -n "$times_file" ]; then
  append_times_from_file "$times_file"
fi

# --- validation --------------------------------------------------------------

[ ${#times[@]} -ge 2 ] ||
  die "need at least two times (a start and an end); see --help"
[ $((${#times[@]} % 2)) -eq 0 ] ||
  die "got ${#times[@]} times, they must come in start/end pairs (e.g. 07:00 23:00 14:00 23:00)"

for t in "${times[@]}"; do
  [[ "$t" =~ ^([01][0-9]|2[0-3]):[0-5][0-9]$ || "$t" == "24:00" ]] ||
    die "invalid time: '$t' (expected 24-hour HH:MM, e.g. 07:00 or 23:30)"
done

[[ "$name" =~ ^[A-Za-z0-9@_.:-]+$ ]] ||
  die "invalid unit name: '$name' (letters, digits, . _ @ : - only)"
[ -n "$command" ] || die "--command is required"
[ -d "$workdir" ] || die "working directory does not exist: $workdir"
[[ "$POLL_SECONDS" =~ ^[1-9][0-9]*$ ]] ||
  die "WINDOW_POLL_SECONDS must be a positive integer, got: '$POLL_SECONDS'"
# How often a running command is checked against the clock: never slower
# than 5s so a window closes promptly.
CHECK_SECONDS=$((POLL_SECONDS < 5 ? POLL_SECONDS : 5))

# WorkingDirectory= does not unquote, so the path must be a plain word.
case "$workdir" in
  *[[:space:]]* | *\'* | *\"* | *\\*)
    die "working directory must not contain whitespace, quotes or backslashes: $workdir"
    ;;
esac

path_value="${PATH:-/usr/bin:/bin}"
case "$path_value" in
  *\'* | *\"* | *\\* | *$'\n'*) die "PATH contains characters that cannot be written into a unit file" ;;
esac

case "$description" in
  *\'* | *\"* | *\\* | *$'\n'*)
    die "description may not contain quotes, backslashes or newlines"
    ;;
esac

if ! id -u "$run_user" >/dev/null 2>&1; then
  note "warning: user '$run_user' does not exist on this machine" >&2
fi

# Split the command into words; the words are embedded into the gate script
# as a bash array, so shell metacharacters are not supported.
read -r -a cmd_words <<<"$command" || :
[ ${#cmd_words[@]} -gt 0 ] || die "command is empty"
cmd_str=""
for w in "${cmd_words[@]}"; do
  case "$w" in
    *\'* | *\"* | *\\* | *\`* | *\$*)
      die "unsupported character in command word '$w' (quotes, backslash, backtick and \$ are not allowed)"
      ;;
  esac
  cmd_str+=" \"$w\""
done

# --- build the windows and the gate script -----------------------------------

declare -a windows=()
for ((i = 0; i < ${#times[@]}; i += 2)); do
  start="${times[i]}"
  end="${times[i + 1]}"
  if [ "$start" == "24:00" ]; then
    die "start time cannot be 24:00 (only end times may be 24:00)"
  fi
  if [ "$start" == "$end" ]; then
    note "note: start and end '$start' are equal, using a 24-hour window" >&2
    windows+=("00:00-24:00")
  else
    windows+=("$start-$end")
  fi
done
windows_str="${windows[*]}"

# The gate script runs inside: ExecStart=/bin/bash -c '<gate>'
# Rules for its content (enforced below): no single quotes and no backslashes,
# because of systemd's quoting/C-escape rules. Every "$" and "%" is escaped
# to "$$" / "%%" afterwards so systemd passes them through verbatim.
gate="windows=\"${windows_str}\"; poll=${POLL_SECONDS}; check=${CHECK_SECONDS}; "
gate+="to_min() { local h=\"\${1%%:*}\" m=\"\${1##*:}\"; echo \$((10#\$h * 60 + 10#\$m)); }; "
gate+="in_window() { local cur hh mm w s e sm em; "
gate+="hh=\$(date +%H); mm=\$(date +%M); cur=\$((10#\$hh * 60 + 10#\$mm)); "
gate+="for w in \$windows; do s=\"\${w%-*}\"; e=\"\${w#*-}\"; "
gate+="sm=\$(to_min \"\$s\"); em=\$(to_min \"\$e\"); "
gate+="if [ \"\$sm\" -le \"\$em\" ]; then "
gate+="[ \"\$cur\" -ge \"\$sm\" ] && [ \"\$cur\" -lt \"\$em\" ] && return 0; "
gate+="else "
gate+="{ [ \"\$cur\" -ge \"\$sm\" ] || [ \"\$cur\" -lt \"\$em\" ]; } && return 0; "
gate+="fi; "
gate+="done; return 1; }; "
# stop_child: SIGTERM, up to 10s of grace, then SIGKILL, then reap.
gate+="stop_child() { local n=0; kill -TERM \"\$1\" 2>/dev/null; "
gate+="while kill -0 \"\$1\" 2>/dev/null && [ \"\$n\" -lt 10 ]; do sleep 1; n=\$((n + 1)); done; "
gate+="kill -KILL \"\$1\" 2>/dev/null || :; wait \"\$1\" 2>/dev/null || :; }; "
gate+="state=\"\"; "
gate+="while :; do "
gate+="if in_window; then "
gate+="state=\"\"; echo \"window-gate: inside a scheduled window, launching command\"; "
gate+="${cmd_str} & child=\$!; "
gate+="while :; do "
gate+="if ! kill -0 \"\$child\" 2>/dev/null; then "
gate+="wait \"\$child\"; rc=\$?; "
gate+="echo \"window-gate: command exited with status \$rc\"; exit \"\$rc\"; "
gate+="fi; "
gate+="if ! in_window; then "
gate+="echo \"window-gate: window closed, stopping command\"; "
gate+="stop_child \"\$child\"; child=\"\"; break; "
gate+="fi; "
gate+="sleep \"\$check\"; "
gate+="done; "
gate+="fi; "
gate+="if [ \"\$state\" != \"outside\" ]; then "
gate+="echo \"window-gate: outside scheduled windows (${windows_str}), checking every ${POLL_SECONDS}s\"; "
gate+="state=\"outside\"; "
gate+="fi; "
gate+="sleep \"\$poll\"; "
gate+="done"

case "$gate" in
  *"'"*) die "internal error: gate script contains a single quote" ;;
  *\\*) die "internal error: gate script contains a backslash" ;;
esac
bash -n -c "$gate" || die "internal error: generated gate script does not parse"

# Escape for systemd: %% -> % and $$ -> $ when the unit file is processed.
gate_escaped="${gate//%/%%}"
gate_escaped="${gate_escaped//\$/\$\$}"

# --- assemble the unit file --------------------------------------------------

if [ -z "$description" ]; then
  description="${name} (runs during ${windows_str})"
fi
description="${description//%/%%}"

unit_file="${name}.service"
output="${output:-./${unit_file}}"
out_dir="$(dirname "$output")"
[ -d "$out_dir" ] || die "output directory does not exist: $out_dir"

cat >"$output" <<EOF
# ---------------------------------------------------------------------------
# ${unit_file} — generated by ${PROGRAM_NAME} on $(date '+%Y-%m-%d %H:%M:%S %Z')
#
# Schedule (local time) : ${windows_str}
# Command               : ${command}
# Runs as               : ${run_user}
# Working directory     : ${workdir}
# Gate poll interval    : ${POLL_SECONDS}s (window end seen within ${CHECK_SECONDS}s)
#
# Regenerate + install  :
#   sudo ${PROGRAM_NAME} --install --name ${name} --command "${command}" ${times[*]}
# ---------------------------------------------------------------------------
[Unit]
Description=${description}
# Never give up: keep restarting the daemon no matter how often it crashes.
StartLimitIntervalSec=0

[Service]
Type=simple
User=${run_user}
WorkingDirectory=${workdir}
Environment="PATH=${path_value}"
ExecStart=/bin/bash -c '${gate_escaped}'
Restart=always
RestartSec=5
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
EOF
chmod 0644 "$output"

note "" >&2
note "--- generated ${output} ---" >&2
cat "$output"
note "----------------------------------------------------------------------------" >&2

# --- optional install/update --------------------------------------------------

install_unit() {
  local dest="${UNIT_DIR}/${unit_file}"
  local run_systemctl=1 changed=1

  if [ "$SYSTEMCTL_EXPLICIT" -eq 1 ]; then
    # SYSTEMCTL_CMD set explicitly: use it, unless it is the empty string
    if [ -n "$SYSTEMCTL_CMD_VALUE" ]; then
      run_systemctl=1
    else
      run_systemctl=0
    fi
  elif [ "$UNIT_DIR" != "$DEFAULT_UNIT_DIR" ]; then
    run_systemctl=0
  fi

  if [ "$UNIT_DIR" == "$DEFAULT_UNIT_DIR" ] && [ "$EUID" -ne 0 ]; then
    die "installing into $UNIT_DIR needs root - re-run with sudo, or set SYSTEMD_UNIT_DIR to stage the file elsewhere"
  fi
  if [ ! -d "$UNIT_DIR" ]; then
    mkdir -p "$UNIT_DIR" || die "cannot create install directory: $UNIT_DIR"
  fi
  [ -w "$UNIT_DIR" ] || die "no write permission on $UNIT_DIR"

  # The header carries a generation timestamp, so compare without it.
  if [ -f "$dest" ] &&
    diff -q <(grep -v "generated by" "$output") <(grep -v "generated by" "$dest") >/dev/null 2>&1; then
    changed=0
  fi

  if [ "$assume_yes" -eq 0 ]; then
    printf 'Install/update %s? [y/N] ' "$dest"
    local reply=""
    read -r reply || reply=""
    case "$reply" in
      y | Y | yes | YES) ;;
      *)
        note ""
        note "aborted - nothing installed (unit file left at $output)"
        return 0
        ;;
    esac
  fi

  install -m 0644 "$output" "$dest" || die "failed to copy $output to $dest"
  if [ "$changed" -eq 1 ]; then
    note "installed: $dest"
  else
    note "installed: $dest (unchanged)"
  fi

  if [ "$run_systemctl" -eq 0 ]; then
    note "systemctl steps skipped - run manually:"
    note "  systemctl daemon-reload && systemctl enable --now ${unit_file}"
    return 0
  fi

  local sc=()
  read -r -a sc <<<"$SYSTEMCTL_CMD_VALUE"
  [ ${#sc[@]} -gt 0 ] || die "systemctl command is empty"
  command -v "${sc[0]}" >/dev/null 2>&1 ||
    die "cannot find '${sc[0]}' - the unit file is installed at $dest; run 'systemctl daemon-reload' manually"

  "${sc[@]}" daemon-reload || die "systemctl daemon-reload failed"
  "${sc[@]}" enable "$unit_file" || die "failed to enable $unit_file"

  # Note: a command that exits quickly leaves the unit in "auto-restart",
  # which is-active does not report as active.
  local state=""
  state="$("${sc[@]}" is-active "$unit_file" 2>/dev/null || true)"
  case "$state" in
    active | activating | rearming | auto-restart)
      if [ "$changed" -eq 1 ]; then
        "${sc[@]}" restart "$unit_file" || die "failed to restart $unit_file"
        note "restarted: $unit_file (configuration changed)"
      else
        note "running:   $unit_file (no changes, no restart needed)"
      fi
      ;;
    *)
      "${sc[@]}" start "$unit_file" || die "failed to start $unit_file"
      note "started:   $unit_file"
      ;;
  esac

  local jc="journalctl"
  case " $SYSTEMCTL_CMD_VALUE " in
    *" --user "*) jc="journalctl --user" ;;
  esac
  note "logs: $jc -u ${unit_file} -f"
}

if [ "$do_install" -eq 1 ]; then
  install_unit
else
  note "review the file above, then re-run with --install to install it."
fi
