# Native ML-DSA block propagation across hosts

November 21, 2026. A Linux x86-64 producer on a Ryzen 5 5500 sent native
P2P transactions and blocks to a separate Windows x86-64 node process
on a Ryzen 7 8745HS. Both used Rust 1.98.1, one configured compute thread per node
and the isolated PQC lab chain. The physical LAN carried the
native TCP byte stream inside an authenticated SSH forward. Both native
listeners and the forward bound to loopback.

## Trial

1. Confirm the funding transfer at height 4. It creates 64 ML-DSA-44
   and 16 Ed25519 outputs.
2. Produce 30 blocks, targeting a 333 ms start-to-start period. Each block
   spends and renews all 80 outputs. The producer sends its block digest
   separately through its control pipe. The receiver obtains blocks via P2P.
3. Close the forward, confirm the receiver has dropped its peer, and
   produce 12 more blocks. The receiver stays at height 34.
4. Restore the forward and wait for the receiver to reach the producer's
   exact tip at height 46. Produce 18 more blocks and check height 64.
5. Compare each encoded block with the producer's SHA-256 digest and
   check the complete supply and UTXO digest. Stop the receiving process,
   reopen its directory with zero peers, and repeat both comparisons.

Each trial contains 3,840 ML-DSA and 960 Ed25519 load spends in 60
blocks, plus four funding and setup blocks. The Windows process verifies
the mixed blocks through native consensus during network receipt and
reopens the persisted state in a later process. Both node instances
use dedicated test directories.

| Trial | First connected window | Partition window | Recover 12 blocks | Second connected window |
| --- | ---: | ---: | ---: | ---: |
| [1](results-1.json) | 9.766 s | 3.882 s | 725.784 ms | 5.744 s |
| [2](results-2.json) | 9.750 s | 3.867 s | 584.033 ms | 5.744 s |
| [3](results-3.json) | 9.773 s | 3.887 s | 614.446 ms | 5.743 s |

Each trial confirmed all 4,800 load spends and compared all 64 block
wires before and after reopening the receiver. The load blocks encoded
to 15,255,280 bytes per trial. Their offered rate was about 240 spends
per second at the configured cadence. The sampled lag on the connected
windows reached one block in each trial. The 12-block catch-up median
was 614.446 ms.

A separate regression after switching the fixture to the native shutdown
routine matched all 64 encoded blocks and the final audit again. The
receiver reported its height-64 snapshot on reopen.

Catch-up time uses the Windows monotonic performance counter from
starting the replacement forward through the first RPC observation of
the exact recovered tip. It includes SSH startup, node reconnection, P2P
transfer, validation and polling every 20 ms. Observed lag samples use
every fifth connected block notice. They give a sampled block-height
difference under the offered workload. This setup covers one physical
path with one producer, one receiver and one interruption. Capacity
limits, unencrypted transport and interoperability with older software
remain open.

## Reproduce

Use disposable source copies at ReQuant revision
`967ce1831a37b56832bda4d52ddd29c428a18f9b`. Apply
[the native lab chain patch](../../native-pqc-lab.patch) and
[the node relay patch](../relay.patch). Copy [the loopback test
source](../relay_test.rs) to `crates/node/tests/pqc_native_relay.inc`.
Copy [relay_lab.rs](relay_lab.rs) into
`crates/node/examples/relay_lab.rs` on both hosts.

```sh
cargo build --locked --offline --release -p requant-node --example relay_lab
```

Run the orchestrator on Windows. Provide a trusted SSH destination and
an existing writable Linux scratch root. A fresh receiver directory
is temporary on Windows. Linux retains a uniquely named producer
directory containing the synthetic chain.

```text
python run.py WINDOWS_EXE USER@HOST /ABSOLUTE/LINUX_BINARY /ABSOLUTE/LINUX_SCRATCH SSH_IDENTITY_FILE
```
