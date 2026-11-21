# Native ML-DSA block relay with CUDA signature batches

This isolated ReQuant lab uses the native block acceptance path
to validate Ed25519 and ML-DSA-44 transactions on one loopback
chain. It measures the signature phase inside the block path and
the time for a receiver to reach the same encoded native tip.
The GPU selector is an opt-in in the disposable source overlay.

## Protocol

Generate a disposable chain with four funding-prefix blocks and
sixty load blocks. Each load block carries 64 ML-DSA and 16 Ed25519
transfers. Restart the producer as a peer serving the stored chain.
Start receivers with empty stores and run three rotating rounds
of CPU, CUDA and CUDA with the device hidden.

The candidate takes single-input ML-DSA spends from a structurally
checked block and validates their signatures in chunks of 32.
Other transaction signatures use their original validation.
A GPU error or an unsuccessful verdict reruns the stock signature
checks in block order. Block and UTXO rules still run in the native
acceptance path. The selector applies only to the isolated
`pqc-lab` chain ID.

For each receiver, record the time from starting its process to
the exact height-64 tip. Check every block's encoded bytes and the
full supply audit. Shut down, reopen the same store through the
CPU path and check the same wires and audit again. Record signature
times per block and separate the first mixed block from the warm
blocks.

The follow-up rotates four paths over one regenerated chain. It
adds the older receiver that retries CUDA at every mixed block
when no device is visible. Both binaries use the same CUDA
library and native chain revision.

## Initial results

| Path | First mixed signature check | Warm signatures per block | Startup through exact tip |
| --- | ---: | ---: | ---: |
| CPU | 10.618 ms | 10.532 ms | 1.239 s |
| CUDA | 162.392 ms | 7.250 ms | 1.185 s |
| Device hidden, retry CUDA each block | 43.464 ms | 13.806 ms | 1.465 s |

These are medians across three matching chains on the LAN Ryzen 5 5500
and RTX 5060 Ti. Each run matched all 64 block wires and the supply
audit before and after restart. Every CUDA receiver processed 3,840
ML-DSA signatures with the GPU branch. The initial fallback path paid
the attempt and preparation cost again on every mixed block.

The native controls refused an altered signed output and a duplicate
spend. A block containing both a parent and child ML-DSA transaction
and an independent Ed25519 payment confirmed successfully in all
three paths.
[Measurements](baseline/report.json) · [Run output](baseline/campaign.txt) ·
[CPU control](baseline/controls-cpu.txt) · [CUDA control](baseline/controls-cuda.txt) ·
[Fallback control](baseline/controls-fallback.txt)

## Missing-device handling

The updated receiver records `cudaErrorNoDevice` as a permanent
condition for that process and switches to the ordinary CPU
checks. An invalid signature or a different CUDA error leaves
later GPU attempts available.

| Same block stream, four receivers per mode | First mixed signature check | Warm signatures per block | Startup through exact tip |
| --- | ---: | ---: | ---: |
| CPU | 10.54 ms | 10.41 ms | 1.253 s |
| CUDA | 170.36 ms | 7.46 ms | 1.234 s |
| Device hidden, CPU after the first failure | 48.95 ms | 10.41 ms | 1.287 s |
| Device hidden, repeated attempts | 50.40 ms | 13.77 ms | 1.451 s |

Each updated receiver with the device hidden attempted CUDA on
the first mixed block and verified the remaining 59 mixed blocks
directly on CPU. CUDA reduced warm signature time by 28.3% on the
active RTX. The first GPU call accounts for most of the difference
between the warm signature speedup and the whole-sync timing.
The experiment covers one prebuilt backlog served by loopback
TCP on one host.

All 16 receivers matched all 64 block encodings and the same
UTXO supply audit before and after reopening. The altered
signature and duplicate spend remained refused. The valid
parent-and-child block still confirmed. Thirty CPU consensus
regressions, the two-node PQC relay regression and an ordinary
regtest relay with the lab selector set passed.
[Measurements](no-device/latched-report.json) · [Run output](no-device/latched-campaign.txt) ·
[CPU control](no-device/latched-control-cpu.txt) · [CUDA control](no-device/latched-control-cuda.txt) ·
[Device-hidden control](no-device/latched-control-fallback.txt) ·
[Consensus regression](no-device/consensus-regression.txt) ·
[PQC relay regression](no-device/relay-regression.txt) · [Ordinary regtest](no-device/regtest-gate.txt) ·
[Toolchain and input pins](no-device/environment.txt)

## Reproduce

Prepare the disposable [CUDA wallet lab](../README.md#reproduce)
with the documented native ReQuant base. Apply the
[native relay patch](../../native-relay/relay.patch) and copy the
relay wrapper and test source as in the
[relay setup](../../native-relay/cross-host/README.md#reproduce).
Create `crates/node/examples` if the base archive omits the empty
directory. Normalize patch line endings when using a checkout
with CRLF.

```sh
python3 "$STUDY/wallet-lab/cuda-verify/block-relay/install.py" "$NATIVE"
cargo build --locked --offline --release -p requant-node --example relay_lab
export CUDA_VISIBLE_DEVICES="$LAB_RTX_UUID"
flock /tmp/requant-new-studies-gpu.lock taskset -c 0,1,2,3 python3 -B \
    "$STUDY/wallet-lab/cuda-verify/block-relay/measure.py" \
    "$NATIVE/target/release/examples/relay_lab" new-report.json
```

For the four-mode comparison, add
`--retry-binary /path/to/a037734-relay-lab`. Build that receiver
from the [initial experiment revision](https://github.com/requant-network/gpu-pqc-ntt/tree/a0377341ae35c90dc02b48ab87e6a5170e2e69dd/wallet-lab/cuda-verify/block-relay)
with the same native base and CUDA library.

Use the CUDA library and linker paths from the wallet lab
instructions. Keep all processes on loopback. The consensus
control runs the same test with `REQUANT_PQC_BLOCK_LAB=cpu`,
`REQUANT_PQC_BLOCK_LAB=cuda` and then `cuda` with
`CUDA_VISIBLE_DEVICES` empty.

```sh
cargo test --locked --offline --release -p requant-consensus --test pqc_wallet \
    ml_dsa_batch_preserves_block_signature_and_utxo_rules -- --nocapture
```
