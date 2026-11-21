# Mixed-signature transaction validation

November 21, 2026. The isolated wallet lab grouped single-input ML-DSA-44
transactions for CUDA and checked Ed25519 and multi-input transactions on CPU.
Every transaction produced its own fee or error, in input order. An unavailable
CUDA device sent the eligible transactions through the native CPU verifier.
Each wire was checked independently against the same funded UTXO snapshot.
Block assembly and mempool admission are outside the experiment.

## Correctness and recovery

The CPU and mixed paths agreed on all **69,120** per-wire verdicts in the
timed runs. These are repeated checks of the deterministic corpus, including
864 checks of deliberately changed transactions. The benchmark used 256
transactions per pass, alternating the run order across six passes in each
of three fresh CPU/CUDA processes.

A separate nine-wire control included valid Ed25519 and ML-DSA transfers,
a transfer with both witness types, modified outputs under both signature
schemes, truncated bytes, a noncanonical ML-DSA input and a valid signature
spending an absent output. Every cyclic rotation preserved the CPU errors
and fees. With the CUDA device hidden, all nine rotations also matched via
the CPU fallback. The lab additionally rejected ML-DSA on legacy regtest,
validated 128 Ed25519 wires without calling CUDA and rejected invalid
batch sizes. The original wallet migration and NIST verification checks
passed, including 12 vectors dispatched to CUDA and 54 CPU regression tests.
[GPU controls and samples](mixed-1.log.txt) · [CPU fallback](fallback.log.txt) ·
[Wallet migration](wallet.log.txt) · [ACVP](acvp.log.txt) · [CPU regressions](cpu.log.txt).

## Warm measurements

Median of 18 passes, in microseconds per input wire on Ryzen 5 5500 and RTX
5060 Ti. The mixed path includes grouping, decoding, CPU signature work,
CUDA transfers and synchronization, and the final native UTXO check. An
outer batch of 32 contains 8 or 16 CUDA items at 25% or 50% ML-DSA, respectively.

| Workload | CPU at batch 32 | Mixed at batch 32 | CPU / mixed |
| --- | ---: | ---: | ---: |
| Ed25519 only | 40.09 | 43.79 | 0.92× |
| 25% ML-DSA | 69.72 | 60.82 | 1.15× |
| 50% ML-DSA | 98.96 | 76.49 | 1.29× |
| 50% ML-DSA, 16 changed wires per 256 | 98.86 | 76.25 | 1.30× |
| ML-DSA only | 157.53 | 108.34 | 1.45× |

Use the direct CPU path for Ed25519-only traffic. Batch 32 is a reasonable
starting point for another warm queue experiment with this hardware. At 25%
ML-DSA, batch 128 was about 0.29 µs per wire faster than batch 32. The
measurements start with batches in memory and exclude arrival wait, cold
CUDA startup, network relay, changes in chain state and deliberate GPU
contention. The [queue follow-up](../queue/README.md) measures service time
and models arrival wait for three synthetic schedules. No release or
network activation decision follows from the lab.
[All batch sizes and timing ratios](results.json) · [Environment and hashes](metadata.log.txt).

## Reproduce

Follow [the pinned native and CUDA build](../README.md#reproduce). Use the
updated installer from this checkout to add the mixed validator and harness.
Run the following from the native checkout with the library and linker
paths from the parent instructions. Repeat the first command in three
separate processes on the selected RTX and retain each output as
`mixed-N.log.txt` in this directory.

```sh
cargo test --offline --locked --release -p requant-consensus --test pqc_wallet \
  mixed::mixed_witness_batches -- --exact --nocapture
CUDA_VISIBLE_DEVICES='' cargo test --offline --locked --release \
  -p requant-consensus --test pqc_wallet mixed::mixed_witness_missing_device \
  -- --exact --ignored --nocapture
```

Run `python3 summarize.py --mixed` from the parent directory to rebuild
the summary. [Run 1](mixed-1.log.txt) · [Run 2](mixed-2.log.txt) · [Run 3](mixed-3.log.txt).
