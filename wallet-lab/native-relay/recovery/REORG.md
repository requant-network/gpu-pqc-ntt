# Reorganize a recovered ReQuant lab node

Both nodes used the same upgraded lab build on the local Linux server.
They ran on isolated loopback on the synthetic `pqc-lab` chain. The producer
stopped at height 4 after funding an ML-DSA output. The test preserved its
block log and snapshot and opened a working copy as the recovered node.

1. The recovered node added a block spending the ML-DSA output and another
   block above it. The producer grew an independent branch from height 4
   to height 7 without spending that output.
2. The recovered node joined the producer over P2P. It selected the
   producer's branch, marked both detached blocks outside the active chain,
   restored the funding output, removed the detached spend's output and
   readmitted the detached spend to its mempool. Both UTXO audits matched.
3. The producer spent the restored output to a different recipient at
   height 8. The recovered node accepted the block. After reopening with no
   peers, it read all eight active block bodies and matched the producer's
   audit and replacement output. Its mempool no longer held the detached
   spend. The preserved backup's block log and snapshot remained byte-for-byte
   intact.

All three [trial records](reorg-results.json) passed. Observed time from
connecting the nodes to selecting the height-7 tip was about 20 ms, using
a 20 ms polling interval. The complete two-node integration target passed
all 10 tests on the same server, including the existing mixed relay test.
This measures a two-block reorganization after recovery with one lab build.
Production release qualification and legacy rollback after further writes
remain open. The [preceding recovery rehearsal](README.md) covers starting
from a verified pre-upgrade copy and starting from an empty directory.

## Run the lab test

Apply the native chain and node relay patches to a disposable copy at the
[relay fixture's pinned ReQuant base](../cross-host/README.md#reproduce).
Install the native relay fixture as described there, then copy
[`reorg.rs`](reorg.rs) to `crates/node/tests/pqc_recovery_reorg.inc`. Append
this module to `crates/node/tests/two_nodes.rs`.

```rust
mod pqc_recovery_reorg {
    include!("pqc_recovery_reorg.inc");
}
```

Run with a fresh writable `TMPDIR` and cached dependencies. The fixture was
checked with Rust 1.98.1.

```sh
cargo test --offline --locked --release -j 2 -p requant-node --test two_nodes \
  pqc_recovery_reorg::pqc_recovered_node_reorg -- --exact --nocapture
cargo test --offline --locked --release -j 2 -p requant-node --test two_nodes \
  -- --test-threads=1
```
