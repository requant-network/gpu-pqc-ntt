# Validation with a second CUDA process

November 21, 2026. Four alternating idle / busy pairs ran the same
[virtual-arrival test](../README.md) on the LAN Ryzen 5 5500 and RTX 5060 Ti.
The validator and the competing process were pinned to separate physical
CPU cores. The competitor issued continuous 32-item calls through the same
synchronous CUDA reconstruction library, using fixed public coefficients
in a separate process and CUDA context. A ready signal followed the
first successful call. The competitor ran until the validator exited,
completed 15,456–17,728 calls per capture and reproduced the same output
digest. The eight validator runs checked 245,760 repeated verdicts and
reported no fallback. [Environment and binary hashes](environment.log.txt)
· [Per-mode medians and load counters](results.json).

## Results

Each cell is a median of four runs, for the 50% ML-DSA mix. The queue uses
the real service time of each call and a synthetic arrival clock.

| Arrival schedule and policy | Service per wire, idle → busy | Modeled p95, idle → busy |
| --- | ---: | ---: |
| 1,000/s smooth, CPU | 101.81 → 107.55 µs | 0.169 → 0.197 ms |
| 1,000/s smooth, immediate CUDA | 89.49 → 134.41 µs | 0.137 → 0.259 ms |
| 10,000/s smooth, CPU | 101.82 → 108.65 µs | 1.935 → 7.956 ms |
| 10,000/s smooth, immediate CUDA | 89.16 → 101.55 µs | 0.167 → 4.868 ms |
| 3,200/s in bursts of 32, CPU | 102.10 → 107.47 µs | 3.223 → 3.591 ms |
| 3,200/s in bursts of 32, immediate CUDA | 75.83 → 86.51 µs | 2.447 → 3.182 ms |

The modeled p95 of immediate CUDA grew in all four pairs for each listed
schedule at 50% ML-DSA. At 10,000/s, service time was close to the 100 µs arrival
interval, making the tail sensitive to small service changes and host
jitter. The four busy p95 values ranged from 0.940 to 9.363 ms. The CPU
control also moved even on its own core. This trial measures the effect
of adding a competing process on the shared host, including any shared
CPU, memory and PCIe effects.

Keep immediate dispatch as the idle reference. The busy 1,000/s case
favored CPU at p95, while the busy burst case favored CUDA. A fixed rule
that sends every eligible input to a shared card would miss this
difference. An integration needs measured device availability and
queueing latency under its real concurrent workloads. This test uses a
steady reconstruction competitor, warm validation and one serial queue.
The measurements do not establish live node or network performance.

## Reproduce

Use the [pinned native wallet build](../../README.md#reproduce). Run the
script below with `LD_LIBRARY_PATH` pointing to the CUDA runtime and the
lab library directory. Use explicit logical CPU IDs from distinct
physical cores and select the intended GPU UUID. Reserve the device for
the duration of the run on a shared host. Run this command from
`wallet-lab/cuda-verify/queue`.

```sh
CUDA_VISIBLE_DEVICES="$GPU_UUID" PQC_CUDA_LIBRARY="$LAB_BUILD/libpqc_verify_lab.so" \
  PQC_VALIDATOR_CPU=2 PQC_LOAD_CPU=4 bash contended.sh "$WALLET_TEST" contention-new
```

The script lives one directory above this report. Its output includes the
eight wallet logs, four competing-process logs and environment details.
For a new summary, place the `*.log.txt` captures in `queue/contention`
and run `python3 summarize.py --contention` from `cuda-verify`.
[Runner](../contended.sh) · [First idle run](idle-1.log.txt) ·
[First busy run](busy-1.log.txt) · [First competing-process log](load-1.log.txt).
