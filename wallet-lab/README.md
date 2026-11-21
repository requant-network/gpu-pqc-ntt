# ML-DSA-44 wallet migration on a native lab chain

November 21, 2026. The experiment applies [a source patch](native-pqc-lab.patch)
to ReQuant `967ce1831a37b56832bda4d52ddd29c428a18f9b` in a disposable checkout.
It uses the CPU implementation in RustCrypto `ml-dsa = 0.1.2`, the smallest
parameter set in [NIST FIPS 204](https://csrc.nist.gov/pubs/fips/204/final).
The lab chain inherits regtest mining parameters and has a distinct chain ID
and genesis. The patched node's named-network selector cannot start this chain.

## Measured result

The native chain confirmed an ordinary Ed25519 payment to the ML-DSA owner hash.
The wallet harness discarded the original key, imported its versioned backup and
recreated the address and transaction byte for byte. A single block confirmed
both the restored ML-DSA spend and a separate Ed25519 spend. The actual block
validator rejected the changed output and preserved the original unspent coin.
Eighteen negative controls also covered signature and encoding mutations, disabled
networks, an unauthorized signer and spent-input replay.

| One input and one output | Ed25519 | ML-DSA-44 lab format |
| --- | ---: | ---: |
| Serialized transaction size | 179 bytes | 3,924 bytes |
| Sign and serialize, median | 16.72 µs | 715.71 µs |
| Decode, verify and check the spend, median | 40.11 µs | 158.30 µs |
| Approximate single-thread signing rate | 59,813/s | 1,397/s |
| Approximate single-thread validation rate | 24,929/s | 6,317/s |

The Linux x64 LAN server used a Ryzen 5 5500 and Rust 1.98.1. Each table entry is
the median of five release-build trials averaging 128 distinct transactions each.
The timings include native transaction hashing and serialization. Validation
checks the in-memory UTXO, signature and shape, with no block production, relay,
storage or wallet startup in the timing loop. The byte ratio is 21.9× with this
wire format. Of the 3,745 additional bytes, 3,732 are the FIPS public key and
signature. The other 13 encode native v2 locks and the unlock tag. The layout
retains the 96 bytes of reserved Ed25519 fields, which a different wire format
could remove.

The release vectors and wallet runs executed in separate Ubuntu containers
with networking disabled. The pinned NIST ACVP sample's ML-DSA-44 group 1 passed
25 key generations, 15 deterministic signatures, 3 positive verification
cases and 12 negative verification cases. The native debug regression suite
passed 132 tests, with the separately executed ACVP reader intentionally ignored
by default. The first broad build surfaced one legacy test that reused a copied
`Unlock`. The patch makes that existing reuse explicit. [ACVP log](acvp.log.txt),
[native suite](native.log.txt), and [wallet timing log](wallet.log.txt).

## Experimental wire and recovery rules

- An existing output carries a 32-byte owner hash. ML-DSA owners use native
  tagged hashing with domain `requant/pqc-lab/ml-dsa-44/v1` over the encoded
  1,312-byte public key. The Bech32m address has HRP `rqpl`, version symbol 1
  and the owner hash as payload. Lab Ed25519 addresses use version symbol 0.
  Legacy parsing and ML-DSA parsing each reject the other version.
- The spend uses native transaction version 1, transfer kind 2, unlock tag 9.
  Its inherited Ed25519 public key and signature fields must be zero. The
  unlock carries 1,312 public-key bytes and 2,420 signature bytes.
  Anyone-can-pay is disabled for this condition. The signature covers the
  native sighash over the lab chain ID, transaction ID and input index, via
  pure deterministic ML-DSA with context `requant/pqc-lab/tx/v1`. The transaction
  ID commits to the public key and excludes signature bytes. The witness
  transaction ID also commits to the signatures. An old decoder rejects tag 9.
- The backup is the version byte 1 followed by the FIPS 204 32-byte seed. The
  harness obtains the seed from the OS, holds backup bytes in zeroizing memory
  and restores into a new signing key. Unknown versions and incorrect lengths
  are refused. These bytes are secret wallet material. The experiment creates
  no persisted backup, keystore or new wallet CLI.
- Migration spends Ed25519 funds normally to a checked lab ML-DSA address,
  confirms the resulting output, then spends with its ML-DSA key. The return
  payment to an Ed25519 address exercised coexistence. Upgraded wallet
  address validation and spend support are prerequisites for a real migration.
  Sending to an opaque owner hash on an unsupported chain can strand funds.

## Reproduce

Run these commands in a disposable copy pinned to the stated ReQuant SHA.

```sh
git apply /path/to/wallet-lab/native-pqc-lab.patch
python3 /path/to/wallet-lab/acvp.py > /tmp/requant-acvp.tsv
cargo test --locked --offline -p requant-consensus -p requant-wallet -p requant-node
PQC_ACVP_TSV=/tmp/requant-acvp.tsv cargo test --locked --offline --release \
  -p requant-consensus --test pqc_vectors -- --ignored --nocapture
PQC_BENCH=1 cargo test --locked --offline --release \
  -p requant-consensus --test pqc_wallet -- --nocapture
```

The generator checks all six downloaded files against pinned SHA-256 digests.
Pass a directory containing those files as the first argument to generate the
TSV offline. Cargo dependencies must be fetched before offline execution.
The reported no-network runs invoked the compiled test binaries directly.

The [Windows client trial](client/README.md) repeated the migration,
backup restore and pinned vectors and measured client-side signing on the
workstation CPU.

## Decision

Keep this fixture and its CPU path as the reference. A separate hybrid run
now measures full validation at 104.97 µs per distinct-key transaction at
batch 32, compared with 157.32 µs on the matched CPU path. Its timing includes
transfers and CPU stages. [CUDA validation trial](cuda-verify/README.md).

The [Windows DPAPI lab](client/windows-vault/README.md) exercised
user-protected storage and restart recovery. A [portable backup
trial](client/portable-backup/README.md) restored three Windows-exported
wallets on Linux. The [two-node relay trial](native-relay/README.md)
confirmed both signature types in one block on Windows and Linux.
The [cross-host relay trial](native-relay/cross-host/README.md) carried
60 mixed blocks per run between separate processes and verified catch-up
after a 12-block partition. Cross-account storage isolation, larger and
more varied network loads, and a deployed migration remain open.
