# METHODS — mathematical contract, kernels, reduction bounds, timing boundaries

## 1. Mathematical contract

Both schemes multiply in R_q = Z_q[X]/(X^256 + 1) (negacyclic convolution).

### ML-KEM / Kyber, q = 3329

- 17 is a primitive **256-th** root of unity mod 3329 (17^128 ≡ −1, 17^256 ≡ 1).
- X^256 + 1 splits into 128 quadratics X² − ζ^{2i+1} (odd powers of 17), but
  those quadratics do **not** split further over Z_3329 (3329 − 1 = 2^8 · 13
  has no factor 512). Kyber therefore uses an **incomplete NTT**: 7
  Cooley–Tukey layers (len = 128, 64, …, 2), zetas[k] = 17^{bitrev7(k)},
  k = 1…127 in bit-reversed order.
- Output: 128 degree-2 blocks; block i is a(X) mod (X² − γ_i) with
  γ_i = 17^{2·bitrev7(i)+1}. Even/odd coefficient streams evaluate at γ_i:
  â[2i] = Σ_k a[2k]·γ_i^k, â[2i+1] = Σ_k a[2k+1]·γ_i^k.
- Pointwise = 128 independent degree-2 products mod (X² − γ_i):
  (a0 + a1X)(b0 + b1X) = (a0b0 + a1b1γ_i) + (a0b1 + a1b0)X.
  Official Kyber pairs block 2i with +zetas[64+i] and block 2i+1 with
  −zetas[64+i]; our γ table reproduces this exactly (verified element-wise
  against the official Montgomery table converted to normal form).
- INTT: 7 Gentleman–Sande layers (len = 2…128), zeta index k = 127 − block
  (descending), butterfly (U,V) → (U+V, (V−U)·ζ), final scale 128^{−1} mod q.

### ML-DSA / Dilithium, q = 8380417

- q − 1 = 2^13 · 1023, so a primitive **512-th** root exists: 1753
  (1753^256 ≡ −1, 1753^512 ≡ 1 mod q).
- X^256 + 1 splits completely into linear factors at the odd powers of 1753.
  Full 8-layer CT NTT, zetas[k] = 1753^{bitrev8(k)}, k = 1…255.
- Output coefficient i is the evaluation a(1753^{2·bitrev8(i)+1}).
- Pointwise = 256 coefficient-wise products.
- INTT: 8 GS layers (len = 1…128), descending k, same (U+V, (V−U)·ζ)
  butterfly (official Dilithium writes it as (t−u)·(−zetas[k]) — algebraically
  identical), final scale 256^{−1} mod q.

### Ground truth and faithfulness checks

- Ground truth: schoolbook negacyclic convolution, exact Python integers:
  c[k] = Σ_{i+j=k} a_i b_j − Σ_{i+j=k+256} a_i b_j (mod q). No floating point
  anywhere in the reference.
- `reference.py selftest`: NTT→pointwise→INTT == schoolbook for random polys
  (2 seeds: 20261119, 987654321), both primes, plus edge cases (zero, all-max
  coefficients, delta at 0, max at top degree, alternating ±max).
- NTT matrix formulation M·a (and M^{−1}) cross-checked against the butterfly
  NTT; limb-GEMM oracle cross-checked against the matrix product.
- Derived zeta tables cross-checked **element-wise against the official
  pq-crystals tables** (fetched 2026-11-19: pq-crystals/kyber `main/ref/ntt.c`,
  pq-crystals/dilithium `master/ref/ntt.c`), converting the official
  Montgomery-form entries to normal form via MONT^{−1} mod q
  (MONT = 2^32 mod q). Kyber basemul γ pairing also checked against official
  zetas[64+i] / −zetas[64+i]. All 13 unit tests pass.

## 2. GPU implementations (cuda/pqc_ntt.cu, -arch=sm_120)

All arithmetic is **normal form** (not Montgomery representation) with
**Barrett reduction**: for product p < q², r = p − q·⌊p·μ/2^k⌋ with
μ = ⌊2^k/q⌋, followed by at most one conditional subtraction.
Kyber uses k=32 (p < 3329² ≈ 2^23.4, error ≤ 1); Dilithium uses k=64
(p < 8380417² ≈ 2^46, 128-bit intermediate via __int128-free 64×64→high
multiply; error ≤ 1). Exhaustive validation: all q² = 11,082,241 Kyber
products; 250,000 sampled Dilithium products (unit tests).

### Route A — SIMT shared-memory radix-2 NTT (control + fastest route)

One block of 256 threads per polynomial; polynomial lives in shared memory;
7 (Kyber) / 8 (Dilithium) CT layers with `__syncthreads()` between layers;
zeta table in constant/global memory; Barrett per butterfly. Batched: B
blocks per launch. Full convolution kernel chains NTT(a), NTT(b), pointwise
(128 degree-2 products for Kyber / 256 scalar products for Dilithium), INTT
in the same kernel. Negative control: non-bit-reversed zeta table
(`simt_badzeta`) — must fail, and does (all 4096 coefficients wrong).

### Route B — NTT-as-GEMM on native INT8 tensor cores

Fixed NTT matrix M (256×256 mod q, centered representatives) times batch
matrix of polynomials (256×B). M and the inputs are decomposed into signed
int8 limbs:

- **Kyber: 2 limbs, base 128**, digits in [−64, 63] (q = 3329 < 128²/2).
- **Dilithium: 3 limbs, base 256 centered**, digits in [−128, 127]
  (q = 8380417 < 256³/2).

Each limb-pair product M_limb · A_limb is a 256×B×256 int8 GEMM with int32
accumulation. **Exactness bound**: K·max_digit² with K = n = 256:
Kyber 256·64² = 2^20 < 2^31; Dilithium 256·128² = 2^22 < 2^31 — every
accumulator is exact in int32, no overflow, no rounding. Recombination:
C = Σ_{i,j} base^{i+j}·C_ij computed in **int64** on device, then reduced
mod q (Barrett). Limb counts: 2×2 = 4 GEMMs (Kyber), 3×3 = 9 (Dilithium).

Two native tensor-core implementations:

1. **cuBLAS**: `cublasGemmEx(CUBLAS_OP_N, CUBLAS_OP_N, 256, B, 256, α=1,
   A: CUDA_R_8I lda=256, B: CUDA_R_8I ldb=256, β=0, C: CUDA_R_32I ldc=256,
   CUBLAS_COMPUTE_32I, CUBLAS_GEMM_DEFAULT)`. M is host-transposed once at
   init because cuBLAS is column-major. Batch padded to a multiple of 16
   columns (probe confirms native support down to B=1; padding is
   latency-bound at small B). Negative control: limb base 127 (Kyber) / 255
   (Dilithium) — digits overflow int8 range semantics and reconstruction is
   wrong; must fail, and does.
2. **Explicit IMMA**: `mma.sync.aligned.m16n8k32.row.col.s32.s8.s8.s32`
   PTX, warp-per-16×8 output tile, K-loop over 8 k-steps of 32, A row-major
   fragments loaded per lane per the PTX ISA layout, int32 accumulators,
   same int64 recombination. No library dispatch ambiguity: this kernel
   *is* the SM120 INT8 tensor-core instruction. Simple tiling (no smem
   staging) — a correctness/native-execution proof, not an optimized GEMM.

### Dispatch probe (evidence of native tensor-core execution)

Single 256×B×256 int8 GEMM, random digits in [−64,63], B ∈ {1,2,4,8,16,256,4096}:
cuBLAS status = 0 (SUCCESS) and element-wise exact vs int64 host oracle at
every B; mma.sync exact at every B; naive SIMT int8 GEMM control exact but
**140.3× slower at B=4096** (2760.19 µs vs 19.68 µs; cuBLAS effective
2·256·256·4096/19.68µs ≈ 27.3 TOPS int8). A SIMT path cannot reach that
throughput at this shape — this is tensor-core dispatch, not emulation.

## 3. Correctness protocol (run.py verify)

For each prime × seed {20261119, 987654321} × cohort:
random B ∈ {1,16,256} plus 5 edge cohorts (zero, max, delta0, max_top,
alternating) at B=1 → 32 cases. Each case runs 4 routes (SIMT NTT, SIMT
conv, cuBLAS GEMM NTT, mma GEMM NTT); **every coefficient** of every output
polynomial is compared to the Python reference (NTT expectation for NTT
routes; schoolbook negacyclic convolution for the conv route).
Result: 1,138,688 coefficient comparisons, 0 mismatches.
6 negative controls (wrong zetas / wrong limb bases, both primes, both GEMM
routes): all rejected with 4096/4096 mismatches.

## 4. Timing protocol and boundaries

- **Resident** (`bench`): inputs uploaded once; per configuration 10 warmup
  launches + 50 timed launches, each wrapped in cudaEvent records on the
  same stream; report median (and min/max) of per-launch elapsed time.
  One "launch" = the full batch pipeline: SIMT = 1 kernel; GEMM routes =
  decompose kernel + 4/9 cuBLAS GEMMs (or 1 fused mma kernel) + recombine
  kernel. Host-side one-time setup (matrix limbs, handles) is excluded.
- **Transfer-inclusive** (`benchx`): pinned host buffers; per rep
  H2D(input) + pipeline + D2H(output), cudaEvent-timed, 3 warmup + 5 reps,
  median reported.
- B ∈ {1,16,256,4096}; both primes; 4 methods resident, 3 methods
  transfer-inclusive (SIMT conv transfer-inclusive not measured — conv
  needs two input polynomials; boundary stated rather than faked).
- Reported: µs/launch, polynomials/s (= B/median), µs/poly.
- Environment per JSON: GPU name, compute cap, CUDA driver/runtime version,
  cuBLAS version, nvidia-smi driver string, UTC timestamp, SHA-256 of all
  executed sources **and the compiled binary**.
- Serialization: `CUDA_VISIBLE_DEVICES=0 flock /tmp/requant-new-studies-gpu.lock`
  (campaign protocol); GPU 0 = RTX 5060 Ti only. CMP 50HX (Turing) never used.
  Other tenants held ~279 MiB of GPU 0 memory but no active compute during
  the runs; min–max ranges are tight (see RESULTS.md).

## 5. Known boundaries / what is NOT claimed

- Forward NTT only for the GEMM comparison; no GEMM-based INTT or conv.
- n=256 only; no RNS multi-prime batching, no CUDA graphs, no streams overlap.
- mma.sync kernel unoptimized (no smem staging); cuBLAS is the library route.
- Normal-form Barrett, not Montgomery — identical linear maps, different
  constant factors; official-table checks bridge the representation.
- These are NTT/convolution microbenchmarks, not end-to-end ML-KEM/ML-DSA
  operation timings.
