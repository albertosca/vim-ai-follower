#!/usr/bin/env bash
# Run COUNT busy loops for at most SECONDS, to reproduce failures that only
# show under load. Every loop is killed by pid on exit, Ctrl-C included.
#   scripts/load_burn.sh COUNT [SECONDS] [-- command ...]
# With a command, the loops run while it runs and stop when it ends; its
# exit status is the script's. COUNT may not exceed the CPU count.
set -euo pipefail

count="${1:?usage: load_burn.sh COUNT [SECONDS] [-- command ...]}"
shift
seconds=600
if [[ $# -gt 0 && "$1" != "--" ]]; then
  seconds="$1"
  shift
fi
[[ "${1:-}" == "--" ]] && shift

cpus="$(sysctl -n hw.ncpu 2>/dev/null || nproc)"
if (( count > cpus )); then
  echo "load_burn: $count loops exceeds the $cpus CPUs" >&2
  exit 2
fi

burners=()
stop_burners() {
  for pid in "${burners[@]}"; do
    kill "$pid" 2>/dev/null || true
  done
}
trap stop_burners EXIT

for (( index = 0; index < count; index++ )); do
  timeout_end=$(( SECONDS + seconds ))
  ( while (( SECONDS < timeout_end )); do :; done ) &
  burners+=("$!")
done

if [[ $# -gt 0 ]]; then
  "$@"
else
  wait
fi
