# gpu-pqc-ntt — Post-quantum NTT (ML-KEM q=3329 / ML-DSA q=8380417) on SM120: SIMT vs tensor-core limb GEMM

**Status: COMPLETE — all correctness checks exact, all timing measured on the RTX 5060 Ti (SM120).**

A bounded research spike: how fast is *exact* NTT / polynomial arithmetic for the
NIST PQC standards ML-KEM (Kyber, q=3329, n=256) and ML-DSA (Dilithium,
q=8380417, n=256) on Blackwell consumer silicon — shared-memory SIMT
butterfly kernels vs an NTT-as-GEMM construction running on native INT8 tensor
cores (cuBLAS IGEMM and an explicit `mma.sync` IMMA kernel)?

## What has actually run (on the server, not simulated)

| Experiment | Scope | Result |
|---|---|---|
| Pure-Python exact reference (`reference.py`, stdlib only) | schoolbook negacyclic ground truth; Kyber incomplete 7-layer NTT + 128 degree-2 base products; Dilithium full 8-layer NTT; NTT matrices + inverses; limb-GEMM oracle | selftest OK, 2 seeds, both primes, edge cases |
| Unit tests (`tests/test_reference.py`, 13 tests) | derived zetas vs **official pq-crystals tables** (element-wise, both primes); exhaustive Barrett reduction for q=3329 (all 11,082,241 products); 250k sampled Barrett checks for q=8380417; limb roundtrips; int32 accumulator bounds | 13/13 OK (local + server) |
| GPU correctness (`run.py verify`) | every coefficient vs Python reference: SIMT NTT, SIMT full convolution, cuBLAS limb-GEMM NTT, mma.sync limb-GEMM NTT; B ∈ {1,16,256}, 2 seeds, 5 edge cohorts, both primes | **1,138,688 coefficients checked, 0 mismatches** (32 cases × 4 routes) |
| Negative controls | wrong zeta table (SIMT), wrong limb base 127/255 (both GEMM routes), both primes | all 6 rejected (4096/4096 coefficients wrong each) |
| INT8 GEMM dispatch probe (`run.py probe`) | cuBLAS `CUBLAS_COMPUTE_32I` vs explicit `mma.sync.m16n8k32.s8` vs naive SIMT int8 GEMM control; B ∈ {1,2,4,8,16,256,4096}; element-wise vs int64 host oracle | all exact; cuBLAS **140× faster** than SIMT control at B=4096 (27.3 TOPS effective) → native tensor-core dispatch |
| Resident timing (`run.py bench`) | warmup 10 + 50 launches, cudaEvent medians; B ∈ {1,16,256,4096}; both primes; 4 methods | see RESULTS.md |
| Transfer-inclusive timing (`run.py benchx`) | pinned H2D + pipeline + D2H, 3 warmup + 5 reps | see RESULTS.md |

## Main result

**SIMT butterfly NTT wins everywhere on SM120 for n=256 PQC primes.** The
tensor-core NTT-as-GEMM construction is *bit-exact* but slower at every batch
size measured:

- ML-KEM NTT, B=4096: **SIMT 27.04 µs = 151.5M polynomials/s (6.6 ns/poly)**
  vs cuBLAS limb-GEMM 67.90 µs (60.3M/s) — SIMT **2.51×** faster.
- ML-DSA NTT, B=4096: **SIMT 38.50 µs = 106.4M polynomials/s (9.4 ns/poly)**
  vs cuBLAS limb-GEMM 159.52 µs (25.7M/s) — SIMT **4.14×** faster.
- Full negacyclic convolution (SIMT NTT+pointwise+INTT), B=4096:
  Kyber **48.3M conv/s**, Dilithium **38.3M conv/s**.
- At small B the gap widens (5.6–14.8×): the GEMM route pays limb
  decomposition + 4 (Kyber) / 9 (Dilithium) GEMM launches + recombination,
  while one SIMT kernel does the whole batch in ~3.5–4.2 µs.
- The explicit `mma.sync` IMMA kernel beats cuBLAS at B ≤ 16 (no library
  launch overhead) but loses at B ≥ 256 (simple tiling, no shared-memory
  staging) — it exists to *prove* native INT8 tensor-core execution, not to
  be the fastest GEMM.

Why: the butterfly is O(n log n) work per polynomial with all data in shared
memory; NTT-as-GEMM is O(n²) work per polynomial, and at n=256 the limb
decomposition/recombination plus multi-launch overhead outweighs the tensor
cores' raw int8 throughput. Tensor cores would need much larger n (or fused
multi-prime/RNS batches) to amortize the dense-matrix formulation.

## Scope limitations (explicit)

- n=256 only; forward NTT for the GEMM comparison (GEMM-based INTT and
  GEMM-based full convolution not implemented — convolution is SIMT-only).
- Resident timings exclude H2D/D2H and one-time setup (NTT matrix limbs are
  uploaded at init). Transfer-inclusive numbers are reported separately.
- cuBLAS batch is padded to a multiple of 16 columns; the probe shows native
  support down to B=1, and padding is latency-bound (no measurable cost) at
  small B. The mma.sync path needs no padding.
- The mma.sync kernel is a straightforward warp-per-16×8-tile implementation
  (no smem staging/double buffering); cuBLAS is the optimized-library route.
- Single GPU (RTX 5060 Ti, GPU 0), campaign flock serialization, no CUDA
  graphs, no multi-stream overlap. Normal-form arithmetic (Barrett), not
  Montgomery representation — mathematically identical linear maps.
- No claims about lattice-scheme end-to-end performance (keygen/encaps/sign
  involve much more than one NTT).

## Layout

- `reference.py` — exact stdlib-only Python reference + ground truth
- `cuda/pqc_ntt.cu` — SIMT kernels, cuBLAS INT8 GEMM pipeline, mma.sync IMMA
  kernel, limb decompose/recombine, probe/bench modes
- `run.py` — orchestrator (reference / probe / verify / bench / benchx / all)
- `tests/test_reference.py` — 13 stdlib unittest cases incl. official-table
  cross-checks and exhaustive Barrett validation
- `results/*.json` — raw measurements with UTC timestamps, GPU/driver/CUDA
  environment, SHA-256 of executed sources + binary
- `METHODS.md` — mathematical contract, reduction bounds, timing boundaries
- `RESULTS.md` — measured tables
- `FINAL_REPORT.md` — conclusions, negative results, next decisions

## Reproduce

```bash
# server (Ubuntu 24.04, CUDA 13.3, RTX 5060 Ti on GPU 0)
/usr/local/cuda/bin/nvcc -std=c++17 -O3 -arch=sm_120 cuda/pqc_ntt.cu \
    -o build/pqc_ntt -lcublas
python3 tests/test_reference.py          # CPU, stdlib only
CUDA_VISIBLE_DEVICES=0 flock /tmp/requant-new-studies-gpu.lock -c \
    'python3 run.py all'
```

## Related work

- `requant-network/gpu-fhe-research` — RNS/NTT for FHE primes (65537-style)
  and CKKS bootstrapping. This study is distinct: NIST PQC primes (3329,
  8380417) and the ML-KEM/ML-DSA NTT structures, SIMT vs INT8 tensor-core
  NTT-as-GEMM on SM120. Not re-run here.
- pq-crystals/kyber and pq-crystals/dilithium reference implementations —
  official zeta tables used for the faithfulness cross-check (see METHODS.md).
