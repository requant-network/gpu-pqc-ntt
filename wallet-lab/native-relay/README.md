# Native ML-DSA relay between two local nodes

November 21, 2026. Two node instances listened on ephemeral loopback TCP
ports with independent data directories. The integration test started the
nodes through the existing library configuration on the isolated PQC lab
chain. Discovery, automatic updates and automatic mining were disabled
for these two instances.

1. Submit a signed Ed25519 migration through the receiver's JSON-RPC.
   Wait until the mining peer receives the same bytes and then confirm
   the migration through the native work and block path.
2. Change the destination amount of a signed ML-DSA payment. The receiver
   rejects its signature and both mempools stay empty.
3. Submit the authentic 3,924-byte ML-DSA-44 spend and a 179-byte Ed25519
   spend. Both reach the mining peer byte for byte. The mining peer
   includes both at height 5 and the receiver reaches the same block tip.
4. Resubmit the confirmed ML-DSA spend. The node refuses it. Reopen the
   receiver's data directory with no configured peer and check the same
   tip and both payment outputs.

The run passed on the Ryzen 7 8745HS Windows host and Ryzen 5 5500 Linux
server. All nine cases in the two-node suite passed on both machines.
The two node instances in each trial ran inside a single test process.
These checks cover native message serialization, mempool and block
hand-off via local TCP, and reopening the receiving store. Separate
processes and hosts, competing blocks, sustained traffic and mixed
software releases require further tests. The lab chain remains outside
the production CLI network selector.
[Retained results](results.log.txt).

## Linux RPC fixture

The wider Linux suite reproduced an existing connection reset in
`rpc_formats_methods_long_poll_and_cookie`. Eight trials of the baseline
test had seven resets. Eight trials with the portable overlay had eight
resets. Both test executables had the same digest. The server can reject
an HTTP request from the headers before reading its body. Buffering the
two small test requests before writing them to the socket removed the
observed timing failure in eight further trials and the full Linux suite.
The [fixture patch](rpc-test-fixture.patch) changes only the test client,
with the original authorization and origin checks intact.

## Reproduce

Start with native ReQuant revision
`967ce1831a37b56832bda4d52ddd29c428a18f9b` and apply
[the lab chain patch](../native-pqc-lab.patch) then
[the relay patch](relay.patch). Copy [relay_test.rs](relay_test.rs) to
`crates/node/tests/pqc_native_relay.inc` in the disposable source tree.

```sh
cargo test --locked --offline --release -p requant-node --test two_nodes -- --nocapture
```
