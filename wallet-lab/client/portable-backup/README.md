# Portable recovery of an ML-DSA wallet

November 21, 2026. A Windows x86-64 workstation exported three freshly
generated ML-DSA-44 test wallets. A Linux x86-64 server opened each
encrypted backup in a new process with its recovery code and an owner pin
taken separately from the sender. Both hosts independently confirmed
each payment on the isolated lab chain. Every 3,924-byte transaction
matched its sender copy byte for byte. [Trial results](results.json).

| Operation | Host CPU | Median of three |
| --- | --- | ---: |
| Derive key and seal | AMD Ryzen 7 8745HS, Windows | 105.910 ms |
| Derive key, open, restore and check owner | AMD Ryzen 5 5500, Linux | 120.097 ms |

This timing covers the specified operations in the release test binaries.
Process startup, network transfer, file access and transaction signing are
outside the timed region. Each host used Rust 1.98.1.

Each 129-byte backup contains an eight-byte format tag, 16-byte random
salt, 24-byte random nonce, 32-byte public owner hash, 33 encrypted seed
bytes (including its version) and a 16-byte authentication tag. The full
header is authenticated. The owner pin is checked against the header and
against the rederived signing key. The fixture uses a random 256-bit
recovery code, Argon2id v1.3 (64 MiB, three passes, four lanes) and
XChaCha20-Poly1305. KDF costs are fixed in the fixture. The Rust code uses
zeroizing buffers for its seed bytes, code, derived key and Argon2
workspace. This setup tests synthetic high-entropy codes. Product recovery
UX, human passphrases, key custody, mobile memory pressure and OS-level
memory retention require separate evidence.
[Argon2 parameters](https://www.rfc-editor.org/rfc/rfc9106.html#section-7.4) ·
[XChaCha construction](https://datatracker.ietf.org/doc/html/draft-irtf-cfrg-xchacha-03).

On each host, the harness refused 11 invalid cases covering shape and
version, the owner pin, wrong recovery code, modified salt, nonce,
ciphertext or tag, and authenticated payloads with the wrong owner or
seed version. The Linux receiver also refused a wrong code and a short
stdin pipe before it wrote any signed output. The runner copies the
encrypted backup via authenticated SSH. It sends the public owner pin
as separate input and carries the recovery code through process stdin
over SSH. The trial retains timings and wire digests. Its temporary
wallet and transaction files are removed at the end of the run.
The pinned NIST ACVP vector checks passed on both hosts, covering 25
key-generation, 15 deterministic signing and 15 verification cases.

The [Linux memory-limit trial](MEMORY_LIMITS.md) made workspace
allocation fallible, tested recovery at fixed process and container limits,
and confirmed compatibility with the earlier Windows exporter.

## Reproduce

Prepare disposable Windows and Linux source copies at native ReQuant
revision `967ce1831a37b56832bda4d52ddd29c428a18f9b`. Apply
[the native patch](../../native-pqc-lab.patch) and then
[the dev-dependency patch](dependencies.patch) in each copy. Copy
[portable_backup.rs](portable_backup.rs) into
`crates/consensus/tests/pqc_portable_backup.inc` and append the following
line to `crates/consensus/tests/pqc_wallet.rs`.

```rust
mod portable_backup { include!("pqc_portable_backup.inc"); }
```

Build and run the local checks on both hosts. Cargo prints the path to
each release test executable.

```sh
cargo test --locked --offline --release -p requant-consensus --test pqc_wallet -- --nocapture
```

On the sender run the cross-host script with the two executables, an SSH
destination with a trusted host key, a writable isolated Linux scratch
directory and the SSH identity file path.

```text
python run.py LOCAL_TEST_EXE USER@HOST /ABSOLUTE/REMOTE_TEST_EXE /ABSOLUTE/REMOTE_SCRATCH SSH_IDENTITY_FILE
```
