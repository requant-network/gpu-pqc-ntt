# Mixed-version ML-DSA relay and in-place upgrade

November 21, 2026. Separate native node processes on the Linux Ryzen 5 5500
server ran a baseline and an ML-DSA-enabled build from ReQuant revision
`967ce1831a37b56832bda4d52ddd29c428a18f9b`, using Rust 1.98.1. The legacy
observer uses the baseline node and consensus source. Its example wrapper
sets the same isolated lab chain identity and loopback network as the
patched fixture. This setup measures wire compatibility at a shared
source revision. Both builds retain the same node version identifier.

## Trial

1. Start the legacy receiver at genesis. Confirm identical block wires
   through height 4, including the Ed25519 transaction that funds 64
   future ML-DSA owners and 16 Ed25519 owners. The old node can decode
   these ordinary output owner hashes.
2. Mine the first mixed block at height 5 and produce 29 more. Query
   header height, accepted chain height and the UTXO audit separately.
   The old receiver can see later headers but keeps the same accepted
   height and UTXO audit as before the first mixed block. Its RPC cannot
   return that block.
3. Shut down the old receiver and start the ML-DSA-enabled receiver
   using the same directory. Verify the retained height-4 snapshot,
   exact catch-up to height 34, and every encoded block from the
   producer. Confirm 30 further blocks on the upgraded receiver and
   verify all 64 wires and the final audit again after reopening it.
4. Open a temporary copy of only the final block store under the old
   binary. The baseline raw replay again reaches height 4. This
   checks replay without a snapshot from the newer binary.

| Trial | Highest observed legacy header | Accepted height before upgrade | Upgrade and sync to height 34 |
| --- | ---: | ---: | ---: |
| [1](results-1.json) | 33 | 4 | 584.247 ms |
| [2](results-2.json) | 33 | 4 | 582.826 ms |
| [3](results-3.json) | 33 | 4 | 577.515 ms |

The legacy observer replayed the same four accepted blocks in each
raw-store test. The upgraded receiver matched all 64 block encodings in
all three trials, including after reopening the store. Time is measured
from launching the upgraded process through the first observation of
the exact height-34 tip. The median was 582.826 ms and covers process
startup, loopback sync, validation and RPC polling.

The funded output alone can be accepted by an old node. The first
transaction that spends it using the new signature changes the wire
format and prevents that node from accepting the block, even while
it receives headers. Checking header progress or the node's version
string alone would have missed this gap. For migration design, require
evidence of spend support before moving funds to the new owner type.
An in-place upgrade caught up in this fixture.

## Windows upgrade and full-store rollback across hosts

On November 22 the same trial ran with a Linux x86-64 producer
(Ryzen 5 5500) and a Windows x86-64 receiver (Ryzen 7 8745HS).
The physical LAN carried the P2P TCP stream inside a loopback-bound SSH
forward. All three builds used Rust 1.98.1 release mode. The two Windows
receivers shared one test directory in each trial, and the raw-store and
full-store rollback probes used separate disposable copies.

| Trial | Legacy header height | Accepted height before upgrade | Upgrade and sync to height 34 |
| --- | ---: | ---: | ---: |
| [1](cross-host-results-1.json) | 33 | 4 | 759.051 ms |
| [2](cross-host-results-2.json) | 33 | 4 | 704.134 ms |
| [3](cross-host-results-3.json) | 33 | 4 | 708.187 ms |

The upgraded Windows node restored the height-4 snapshot and matched 64
block encodings and the producer's supply and UTXO audit after a restart.
The old node replayed the raw block log up to height 4. Given the
complete upgraded directory, the old node instead reported height 64
and returned the same audit after restoring the new snapshot.
`getblock` failed for mixed blocks 5 and 64 with `block body not stored`.
The same process could still read all four early blocks. A graceful
shutdown rewrote `chainstate.bin` on the copied store and left
`blocks.dat` byte-identical. The upgraded build reopened that same copy
and matched all 64 block encodings and the full audit again.

A snapshot-backed height and a matching UTXO audit are insufficient
to qualify rollback. A rollout check should cover block-body decoding
and replay with mixed spends. Keep an independent pre-upgrade copy
before trying an older binary. The experiment did not test a rollback
followed by additional blocks or a reorg. Mixed production releases and
network activation remain separate qualification steps.

## Reproduce

Prepare two disposable source copies at the pinned base. In the
baseline copy, put [legacy_observer.rs](legacy_observer.rs) into
`crates/node/examples/legacy_observer.rs`.

```sh
cargo build --locked --offline --release -p requant-node --example legacy_observer
```

Build the upgraded example in the other copy using the
[cross-host fixture instructions](../cross-host/README.md#reproduce).
Then run both binaries on the same Linux host from the research tree.

```text
python3 compare.py /ABSOLUTE/UPGRADED/relay_lab /ABSOLUTE/BASELINE/legacy_observer
```

On Windows, add the SSH destination and paths to run the producer
on Linux. The lab scratch directory must already exist there.

```text
python compare.py WINDOWS_UPGRADED_EXE WINDOWS_LEGACY_EXE --remote USER@HOST /ABSOLUTE/LINUX/relay_lab /ABSOLUTE/LINUX/SCRATCH SSH_IDENTITY_FILE
```
