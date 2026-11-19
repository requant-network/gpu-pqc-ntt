# RESULTS — measured on NVIDIA GeForce RTX 5060 Ti (SM120, compute cap 12.0)

Environment (recorded in every results JSON): driver 610.43.03 (CUDA driver
API 13030), CUDA runtime 13.3 (13030), cuBLAS 13.6.0 (130600), nvcc
`-std=c++17 -O3 -arch=sm_120`, Python 3.12.3, Ubuntu 24.04 server `me4me`.
All runs serialized under the campaign flock on GPU 0; CMP 50HX never used.
Raw data: `results/*.json` (with UTC timestamps and SHA-256 source/binary hashes).

## Correctness status: ALL EXACT

- 32 verification cases (2 primes × 2 seeds × {B=1,16,256 random + 5 edge
  cohorts}) × 4 routes (SIMT NTT, SIMT conv, cuBLAS limb-GEMM NTT,
  mma.sync limb-GEMM NTT): **1,138,688 coefficients compared to the exact
  Python reference, 0 mismatches** (`results/verification.json`).
- 6 negative controls (wrong zeta table; wrong limb base 127/255 on both
  GEMM routes; both primes): **all rejected**, 4096/4096 coefficients wrong
  in each — the harness demonstrably fails on wrong math.
- 13/13 stdlib unit tests pass (local + server), including element-wise
  agreement of derived zetas with the official pq-crystals Kyber/Dilithium
  tables and exhaustive Barrett validation over all 3329² products.
- Documented negative result: the first GPU build had a sign error in the
  GS INTT butterfly ((U−V)·ζ instead of (V−U)·ζ); coefficient-exact
  verification caught it immediately (128/256 wrong on Kyber conv,
  `results/verification_fail_initial.json`). Fixed, re-verified, all exact.

## Resident NTT throughput (forward NTT only)

Median of 50 cudaEvent-timed launches after 10 warmup launches; inputs resident on device; no transfers in the timed region. One launch = full batch pipeline (SIMT: 1 kernel; GEMM: decompose + limb GEMMs + recombine).

### ML-KEM / Kyber (q=3329)

| B | SIMT NTT µs | SIMT poly/s | cuBLAS limb-GEMM µs | cuBLAS poly/s | mma.sync limb-GEMM µs | mma poly/s | SIMT speedup vs cuBLAS |
|---|---|---|---|---|---|---|---|
| 1 | 3.55 | 282k | 29.15 | 34k | 13.44 | 74k | 8.21x |
| 16 | 3.55 | 4.5M | 28.58 | 560k | 17.79 | 899k | 8.05x |
| 256 | 5.41 | 47.3M | 30.50 | 8.4M | 27.81 | 9.2M | 5.64x |
| 4096 | 27.04 | 151.5M | 67.90 | 60.3M | 296.16 | 13.8M | 2.51x |

### ML-DSA / Dilithium (q=8380417)

| B | SIMT NTT µs | SIMT poly/s | cuBLAS limb-GEMM µs | cuBLAS poly/s | mma.sync limb-GEMM µs | mma poly/s | SIMT speedup vs cuBLAS |
|---|---|---|---|---|---|---|---|
| 1 | 3.78 | 265k | 55.81 | 18k | 23.58 | 42k | 14.78x |
| 16 | 4.19 | 3.8M | 57.50 | 278k | 33.25 | 481k | 13.72x |
| 256 | 5.63 | 45.5M | 61.86 | 4.1M | 55.36 | 4.6M | 10.98x |
| 4096 | 38.50 | 106.4M | 159.52 | 25.7M | 672.96 | 6.1M | 4.14x |

Per-polynomial cost at B=4096 (resident): Kyber SIMT **6.60 ns/poly**,
Dilithium SIMT **9.40 ns/poly**; cuBLAS GEMM route 16.58 / 38.95 ns/poly.

## Resident full negacyclic convolution (SIMT: NTT + pointwise + INTT)

| Prime | B | µs/launch | conv/s | µs/conv |
|---|---|---|---|---|
| Kyber | 1 | 4.29 | 233k | 4.288 |
| Kyber | 16 | 4.32 | 3.7M | 0.270 |
| Kyber | 256 | 9.31 | 27.5M | 0.036 |
| Kyber | 4096 | 84.74 | 48.3M | 0.021 |
| Dilithium | 1 | 5.60 | 179k | 5.600 |
| Dilithium | 16 | 5.57 | 2.9M | 0.348 |
| Dilithium | 256 | 11.46 | 22.3M | 0.045 |
| Dilithium | 4096 | 106.94 | 38.3M | 0.026 |

## Transfer-inclusive (pinned H2D + pipeline + D2H, median of 5 reps)

| Prime | B | SIMT NTT µs | cuBLAS GEMM µs | mma GEMM µs |
|---|---|---|---|---|
| Kyber | 1 | 8.26 | 32.35 | 17.89 |
| Kyber | 16 | 18.62 | 34.18 | 27.87 |
| Kyber | 256 | 89.18 | 102.56 | 111.62 |
| Kyber | 4096 | 1252.77 | 1291.71 | 1520.54 |
| Dilithium | 1 | 8.61 | 63.55 | 27.30 |
| Dilithium | 16 | 10.75 | 60.42 | 39.68 |
| Dilithium | 256 | 89.95 | 108.64 | 135.01 |
| Dilithium | 4096 | 1257.79 | 1379.81 | 1896.00 |

At B=4096 the 4 MiB in + 4 MiB out transfer dominates (~1.25 ms, ≈6.7 GB/s
effective on this host's PCIe link); compute is ~2–12% of transfer-inclusive
time for the SIMT/cuBLAS routes (up to ~36% for the unoptimized mma.sync
Dilithium pipeline). At B=1 the fixed H2D/D2H + launch overhead (~8 µs)
dominates.

## INT8 GEMM dispatch probe (single 256xBx256 limb-pair GEMM, random int8 in [-64,63])

Exactness checked element-wise against an int64 host oracle. The naive SIMT int8 GEMM control is the non-tensor-core baseline.

| B | cuBLAS status | cuBLAS exact | mma exact | SIMT exact | cuBLAS µs | mma µs | SIMT control µs | cuBLAS vs SIMT |
|---|---|---|---|---|---|---|---|---|
| 1 | 0 (SUCCESS) | True | True | True | 10.30 | 3.52 | 27.68 | 2.7x |
| 2 | 0 (SUCCESS) | True | True | True | 8.19 | 3.62 | 27.78 | 3.4x |
| 4 | 0 (SUCCESS) | True | True | True | 9.50 | 4.32 | 27.65 | 2.9x |
| 8 | 0 (SUCCESS) | True | True | True | 10.21 | 5.73 | 27.81 | 2.7x |
| 16 | 0 (SUCCESS) | True | True | True | 9.41 | 5.50 | 27.55 | 2.9x |
| 256 | 0 (SUCCESS) | True | True | True | 10.88 | 8.38 | 197.06 | 18.1x |
| 4096 | 0 (SUCCESS) | True | True | True | 19.68 | 68.74 | 2760.19 | 140.3x |

cuBLAS effective int8 throughput at B=4096: 2·256·256·4096 / 19.68 µs ≈
**27.3 TOPS** — unreachable by the SIMT control at this shape; native
SM120 INT8 tensor-core dispatch confirmed (plus the explicit mma.sync
kernel executing the same math exactly).

## Timing ranges (min-max across the 50 resident launches)

| Prime | B | Method | median µs | min µs | max µs |
|---|---|---|---|---|---|
| Kyber | 1 | simt-ntt | 3.55 | 2.72 | 5.76 |
| Kyber | 1 | gemm-cublas | 29.15 | 26.98 | 40.51 |
| Kyber | 1 | gemm-mma | 13.44 | 12.26 | 22.72 |
| Kyber | 4096 | simt-ntt | 27.04 | 25.79 | 28.93 |
| Kyber | 4096 | gemm-cublas | 67.90 | 66.05 | 71.90 |
| Kyber | 4096 | gemm-mma | 296.16 | 294.46 | 297.89 |
| Dilithium | 1 | simt-ntt | 3.78 | 2.98 | 8.38 |
| Dilithium | 1 | gemm-cublas | 55.81 | 53.73 | 72.32 |
| Dilithium | 1 | gemm-mma | 23.58 | 21.86 | 35.20 |
| Dilithium | 4096 | simt-ntt | 38.50 | 37.41 | 40.93 |
| Dilithium | 4096 | gemm-cublas | 159.52 | 157.41 | 164.29 |
| Dilithium | 4096 | gemm-mma | 672.96 | 672.00 | 677.38 |

Large-B medians are stable (±5%); small-B single-launch timings carry
launch/scheduling jitter (ranges shown, medians reported).
