# ML-DSA-44 transaction validation with CUDA

November 21, 2026. The native wallet lab validated the same encoded
transactions with the RustCrypto CPU path and a hybrid CPU/CUDA path. The
accelerator computes the public polynomial reconstruction in Algorithm 8 of
FIPS 204, using the existing exact SIMT butterflies for forward and inverse
NTTs and pointwise multiply-accumulate on RTX 5060 Ti. CPU code decodes the
public key and signature, expands the public key, prepares the message
representative, uses the signature hint and checks the commitment. The lab
then checks native ownership, amounts, maturity and the unspent output.
[FIPS 204](https://csrc.nist.gov/pubs/fips/204/final).

## Full validation measurements

The table uses the median of 18 matched passes across three fresh processes.
Each pass used the same 256 transactions per cohort on both paths. CPU and
hybrid run order alternated. The distinct cohort spent individually funded
outputs under 256 different ML-DSA public keys. The shared-key cohort checked
independent alternatives spending the same output. Each measurement starts
at the encoded transaction and ends after the UTXO check. No signing,
persistent storage, batch-formation wait or network relay is part of this
interval. The hybrid clock includes coefficient marshalling, transfer to
the card, two kernel launches, transfer back and CUDA synchronization.

| CUDA batch | CPU, distinct keys | Hybrid, distinct keys | Throughput gain | Hybrid, shared key |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 157.32 µs/tx | 130.68 µs/tx | 1.20× | 130.40 µs/tx |
| 8 | 157.42 µs/tx | 108.19 µs/tx | 1.46× | 108.25 µs/tx |
| 32 | 157.32 µs/tx | 104.97 µs/tx | 1.50× | 104.74 µs/tx |
| 128 | 157.58 µs/tx | 109.83 µs/tx | 1.43× | 113.02 µs/tx |

At batch 32 the distinct-key path increased warm throughput from about
6,356 to 9,526 transactions per second. Batch 128 was slower on this host.
The first hybrid call in a fresh process took 125.40–164.63 ms. Dividing
that setup time by the 52.35 µs warm saving at batch 32 gives an amortization
threshold of roughly 2,400–3,150 transactions per process under this
workload. That calculation excludes queue wait and models a process that
keeps the device initialized.

The host was a Ryzen 5 5500 on Linux x64, using Rust 1.98.1, CUDA
13.3.73 and driver 610.43.03. The RTX alone was visible to the process.
Each transaction sends 25 × 256 32-bit coefficients and receives 4 × 256
coefficients. The device buffers and zetas persist across calls. The
experiment expands each key again per transaction and uses ordinary host
buffers and blocking transfer. [Environment and source hashes](metadata.log.txt)
· [Measured results](results.json).

## Correctness

All three wallet runs exercised the prior migration and backup restoration
fixture. The CUDA path accepted valid transfers and rejected an output
mutation embedded in a batch and a transfer on an unsupported chain.
The CPU and hybrid path agreed with the NIST ACVP outcomes. Twelve of the
fifteen verification vectors reached CUDA. The other three were refused
by the shared signature decoder. Key-generation and signing vectors ran
on CPU. Twenty-eight additional CPU consensus regressions passed after
the verifier refactor. Hiding every CUDA device produced an explicit
backend error.

[ACVP](acvp.log.txt) · [Wallet run 1](wallet-1.log.txt) ·
[Wallet run 2](wallet-2.log.txt) · [Wallet run 3](wallet-3.log.txt) ·
[No CUDA device](unavailable.log.txt) · [Native CPU regressions](native-regressions.log.txt).

The CPU remains the reference for general validation. Batch 32 is the
measured throughput baseline for this CUDA backend. Production integration
still needs queue latency, mixed transaction classes, invalid-batch recovery,
GPU resource contention and a policy for device failure. This batch verifier
is a separate wallet-lab path, outside native block acceptance or deployment.

## Reproduce

Use a disposable ReQuant checkout at
`967ce1831a37b56832bda4d52ddd29c428a18f9b` and apply
[the native wallet lab patch](../native-pqc-lab.patch). Fetch the locked
dependencies once. Set `STUDY` to this NTT repository, `NATIVE` to the lab
checkout, `ML_DSA_SRC` to the local Cargo registry directory for
`ml-dsa-0.1.2`, and `LAB_BUILD` to a writable build directory. Select
the intended GPU by UUID in `CUDA_VISIBLE_DEVICES`.

```sh
nvcc -std=c++17 -O3 -arch=sm_120 -shared -Xcompiler=-fPIC \
  "$STUDY/wallet-lab/cuda-verify/verify.cu" \
  -o "$LAB_BUILD/libpqc_verify_lab.so" -lcublas
python3 "$STUDY/wallet-lab/cuda-verify/install.py" "$NATIVE" "$ML_DSA_SRC"
python3 "$STUDY/wallet-lab/acvp.py" > "$LAB_BUILD/acvp.tsv"
cd "$NATIVE"
export RUSTFLAGS="-L native=$LAB_BUILD"
export LD_LIBRARY_PATH="$LAB_BUILD:$CUDA_LIB_DIR"
cargo update --offline -p ml-dsa
PQC_ACVP_TSV="$LAB_BUILD/acvp.tsv" cargo test --offline --locked --release \
  -p requant-consensus --test pqc_vectors -- --ignored --nocapture
cargo test --offline --locked --release -p requant-consensus \
  --test pqc_wallet -- --nocapture
```

Set `CUDA_LIB_DIR` to the CUDA toolkit runtime library directory. The
overlay keeps the FFI in the native experiment and RustCrypto retains its
unsafe-code restriction. Run `python3 summarize.py` here to regenerate
the retained timing summary from the three captured wallet logs.
