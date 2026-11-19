# FINAL REPORT — Post-quantum NTT on SM120: SIMT vs tensor-core limb GEMM

Study: `gpu-pqc-ntt` (bounded spike, completed 2026-11-19)
Hardware: NVIDIA GeForce RTX 5060 Ti, SM120 (compute cap 12.0), driver
610.43.03, CUDA 13.3, cuBLAS 13.6.0. All GPU work on GPU 0 only, compiled
`-arch=sm_120`, serialized under the campaign flock. CMP 50HX (Turing) never
used. Every results JSON carries GPU name + compute cap + driver + source
and binary SHA-256 hashes.

## Question

How fast is exact NTT/polynomial arithmetic for ML-KEM (Kyber, q=3329,
n=256) and ML-DSA (Dilithium, q=8380417, n=256) on SM120 — SIMT kernels vs a
tensor-core NTT-as-GEMM construction — at batched sizes?

## Answer (measured, not projected)

**SIMT shared-memory butterfly NTT is the right tool for n=256 PQC primes on
SM120. It beats the exact INT8 tensor-core NTT-as-GEMM construction at every
batch size tested (B = 1…4096), by 2.5–4.1× at B=4096 and 5.6–14.8× at small
B.** The tensor-core route is fully exact — it is not a precision tradeoff,
it is a work/overhead tradeoff.

Headline numbers (resident, median of 50 launches, B=4096):

| Metric | ML-KEM (q=3329) | ML-DSA (q=8380417) |
|---|---|---|
| SIMT forward NTT | 27.04 µs → **151.5M poly/s** (6.60 ns/poly) | 38.50 µs → **106.4M poly/s** (9.40 ns/poly) |
| cuBLAS INT8 limb-GEMM NTT | 67.90 µs → 60.3M poly/s | 159.52 µs → 25.7M poly/s |
| mma.sync INT8 limb-GEMM NTT | 296.16 µs → 13.8M poly/s | 672.96 µs → 6.1M poly/s |
| SIMT full negacyclic conv | 84.74 µs → **48.3M conv/s** | 106.94 µs → **38.3M conv/s** |
| Transfer-inclusive NTT (SIMT) | 1252.77 µs | 1257.79 µs |

Small-B behavior: one SIMT NTT launch costs ~3.5–4.2 µs regardless of B≤16
(latency floor), so batching is essentially free up to hundreds of
polynomials; the GEMM route has a ~29 µs (Kyber) / ~56 µs (Dilithium) floor
from decompose + 4/9 GEMM launches + recombine.

## Why SIMT wins here

1. **Work complexity**: butterfly = O(n log n): 896 (Kyber) / 1024
   (Dilithium) butterflies per polynomial, each one modular multiply + two
   adds, all in shared memory. NTT-as-GEMM = O(n²) = 65,536 int8 MACs per
   polynomial per limb pair, ×4 (Kyber) / ×9 (Dilithium) limb pairs =
   262,144 / 589,824 MACs — ~293× / 576× more multiply-accumulates than the
   butterfly's modular multiplies. Tensor cores are fast, but not enough to
   overcome that work ratio at these shapes.
2. **Pipeline overhead**: the GEMM route needs limb decomposition, 4/9
   separate GEMM launches (cuBLAS) and int64 recombination + mod-q reduction
   kernels. At B=4096 the raw cuBLAS GEMMs alone (4 × ~19.7 µs isolated)
   already exceed the entire SIMT NTT (27 µs).
3. **Shape**: K=256 is small for tensor-core GEMM; the probe shows cuBLAS
   reaching ~27.3 TOPS int8 at B=4096 (140× the SIMT int8 GEMM control —
   genuine tensor-core dispatch), but that throughput applies to 16× more
   arithmetic than the butterfly needs.
4. **Where tensor cores DO win the sub-battle**: the explicit mma.sync
   kernel beats cuBLAS at B≤16 (13.4 vs 29.2 µs Kyber) by avoiding library
   launch overhead — relevant if a fused single-kernel GEMM pipeline were
   built. It still loses to SIMT NTT everywhere.

## Correctness status: ALL EXACT

- 1,138,688 coefficient comparisons vs the exact stdlib Python reference
  (schoolbook negacyclic ground truth): **0 mismatches**, 32 cases × 4 GPU
  routes, both primes, 2 seeds, B ∈ {1,16,256} + edge cohorts (zero, max
  coefficients, delta, alternating).
- 6 negative controls (wrong zeta table; wrong limb bases on both GEMM
  routes): all rejected with 100% coefficient mismatches.
- Derived zetas match the official pq-crystals Kyber/Dilithium tables
  element-wise (Montgomery→normal conversion); Kyber basemul γ pairing
  matches official zetas[64+i]/−zetas[64+i].
- Barrett reduction exhaustively validated for q=3329 (all 11,082,241
  products) and sampled (250k) for q=8380417; int32 GEMM accumulator bound
  K·max_digit² = 2^20 / 2^22 < 2^31 proven and tested.
- 13/13 stdlib unit tests pass locally and on the server.

## Negative results and lessons (kept, not hidden)

1. **Initial INTT sign bug**: first CUDA build used (U−V)·ζ in the GS
   butterfly; official Kyber/Dilithium both reduce to (V−U)·ζ in normal
   form. Coefficient-exact verification caught it on the first case
   (128/256 Kyber conv coefficients wrong; evidence retained in
   `results/verification_fail_initial.json`). Lesson: NTT self-consistency
   (NTT→INTT roundtrip) would NOT have caught a compensating convention
   error — schoolbook ground truth per coefficient is mandatory.
2. **Dilithium root order**: 1753 is a primitive 512-th root (1753^256 ≡ −1),
   not 256-th; the full NTT evaluates at odd powers 1753^{2·bitrev8(i)+1}.
   Assuming a 256-th root fails the reference selftest immediately.
3. **cuBLAS column-major gotcha**: row-major NTT matrix uploads are read
   transposed under CUBLAS_OP_N; fixed by a one-time host transpose. The
   mma.sync path reads A row-major natively.
4. **mma.sync naive tiling loses to cuBLAS at B≥256** (296 vs 68 µs Kyber
   B=4096): warp-per-16×8-tile without shared-memory staging re-reads A per
   tile column. An optimized IMMA NTT kernel could close part of the gap to
   cuBLAS, but not the 2.5× gap to SIMT — the work-complexity argument is
   independent of GEMM optimization quality.

## Boundaries (what this study does not claim)

- Forward NTT only for the GEMM comparison; convolution measured SIMT-only.
- n=256, single prime per launch; no RNS multi-prime fusion, no CUDA graphs,
  no stream overlap, no persistent kernels.
- Resident timings exclude transfers and one-time setup; transfer-inclusive
  reported separately (PCIe ~6.7 GB/s effective dominates at B=4096).
- Microbenchmarks of NTT/convolution, not end-to-end ML-KEM/ML-DSA ops.
- mma.sync kernel is a native-execution proof with simple tiling, not an
  optimized tensor-core kernel.

## Next decisions this evidence supports

1. **Use SIMT butterfly NTT for PQC workloads on SM120** (and likely on any
   GPU at n=256): ~150M Kyber NTT/s and ~106M Dilithium NTT/s per device,
   ~48M/38M full conv/s at B=4096, with a ~3.5 µs latency floor.
2. **Do not pursue NTT-as-GEMM for n=256 PQC primes** as a throughput play;
   the dense-matrix formulation only becomes interesting at much larger n
   (FHE-style degrees ≥ 4096, cf. requant-network/gpu-fhe-research) or when
   many primes/large batches fuse into one big GEMM.
3. If a tensor-core PQC path is ever revisited, fuse decompose+GEMM+recombine
   into one persistent kernel and benchmark against the SIMT floor measured
   here; the exact limb construction (2 limbs base-128 / 3 limbs base-256
   centered, int32-exact) is validated and reusable.
4. Batch aggressively: SIMT NTT cost is flat to B≈256; transfer-inclusive
   cost is PCIe-bound above B≈256 — keep polynomials resident.

## Artifacts

- `reference.py`, `cuda/pqc_ntt.cu`, `run.py`, `tests/test_reference.py`
- `results/`: reference_check.json, probe.json, verification.json,
  verification_fail_initial.json (documented negative result),
  bench_resident.json, bench_transfer.json — all with UTC, GPU/driver env,
  SHA-256 of sources + binary
- README.md / METHODS.md / RESULTS.md (this report: FINAL_REPORT.md)
