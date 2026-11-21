#!/usr/bin/env bash
set -euo pipefail

wallet=${1:?path to the compiled pqc_wallet test executable}
output=${2:?new directory for captures}
library=${PQC_CUDA_LIBRARY:?path to libpqc_verify_lab.so}
validator_cpu=${PQC_VALIDATOR_CPU:?logical CPU for the validator}
load_cpu=${PQC_LOAD_CPU:?logical CPU on another physical core}
case ${CUDA_VISIBLE_DEVICES:-} in
  *,*) echo 'Select exactly one CUDA device' >&2; exit 2 ;;
  GPU-*) ;;
  *) echo 'Select exactly one device by CUDA_VISIBLE_DEVICES GPU UUID' >&2; exit 2 ;;
esac
test -x "$wallet"
test -f "$library"
validator_siblings=$(cat "/sys/devices/system/cpu/cpu$validator_cpu/topology/thread_siblings_list")
load_siblings=$(cat "/sys/devices/system/cpu/cpu$load_cpu/topology/thread_siblings_list")
test "$validator_siblings" != "$load_siblings"
mkdir "$output"

{
  lscpu | awk '/^Model name:/{print}'
  nvidia-smi -i "$CUDA_VISIBLE_DEVICES" --query-gpu=name,driver_version,utilization.gpu,memory.used --format=csv,noheader
  printf 'validator_cpu=%s siblings=%s load_cpu=%s siblings=%s\n' \
    "$validator_cpu" "$validator_siblings" "$load_cpu" "$load_siblings"
  sha256sum "$wallet" "$library" | awk '{n=split($2, p, "/"); print $1 "  " p[n]}'
} > "$output/environment.log.txt"

load_pid=
trap 'if [[ -n $load_pid ]]; then touch "$stop"; wait "$load_pid" || true; fi' EXIT

for trial in 1 2 3 4; do
  order=(idle busy)
  if (( trial % 2 == 0 )); then order=(busy idle); fi
  for mode in "${order[@]}"; do
    if [[ $mode == busy ]]; then
      ready="$output/busy-$trial.ready"
      stop="$output/busy-$trial.stop"
      taskset -c "$load_cpu" timeout 20s python3 -u - "$library" "$ready" "$stop" \
        > "$output/load-$trial.log.txt" 2>&1 <<'PY' &
import ctypes as ct
import hashlib
from pathlib import Path
import sys
import time

library, ready, stop = sys.argv[1:]
reconstruct = ct.CDLL(library).pqc_cuda_reconstruct
reconstruct.argtypes = (ct.POINTER(ct.c_uint32), ct.POINTER(ct.c_uint32), ct.c_int)
reconstruct.restype = ct.c_int
batch = 32
length = batch * 25 * 256
inputs = (ct.c_uint32 * length)(*((i * 104729 + 17) % 8380417 for i in range(length)))
outputs = (ct.c_uint32 * (batch * 4 * 256))()

def launch():
    code = reconstruct(inputs, outputs, batch)
    if code:
        raise RuntimeError(f"CUDA reconstruction failed with status {code}")

launch()
digest = hashlib.sha256(bytes(outputs)).hexdigest()
stop = Path(stop)
start = time.monotonic()
Path(ready).touch()
calls = 0
while not stop.exists():
    for _ in range(16):
        launch()
    calls += 16
elapsed = time.monotonic() - start
assert calls > 0 and hashlib.sha256(bytes(outputs)).hexdigest() == digest
print(f"load_batch={batch} calls={calls} seconds={elapsed:.6f} digest={digest}")
PY
      load_pid=$!
      until [[ -f $ready ]]; do
        kill -0 "$load_pid" 2>/dev/null || { cat "$output/load-$trial.log.txt" >&2; exit 1; }
        sleep 0.05
      done
    fi

    printf 'trial=%d mode=%s\n' "$trial" "$mode"
    taskset -c "$validator_cpu" timeout 15s "$wallet" mixed::virtual_arrival_queue \
      --exact --nocapture > "$output/$mode-$trial.log.txt" 2>&1
    if [[ $mode == busy ]]; then
      touch "$stop"
      wait "$load_pid"
      load_pid=
    fi
  done
done
