# Native ML-DSA block relay with CUDA signature batches

This isolated ReQuant lab uses the native block acceptance path
to validate Ed25519 and ML-DSA-44 transactions on one loopback
chain. It measures the signature phase inside the block path and
the time for a receiver to reach the same encoded native tip.

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

Use the CUDA library and linker paths from the wallet lab
instructions. Keep all processes on loopback. The consensus
control runs the same test with `REQUANT_PQC_BLOCK_LAB=cpu`,
`REQUANT_PQC_BLOCK_LAB=cuda` and then `cuda` with
`CUDA_VISIBLE_DEVICES` empty.

```sh
cargo test --locked --offline --release -p requant-consensus --test pqc_wallet \
    ml_dsa_batch_preserves_block_signature_and_utxo_rules -- --nocapture
```
