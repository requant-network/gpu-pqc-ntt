# Wallet signing on Windows

November 21, 2026. The existing [native wallet fixture](../README.md) ran
on the Windows workstation CPU, an AMD Ryzen 7 8745HS with Rust 1.98.1.
The harness migrated one funded output from Ed25519 to ML-DSA-44, restored
the ML-DSA wallet from its versioned seed and recreated the signed
transaction byte for byte. Both spending formats confirmed together on
the isolated chain. Each fresh process also exercised all 18 negative
controls. All pinned NIST ACVP cases passed on Windows, including the
25 key-generation, 15 deterministic signing and 15 verification cases.
All 54 focused native CPU regressions also passed.
[Environment and pins](environment.log.txt) · [ACVP results](acvp.log.txt) ·
[CPU regressions](cpu.log.txt).

| One input and one output | Ed25519 | ML-DSA-44 |
| --- | ---: | ---: |
| Encoded wire | 179 bytes | 3,924 bytes |
| Sign and encode, median | 16.449 µs | 741.139 µs |
| Sign and encode, range of trial means | 16.098–20.707 µs | 628.610–832.511 µs |
| Decode, verify and check spend, median | 36.081 µs | 157.424 µs |

These are medians of 25 trial means for each format, with five trials in
each of five fresh release-build processes. Each trial averages 128
transactions. Signing includes native transaction construction and
serialization with the key in process memory. The check uses the funded
UTXO in the test process. The measured ML-DSA operation takes about
45 times longer than Ed25519 on this machine and stays below 1 ms per
wire in all 25 trial means. Key acquisition and storage, app startup,
Android signing and network relay are outside this measurement.
[Timing summary](results.json) · [First process log](wallet-1.log.txt).

The [DPAPI follow-up](windows-vault/README.md) reopens a Windows
user-protected seed file in new signer processes, measures recovery
latency and exercises refusal cases on the lab chain.

## Reproduce

Follow the [native fixture instructions](../README.md#reproduce) with the
base patch and locked dependencies in a disposable ReQuant checkout.
On Windows PowerShell, from that checkout, run the release harness and
the pinned vector set.

```powershell
$env:PQC_BENCH = '1'
cargo test --locked --offline --release -p requant-consensus --test pqc_wallet -- --nocapture
$env:PQC_ACVP_TSV = 'C:/path/to/group-1.tsv'
cargo test --locked --offline --release -p requant-consensus --test pqc_vectors -- --ignored --nocapture
```

Repeat the wallet command in five fresh processes. Capture the test output
in `wallet-1.log.txt` through `wallet-5.log.txt` and run `python summarize.py`
in this directory to rebuild the table data.
