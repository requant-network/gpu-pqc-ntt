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
An in-place upgrade caught up in this fixture. Downgrading complete
state with a newer snapshot, testing mixed production releases and
choosing network activation rules require separate qualification.

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
