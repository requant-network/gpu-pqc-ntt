// pqc_ntt.cu -- Exact NTT for ML-KEM (q=3329) and ML-DSA (q=8380417), n=256,
// on SM120 (Blackwell, RTX 5060 Ti). Three compute routes:
//
//   1. SIMT: shared-memory radix-2 Cooley-Tukey NTT / Gentleman-Sande INTT,
//      one 256-thread block per polynomial, Barrett reduction (normal form,
//      no Montgomery representation). Kyber uses the standard INCOMPLETE
//      7-layer NTT + 128 degree-2 base products; Dilithium the full 8-layer
//      NTT + coefficient-wise pointwise.
//   2. cuBLAS INT8 GEMM (NTT-as-GEMM): fixed NTT matrix M decomposed into
//      balanced signed int8 limbs (Kyber: 2 limbs base 128; Dilithium:
//      3 limbs base 256 centered), one cublasGemmEx(CUBLAS_COMPUTE_32I,
//      CUDA_R_8I x CUDA_R_8I -> CUDA_R_32I) per limb pair, exact int64
//      recombination mod q. K*max_digit^2 = 2^20 / 2^22 < 2^31, so int32
//      accumulators are exact.
//   3. mma.sync IMMA: explicit mma.sync.aligned.m16n8k32.row.col.s32.s8.s8.s32
//      kernel (native SM120 INT8 tensor cores, no library dispatch ambiguity),
//      same limb decomposition / recombination as route 2.
//
// Negative controls: `badzeta` (non-bit-reversed zeta table) for SIMT and
// `bad` (wrong limb base 127/255 in recombination) for the GEMM routes.
//
// Build: /usr/local/cuda/bin/nvcc -std=c++17 -O3 -arch=sm_120 \
//        cuda/pqc_ntt.cu -o build/pqc_ntt -lcublas
// All GPU work must run with CUDA_VISIBLE_DEVICES=0 (RTX 5060 Ti only).

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cstdint>
#include <cmath>
#include <vector>
#include <string>
#include <algorithm>
#include <functional>
#include <cuda_runtime.h>
#include <cublas_v2.h>

#define CK(x) do { cudaError_t e_ = (x); if (e_ != cudaSuccess) { \
  fprintf(stderr, "CUDA error %s at %s:%d: %s\n", #x, __FILE__, __LINE__, \
          cudaGetErrorString(e_)); exit(1); } } while (0)
#define CB(x) do { cublasStatus_t s_ = (x); if (s_ != CUBLAS_STATUS_SUCCESS) { \
  fprintf(stderr, "cuBLAS error %d at %s:%d\n", (int)s_, __FILE__, __LINE__); \
  exit(1); } } while (0)

static const int N = 256;
static const int QK = 3329;
static const int QD = 8380417;
static const int64_t MUK = 1290167;            // floor(2^32 / 3329)
static const uint64_t MUD = 2201172575745ULL;  // floor(2^64 / 8380417)

// ---------------------------------------------------------------------------
// Device arithmetic (exact, Barrett; mirrors tests/test_reference.py)
// ---------------------------------------------------------------------------

template<int Q> __device__ __forceinline__ int red(int64_t a);

template<> __device__ __forceinline__ int red<QK>(int64_t a) {
  // 0 <= a < 2*QK^2; quotient estimate via 32-bit Barrett
  int64_t t = (a * MUK) >> 32;
  int r = (int)(a - t * QK);
  if (r >= QK) r -= QK;
  return r;
}

template<> __device__ __forceinline__ int red<QD>(int64_t a) {
  // 0 <= a < 2*QD^2 (~1.4e14); quotient estimate via 64-bit Barrett
  uint64_t t = __umul64hi((uint64_t)a, MUD);
  int64_t r = a - (int64_t)(t * (uint64_t)QD);
  if (r >= QD) r -= QD;
  return (int)r;
}

template<int Q> __device__ __forceinline__ int addm(int a, int b) {
  int s = a + b; if (s >= Q) s -= Q; return s;
}
template<int Q> __device__ __forceinline__ int subm(int a, int b) {
  int s = a - b; if (s < 0) s += Q; return s;
}

// ---------------------------------------------------------------------------
// Shared-memory NTT / INTT (all 256 threads of the block participate)
// FULL=false: Kyber (7 CT layers len 128..2, GS len 2..128, scale 128^-1)
// FULL=true:  Dilithium (8 CT layers len 128..1, GS len 1..128, scale 256^-1)
// Zeta index math (identical for both primes, tables differ):
//   CT layer len, block b: k = 128/len + b
//   GS layer len, block b: k = 256/len - 1 - b
// ---------------------------------------------------------------------------

template<int Q, bool FULL>
__device__ void ntt_shared(int* s, const int* zetas) {
  const int tid = threadIdx.x;
  int len = 128;
#pragma unroll
  for (int l = 0; l < (FULL ? 8 : 7); l++) {
    __syncthreads();
    if ((tid & len) == 0) {
      int k = 128 / len + tid / (2 * len);
      int z = zetas[k];
      int t = red<Q>((int64_t)s[tid + len] * z);
      int u = s[tid];
      s[tid] = addm<Q>(u, t);
      s[tid + len] = subm<Q>(u, t);
    }
    len >>= 1;
  }
  __syncthreads();
}

template<int Q, bool FULL>
__device__ void intt_shared(int* s, const int* zetas, int inv_n) {
  const int tid = threadIdx.x;
  int len = FULL ? 1 : 2;
#pragma unroll
  for (int l = 0; l < (FULL ? 8 : 7); l++) {
    __syncthreads();
    if ((tid & len) == 0) {
      int k = 256 / len - 1 - tid / (2 * len);
      int z = zetas[k];
      int t = s[tid];
      int u = s[tid + len];
      s[tid] = addm<Q>(t, u);
      // GS butterfly: (U,V) -> (U+V, (V-U)*zeta). Both official Kyber
      // (r[j+len]-t times +zetas[k]) and Dilithium (t-r[j+len] times
      // -zetas[k]) reduce to this same normal-form expression.
      s[tid + len] = red<Q>((int64_t)subm<Q>(u, t) * z);
    }
    len <<= 1;
  }
  __syncthreads();
  s[tid] = red<Q>((int64_t)s[tid] * inv_n);
  __syncthreads();
}

template<int Q, bool FULL>
__global__ void ntt_kernel(const int* __restrict__ in, int* __restrict__ out,
                           const int* __restrict__ zetas) {
  __shared__ int s[N];
  const size_t off = (size_t)blockIdx.x * N;
  s[threadIdx.x] = in[off + threadIdx.x];
  ntt_shared<Q, FULL>(s, zetas);
  out[off + threadIdx.x] = s[threadIdx.x];
}

template<int Q, bool FULL>
__global__ void intt_kernel(const int* __restrict__ in, int* __restrict__ out,
                            const int* __restrict__ zetas, int inv_n) {
  __shared__ int s[N];
  const size_t off = (size_t)blockIdx.x * N;
  s[threadIdx.x] = in[off + threadIdx.x];
  intt_shared<Q, FULL>(s, zetas, inv_n);
  out[off + threadIdx.x] = s[threadIdx.x];
}

// Full negacyclic convolution: NTT(a), NTT(b), pointwise, INTT.
// gammas[i] = 17^(2*bitrev7(i)+1) mod q (Kyber base-product moduli).
template<int Q, bool FULL>
__global__ void conv_kernel(const int* __restrict__ a, const int* __restrict__ b,
                            int* __restrict__ out, const int* __restrict__ zetas,
                            const int* __restrict__ gammas, int inv_n) {
  __shared__ int s[2][N];
  const int tid = threadIdx.x;
  const size_t off = (size_t)blockIdx.x * N;
  s[0][tid] = a[off + tid];
  ntt_shared<Q, FULL>(s[0], zetas);
  s[1][tid] = b[off + tid];
  ntt_shared<Q, FULL>(s[1], zetas);
  int v;
  if (FULL) {
    v = red<Q>((int64_t)s[0][tid] * s[1][tid]);
  } else {
    int i = tid / 2;
    int g = gammas[i];
    int a0 = s[0][2 * i], a1 = s[0][2 * i + 1];
    int b0 = s[1][2 * i], b1 = s[1][2 * i + 1];
    if ((tid & 1) == 0) {
      int t = red<Q>((int64_t)a1 * b1);
      t = red<Q>((int64_t)t * g);
      v = addm<Q>(red<Q>((int64_t)a0 * b0), t);
    } else {
      v = addm<Q>(red<Q>((int64_t)a0 * b1), red<Q>((int64_t)a1 * b0));
    }
  }
  __syncthreads();
  s[0][tid] = v;
  intt_shared<Q, FULL>(s[0], zetas, inv_n);
  out[off + tid] = s[0][tid];
}

// ---------------------------------------------------------------------------
// Limb decomposition / recombination for the tensor-core routes
// ---------------------------------------------------------------------------

// in: n*B coefficients in [0,q), column-major (poly k, coeff j at k*n+j).
// Limbs use the same layout, int8. Kyber: base 128, d0 in [-64,63].
// Dilithium: base 256 centered, d0,d1 in [-128,127], d2 in [-64,64].
__global__ void decompose_kernel(const int* __restrict__ in, int8_t* L0,
                                 int8_t* L1, int8_t* L2, int total, int q) {
  int idx = blockIdx.x * blockDim.x + threadIdx.x;
  if (idx >= total) return;
  int v = in[idx];
  int c = (v <= q / 2) ? v : v - q;
  if (q == QK) {
    int d0 = c % 128;
    if (d0 > 63) d0 -= 128;
    if (d0 < -64) d0 += 128;
    int d1 = (c - d0) / 128;
    L0[idx] = (int8_t)d0;
    L1[idx] = (int8_t)d1;
  } else {
    int d0 = c % 256;
    if (d0 > 127) d0 -= 256;
    if (d0 < -128) d0 += 256;
    int rem = (c - d0) / 256;
    int d1 = rem % 256;
    if (d1 > 127) d1 -= 256;
    if (d1 < -128) d1 += 256;
    int d2 = (rem - d1) / 256;
    L0[idx] = (int8_t)d0;
    L1[idx] = (int8_t)d1;
    L2[idx] = (int8_t)d2;
  }
}

// C[(p*nl+r)*cstride + idx] = sum_j M_p[i][j] * A_r[j][k] (exact int32).
// Recombine with limb weights and reduce mod q. bad=1 uses base 127/255
// (deliberately wrong negative control).
__global__ void recombine_kernel(const int* __restrict__ C, int* __restrict__ out,
                                 int total, size_t cstride, int q, int nl,
                                 int bad) {
  int idx = blockIdx.x * blockDim.x + threadIdx.x;
  if (idx >= total) return;
  int64_t v;
  if (nl == 2) {
    int64_t b = bad ? 127 : 128;
    v = (int64_t)C[idx]
      + b * ((int64_t)C[cstride + idx] + C[2 * cstride + idx])
      + b * b * (int64_t)C[3 * cstride + idx];
  } else {
    int64_t b = bad ? 255 : 256;
    int64_t b2 = b * b, b3 = b2 * b, b4 = b3 * b;
    v = (int64_t)C[idx]
      + b * ((int64_t)C[cstride + idx] + C[3 * cstride + idx])
      + b2 * ((int64_t)C[2 * cstride + idx] + C[4 * cstride + idx]
              + C[6 * cstride + idx])
      + b3 * ((int64_t)C[5 * cstride + idx] + C[7 * cstride + idx])
      + b4 * (int64_t)C[8 * cstride + idx];
  }
  v %= q;
  if (v < 0) v += q;
  out[idx] = (int)v;
}

// ---------------------------------------------------------------------------
// Native INT8 tensor-core GEMM: mma.sync.aligned.m16n8k32.row.col.s32.s8.s8.s32
// C(256 x n) = A(256 x 256, row-major) * B(256 x n, column-major), int8 -> int32.
// One warp per 16x8 output tile; K = 256 = 8 steps of 32. No padding needed
// (tail columns guarded). Fragment layouts per PTX ISA (mma.m16n8k32, .s8).
// ---------------------------------------------------------------------------

__global__ void imma_gemm_kernel(const int8_t* __restrict__ A,
                                 const int8_t* __restrict__ B,
                                 int* __restrict__ C, int n) {
  int warp = (blockIdx.x * blockDim.x + threadIdx.x) >> 5;
  int lane = threadIdx.x & 31;
  int tiles_n = (n + 7) >> 3;
  int mi = warp / tiles_n;
  int ni = warp % tiles_n;
  if (mi >= 16) return;
  int n0 = ni * 8;
  int gid = lane >> 2, tig = lane & 3;
  int c0 = 0, c1 = 0, c2 = 0, c3 = 0;
  const int8_t* Arow0 = A + (mi * 16 + gid) * 256;
  const int8_t* Arow1 = A + (mi * 16 + gid + 8) * 256;
  int col = n0 + gid;
#pragma unroll
  for (int k0 = 0; k0 < 256; k0 += 32) {
    int a0 = *(const int*)(Arow0 + k0 + tig * 4);
    int a1 = *(const int*)(Arow1 + k0 + tig * 4);
    int a2 = *(const int*)(Arow0 + k0 + tig * 4 + 16);
    int a3 = *(const int*)(Arow1 + k0 + tig * 4 + 16);
    int b0 = 0, b1 = 0;
    if (col < n) {
      const int8_t* Bp = B + (size_t)col * 256 + k0 + tig * 4;
      b0 = (uint8_t)Bp[0] | ((uint8_t)Bp[1] << 8)
         | ((uint8_t)Bp[2] << 16) | ((uint8_t)Bp[3] << 24);
      b1 = (uint8_t)Bp[16] | ((uint8_t)Bp[17] << 8)
         | ((uint8_t)Bp[18] << 16) | ((uint8_t)Bp[19] << 24);
    }
    asm volatile(
        "mma.sync.aligned.m16n8k32.row.col.s32.s8.s8.s32 "
        "{%0,%1,%2,%3}, {%4,%5,%6,%7}, {%8,%9}, {%0,%1,%2,%3};\n"
        : "+r"(c0), "+r"(c1), "+r"(c2), "+r"(c3)
        : "r"(a0), "r"(a1), "r"(a2), "r"(a3), "r"(b0), "r"(b1));
  }
  int r0 = mi * 16 + gid, r1 = r0 + 8;
  int cc0 = n0 + tig * 2, cc1 = cc0 + 1;
  if (cc0 < n) {
    C[r0 + (size_t)cc0 * 256] = c0;
    C[r1 + (size_t)cc0 * 256] = c2;
  }
  if (cc1 < n) {
    C[r0 + (size_t)cc1 * 256] = c1;
    C[r1 + (size_t)cc1 * 256] = c3;
  }
}

// Naive SIMT int8 GEMM control (one thread per output element). Used only as
// a dispatch control: cuBLAS/mma should vastly outperform this if they hit
// tensor cores.
__global__ void simt_gemm_kernel(const int8_t* __restrict__ A,
                                 const int8_t* __restrict__ B,
                                 int* __restrict__ C, int n) {
  int idx = blockIdx.x * blockDim.x + threadIdx.x;
  if (idx >= 256 * n) return;
  int row = idx & 255, col = idx >> 8;
  int64_t s = 0;
  const int8_t* Ar = A + row * 256;
  const int8_t* Bc = B + (size_t)col * 256;
  for (int k = 0; k < 256; k++) s += (int)Ar[k] * (int)Bc[k];
  C[idx] = (int)s;
}

static void launch_imma(const int8_t* A, const int8_t* B, int* C, int n) {
  int warps = 16 * ((n + 7) / 8);
  int threads = 256;  // 8 warps per block
  int blocks = (warps + 7) / 8;
  imma_gemm_kernel<<<blocks, threads>>>(A, B, C, n);
}

static void launch_simt_gemm(const int8_t* A, const int8_t* B, int* C, int n) {
  int total = 256 * n;
  simt_gemm_kernel<<<(total + 255) / 256, 256>>>(A, B, C, n);
}

// ---------------------------------------------------------------------------
// Host helpers
// ---------------------------------------------------------------------------

static int powmod(int64_t b, int64_t e, int m) {
  int64_t r = 1; b %= m;
  while (e > 0) { if (e & 1) r = r * b % m; b = b * b % m; e >>= 1; }
  return (int)r;
}
static int brv(int x, int bits) {
  int r = 0;
  for (int i = 0; i < bits; i++) { r = (r << 1) | (x & 1); x >>= 1; }
  return r;
}

struct Prime {
  int q, root, nl, full, inv_n;
};
static Prime KYBER{QK, 17, 2, 0, 0};
static Prime DILITHIUM{QD, 1753, 3, 1, 0};

static Prime get_prime(const std::string& s) {
  if (s == "kyber") { KYBER.inv_n = powmod(128, QK - 2, QK); return KYBER; }
  if (s == "dilithium") { DILITHIUM.inv_n = powmod(256, QD - 2, QD); return DILITHIUM; }
  fprintf(stderr, "unknown prime %s\n", s.c_str()); exit(1);
}

// Zeta tables in NORMAL form, derived exactly as in reference.py.
static std::vector<int> make_zetas(const Prime& p, bool bad) {
  std::vector<int> z(256, 0);
  int kmax = p.full ? 255 : 127;
  int bits = p.full ? 8 : 7;
  for (int k = 1; k <= kmax; k++)
    z[k] = powmod(p.root, bad ? k : brv(k, bits), p.q);
  return z;
}
static std::vector<int> make_gammas(const Prime& p) {
  std::vector<int> g(128, 0);
  if (!p.full)
    for (int i = 0; i < 128; i++)
      g[i] = powmod(p.root, 2 * brv(i, 7) + 1, p.q);
  return g;
}

// Fixed NTT matrix M (normal form), identical definition to reference.py.
static std::vector<int> make_matrix(const Prime& p) {
  std::vector<int> M(N * N);
  if (!p.full) {
    for (int i = 0; i < 128; i++) {
      int g = powmod(p.root, 2 * brv(i, 7) + 1, p.q);
      int64_t gk = 1;
      for (int k = 0; k < 128; k++) {
        M[(2 * i) * N + 2 * k] = (int)gk;
        M[(2 * i + 1) * N + 2 * k + 1] = (int)gk;
        gk = gk * g % p.q;
      }
    }
  } else {
    for (int i = 0; i < N; i++) {
      int e = 2 * brv(i, 8) + 1;
      for (int j = 0; j < N; j++)
        M[i * N + j] = powmod(p.root, (int64_t)e * j, p.q);
    }
  }
  return M;
}

static void decompose_host(const std::vector<int>& M, int q, int nl,
                           std::vector<std::vector<int8_t>>& L) {
  L.assign(nl, std::vector<int8_t>(N * N));
  for (int idx = 0; idx < N * N; idx++) {
    int v = M[idx];
    int c = (v <= q / 2) ? v : v - q;
    if (nl == 2) {
      int d0 = c % 128; if (d0 > 63) d0 -= 128; if (d0 < -64) d0 += 128;
      int d1 = (c - d0) / 128;
      L[0][idx] = (int8_t)d0; L[1][idx] = (int8_t)d1;
    } else {
      int d0 = c % 256; if (d0 > 127) d0 -= 256; if (d0 < -128) d0 += 256;
      int rem = (c - d0) / 256;
      int d1 = rem % 256; if (d1 > 127) d1 -= 256; if (d1 < -128) d1 += 256;
      int d2 = (rem - d1) / 256;
      L[0][idx] = (int8_t)d0; L[1][idx] = (int8_t)d1; L[2][idx] = (int8_t)d2;
    }
  }
}

static std::vector<int32_t> read_i32(const char* path, size_t count) {
  FILE* f = fopen(path, "rb");
  if (!f) { fprintf(stderr, "cannot open %s\n", path); exit(1); }
  std::vector<int32_t> v(count);
  size_t got = fread(v.data(), 4, count, f);
  fclose(f);
  if (got != count) { fprintf(stderr, "short read %s: %zu/%zu\n", path, got, count); exit(1); }
  return v;
}
static void write_i32(const char* path, const std::vector<int32_t>& v) {
  FILE* f = fopen(path, "wb");
  if (!f) { fprintf(stderr, "cannot write %s\n", path); exit(1); }
  fwrite(v.data(), 4, v.size(), f);
  fclose(f);
}

static void gpu_info_json(FILE* out) {
  cudaDeviceProp prop;
  CK(cudaGetDeviceProperties(&prop, 0));
  int drv = 0, rt = 0;
  CK(cudaDriverGetVersion(&drv));
  CK(cudaRuntimeGetVersion(&rt));
  fprintf(out, "\"gpu\": \"%s\", \"compute_cap\": \"%d.%d\", "
          "\"driver_version\": %d, \"cuda_runtime_version\": %d",
          prop.name, prop.major, prop.minor, drv, rt);
}

// ---------------------------------------------------------------------------
// Pipeline objects
// ---------------------------------------------------------------------------

struct SimtPipeline {
  Prime p;
  int B;
  int *dIn = nullptr, *dOut = nullptr, *dZ = nullptr, *dG = nullptr;
  int *dInB = nullptr;  // second operand for conv
  int mode;             // 0=ntt, 1=intt, 2=conv
  void init(const Prime& pr, int b, int m, const std::vector<int32_t>& in,
            bool badzeta) {
    p = pr; B = b; mode = m;
    auto z = make_zetas(p, badzeta);
    auto g = make_gammas(p);
    CK(cudaMalloc(&dIn, (size_t)B * N * 4));
    CK(cudaMalloc(&dOut, (size_t)B * N * 4));
    CK(cudaMalloc(&dZ, 256 * 4));
    CK(cudaMalloc(&dG, 128 * 4));
    CK(cudaMemcpy(dIn, in.data(), (size_t)B * N * 4, cudaMemcpyHostToDevice));
    CK(cudaMemcpy(dZ, z.data(), 256 * 4, cudaMemcpyHostToDevice));
    CK(cudaMemcpy(dG, g.data(), 128 * 4, cudaMemcpyHostToDevice));
    if (mode == 2) {
      CK(cudaMalloc(&dInB, (size_t)B * N * 4));
      CK(cudaMemcpy(dInB, in.data() + (size_t)B * N, (size_t)B * N * 4,
                    cudaMemcpyHostToDevice));
    }
  }
  void launch() {
    if (mode == 0) {
      if (p.full) ntt_kernel<QD, true><<<B, N>>>(dIn, dOut, dZ);
      else ntt_kernel<QK, false><<<B, N>>>(dIn, dOut, dZ);
    } else if (mode == 1) {
      if (p.full) intt_kernel<QD, true><<<B, N>>>(dIn, dOut, dZ, p.inv_n);
      else intt_kernel<QK, false><<<B, N>>>(dIn, dOut, dZ, p.inv_n);
    } else {
      if (p.full) conv_kernel<QD, true><<<B, N>>>(dIn, dInB, dOut, dZ, dG, p.inv_n);
      else conv_kernel<QK, false><<<B, N>>>(dIn, dInB, dOut, dZ, dG, p.inv_n);
    }
  }
  std::vector<int32_t> result() {
    std::vector<int32_t> r((size_t)B * N);
    CK(cudaMemcpy(r.data(), dOut, (size_t)B * N * 4, cudaMemcpyDeviceToHost));
    return r;
  }
  void free_all() {
    cudaFree(dIn); cudaFree(dOut); cudaFree(dZ); cudaFree(dG);
    if (dInB) cudaFree(dInB);
  }
};

struct GemmPipeline {
  Prime p;
  int B, Bpad;
  bool use_mma;
  int bad;
  cublasHandle_t h = nullptr;
  int *dIn = nullptr, *dOut = nullptr;
  int8_t *dM[3] = {nullptr, nullptr, nullptr};
  int8_t *dA[3] = {nullptr, nullptr, nullptr};
  int *dC = nullptr;
  size_t cstride;
  void init(const Prime& pr, int b, bool mma, int bad_,
            const std::vector<int32_t>& in) {
    p = pr; B = b; use_mma = mma; bad = bad_;
    // cuBLAS INT8 path pads the batch to a multiple of 16 (library alignment
    // preference); the mma.sync path needs no padding.
    Bpad = use_mma ? B : ((B + 15) / 16) * 16;
    cstride = (size_t)Bpad * N;
    auto M = make_matrix(p);
    if (!use_mma) {
      // cuBLAS OP_N reads column-major: upload M^T (row-major) so that the
      // matrix cuBLAS sees is M itself. The mma.sync route reads A row-major
      // and therefore uses M directly.
      std::vector<int> Mt(N * N);
      for (int i = 0; i < N; i++)
        for (int j = 0; j < N; j++)
          Mt[j * N + i] = M[i * N + j];
      M.swap(Mt);
    }
    std::vector<std::vector<int8_t>> L;
    decompose_host(M, p.q, p.nl, L);
    CK(cudaMalloc(&dIn, (size_t)B * N * 4));
    CK(cudaMalloc(&dOut, (size_t)B * N * 4));
    CK(cudaMemcpy(dIn, in.data(), (size_t)B * N * 4, cudaMemcpyHostToDevice));
    for (int t = 0; t < p.nl; t++) {
      CK(cudaMalloc(&dM[t], N * N));
      CK(cudaMemcpy(dM[t], L[t].data(), N * N, cudaMemcpyHostToDevice));
      CK(cudaMalloc(&dA[t], cstride));
      CK(cudaMemset(dA[t], 0, cstride));
    }
    CK(cudaMalloc(&dC, (size_t)p.nl * p.nl * cstride * 4));
    if (!use_mma) CB(cublasCreate(&h));
  }
  void launch() {
    int total = B * N;
    decompose_kernel<<<(total + 255) / 256, 256>>>(dIn, dA[0], dA[1], dA[2],
                                                 total, p.q);
    int alpha = 1, beta = 0;
    for (int pp = 0; pp < p.nl; pp++) {
      for (int r = 0; r < p.nl; r++) {
        int* Cpr = dC + (size_t)(pp * p.nl + r) * cstride;
        if (use_mma) {
          launch_imma(dM[pp], dA[r], Cpr, B);
        } else {
          CB(cublasGemmEx(h, CUBLAS_OP_N, CUBLAS_OP_N, N, Bpad, N, &alpha,
                          dM[pp], CUDA_R_8I, N, dA[r], CUDA_R_8I, N, &beta,
                          Cpr, CUDA_R_32I, N, CUBLAS_COMPUTE_32I,
                          CUBLAS_GEMM_DEFAULT));
        }
      }
    }
    recombine_kernel<<<(total + 255) / 256, 256>>>(dC, dOut, total, cstride,
                                                 p.q, p.nl, bad);
  }
  std::vector<int32_t> result() {
    std::vector<int32_t> r((size_t)B * N);
    CK(cudaMemcpy(r.data(), dOut, (size_t)B * N * 4, cudaMemcpyDeviceToHost));
    return r;
  }
  void free_all() {
    cudaFree(dIn); cudaFree(dOut); cudaFree(dC);
    for (int t = 0; t < 3; t++) {
      if (dM[t]) cudaFree(dM[t]);
      if (dA[t]) cudaFree(dA[t]);
    }
    if (h) cublasDestroy(h);
  }
};

// ---------------------------------------------------------------------------
// Timing
// ---------------------------------------------------------------------------

struct Timing {
  double median_us, min_us, max_us, mean_us;
  std::vector<double> all_us;
};

static Timing time_launches(const std::function<void()>& launch, int warmup,
                            int launches) {
  cudaEvent_t start, stop;
  CK(cudaEventCreate(&start));
  CK(cudaEventCreate(&stop));
  for (int i = 0; i < warmup; i++) { launch(); }
  CK(cudaDeviceSynchronize());
  Timing t;
  for (int i = 0; i < launches; i++) {
    CK(cudaEventRecord(start));
    launch();
    CK(cudaEventRecord(stop));
    CK(cudaEventSynchronize(stop));
    float ms = 0;
    CK(cudaEventElapsedTime(&ms, start, stop));
    t.all_us.push_back((double)ms * 1000.0);
  }
  CK(cudaEventDestroy(start));
  CK(cudaEventDestroy(stop));
  std::vector<double> s = t.all_us;
  std::sort(s.begin(), s.end());
  t.median_us = s[s.size() / 2];
  t.min_us = s.front();
  t.max_us = s.back();
  double sum = 0; for (double x : t.all_us) sum += x;
  t.mean_us = sum / t.all_us.size();
  return t;
}

static void print_timing_json(FILE* out, const Timing& t, const Prime& p, int B,
                              const char* method, int warmup, int launches,
                              int Bpad) {
  fprintf(out, "{");
  gpu_info_json(out);
  fprintf(out, ", \"prime\": %d, \"B\": %d, \"Bpad\": %d, \"method\": \"%s\", "
          "\"warmup\": %d, \"launches\": %d, "
          "\"median_us\": %.3f, \"min_us\": %.3f, \"max_us\": %.3f, "
          "\"mean_us\": %.3f, \"us_per_poly\": %.4f, \"polys_per_s\": %.1f, "
          "\"all_us\": [",
          p.q, B, Bpad, method, warmup, launches, t.median_us, t.min_us,
          t.max_us, t.mean_us, t.median_us / B, B * 1e6 / t.median_us);
  for (size_t i = 0; i < t.all_us.size(); i++)
    fprintf(out, "%s%.3f", i ? "," : "", t.all_us[i]);
  fprintf(out, "]}\n");
}

// ---------------------------------------------------------------------------
// Modes
// ---------------------------------------------------------------------------

static int mode_ntt(int argc, char** argv, int conv_mode) {
  // ntt <prime> <B> <in> <out> [badzeta]   /  conv <prime> <B> <in> <out>
  Prime p = get_prime(argv[2]);
  int B = atoi(argv[3]);
  bool badzeta = argc > 6 && std::string(argv[6]) == "badzeta";
  size_t count = (size_t)B * N * (conv_mode == 2 ? 2 : 1);
  auto in = read_i32(argv[4], count);
  SimtPipeline pl;
  pl.init(p, B, conv_mode, in, badzeta);
  pl.launch();
  CK(cudaDeviceSynchronize());
  CK(cudaGetLastError());
  write_i32(argv[5], pl.result());
  pl.free_all();
  return 0;
}

static int mode_gemm(int argc, char** argv) {
  // gemm <prime> <B> <in> <out> [cublas|mma] [bad]
  Prime p = get_prime(argv[2]);
  int B = atoi(argv[3]);
  bool mma = argc > 6 && std::string(argv[6]) == "mma";
  int bad = argc > 7 && std::string(argv[7]) == "bad" ? 1 : 0;
  auto in = read_i32(argv[4], (size_t)B * N);
  GemmPipeline pl;
  pl.init(p, B, mma, bad, in);
  pl.launch();
  CK(cudaDeviceSynchronize());
  CK(cudaGetLastError());
  write_i32(argv[5], pl.result());
  pl.free_all();
  return 0;
}

static int mode_bench(int argc, char** argv) {
  // bench <prime> <B> <simt-ntt|simt-conv|gemm-cublas|gemm-mma> [launches]
  Prime p = get_prime(argv[2]);
  int B = atoi(argv[3]);
  std::string method = argv[4];
  int launches = argc > 5 ? atoi(argv[5]) : 50;
  int warmup = 10;
  // deterministic resident input
  std::vector<int32_t> in((size_t)B * N * (method == "simt-conv" ? 2 : 1));
  uint64_t s = 0x9e3779b97f4a7c15ULL ^ (uint64_t)B;
  for (auto& v : in) {
    s ^= s << 13; s ^= s >> 7; s ^= s << 17;
    v = (int32_t)(s % (uint64_t)p.q);
  }
  Timing t;
  int Bpad = B;
  if (method == "simt-ntt" || method == "simt-conv") {
    SimtPipeline pl;
    pl.init(p, B, method == "simt-conv" ? 2 : 0, in, false);
    t = time_launches([&]() { pl.launch(); }, warmup, launches);
    pl.free_all();
  } else if (method == "gemm-cublas" || method == "gemm-mma") {
    GemmPipeline pl;
    pl.init(p, B, method == "gemm-mma", 0, in);
    Bpad = pl.Bpad;
    t = time_launches([&]() { pl.launch(); }, warmup, launches);
    pl.free_all();
  } else {
    fprintf(stderr, "unknown method %s\n", method.c_str());
    return 1;
  }
  print_timing_json(stdout, t, p, B, method.c_str(), warmup, launches, Bpad);
  return 0;
}

static int mode_benchx(int argc, char** argv) {
  // benchx <prime> <B> <simt-ntt|gemm-cublas|gemm-mma>
  // Transfer-inclusive: pinned-host H2D + resident pipeline + D2H, per rep.
  Prime p = get_prime(argv[2]);
  int B = atoi(argv[3]);
  std::string method = argv[4];
  size_t count = (size_t)B * N;
  std::vector<int32_t> in(count);
  uint64_t s = 0xdeadbeefcafeULL ^ (uint64_t)B;
  for (auto& v : in) {
    s ^= s << 13; s ^= s >> 7; s ^= s << 17;
    v = (int32_t)(s % (uint64_t)p.q);
  }
  int32_t *hIn = nullptr, *hOut = nullptr;
  CK(cudaMallocHost(&hIn, count * 4));
  CK(cudaMallocHost(&hOut, count * 4));
  memcpy(hIn, in.data(), count * 4);

  cudaEvent_t start, stop;
  CK(cudaEventCreate(&start));
  CK(cudaEventCreate(&stop));
  std::vector<double> reps;
  int Bpad = B;

  if (method == "simt-ntt") {
    SimtPipeline pl;
    pl.init(p, B, 0, in, false);
    for (int w = 0; w < 3; w++) {
      CK(cudaMemcpyAsync(pl.dIn, hIn, count * 4, cudaMemcpyHostToDevice));
      pl.launch();
      CK(cudaMemcpyAsync(hOut, pl.dOut, count * 4, cudaMemcpyDeviceToHost));
      CK(cudaDeviceSynchronize());
    }
    for (int i = 0; i < 5; i++) {
      CK(cudaEventRecord(start));
      CK(cudaMemcpyAsync(pl.dIn, hIn, count * 4, cudaMemcpyHostToDevice));
      pl.launch();
      CK(cudaMemcpyAsync(hOut, pl.dOut, count * 4, cudaMemcpyDeviceToHost));
      CK(cudaEventRecord(stop));
      CK(cudaEventSynchronize(stop));
      float ms; CK(cudaEventElapsedTime(&ms, start, stop));
      reps.push_back(ms * 1000.0);
    }
    pl.free_all();
  } else {
    GemmPipeline pl;
    pl.init(p, B, method == "gemm-mma", 0, in);
    Bpad = pl.Bpad;
    for (int w = 0; w < 3; w++) {
      CK(cudaMemcpyAsync(pl.dIn, hIn, count * 4, cudaMemcpyHostToDevice));
      pl.launch();
      CK(cudaMemcpyAsync(hOut, pl.dOut, count * 4, cudaMemcpyDeviceToHost));
      CK(cudaDeviceSynchronize());
    }
    for (int i = 0; i < 5; i++) {
      CK(cudaEventRecord(start));
      CK(cudaMemcpyAsync(pl.dIn, hIn, count * 4, cudaMemcpyHostToDevice));
      pl.launch();
      CK(cudaMemcpyAsync(hOut, pl.dOut, count * 4, cudaMemcpyDeviceToHost));
      CK(cudaEventRecord(stop));
      CK(cudaEventSynchronize(stop));
      float ms; CK(cudaEventElapsedTime(&ms, start, stop));
      reps.push_back(ms * 1000.0);
    }
    pl.free_all();
  }
  std::vector<double> sr = reps;
  std::sort(sr.begin(), sr.end());
  double med = sr[sr.size() / 2];
  printf("{");
  gpu_info_json(stdout);
  printf(", \"prime\": %d, \"B\": %d, \"Bpad\": %d, \"method\": \"%s\", "
         "\"boundary\": \"transfer-inclusive (pinned H2D + pipeline + D2H)\", "
         "\"reps\": %zu, \"median_us\": %.3f, \"min_us\": %.3f, "
         "\"max_us\": %.3f, \"us_per_poly\": %.4f, \"polys_per_s\": %.1f, "
         "\"all_us\": [",
         p.q, B, Bpad, method.c_str(), reps.size(), med, sr.front(), sr.back(),
         med / B, B * 1e6 / med);
  for (size_t i = 0; i < reps.size(); i++)
    printf("%s%.3f", i ? "," : "", reps[i]);
  printf("]}\n");
  CK(cudaFreeHost(hIn));
  CK(cudaFreeHost(hOut));
  CK(cudaEventDestroy(start));
  CK(cudaEventDestroy(stop));
  return 0;
}

// Probe: INT8 GEMM support + exactness + dispatch control on this device.
static int mode_probe() {
  printf("{\"probe\": [");
  cublasHandle_t h;
  CB(cublasCreate(&h));
  int Bs[] = {1, 2, 4, 8, 16, 256, 4096};
  bool first = true;
  for (int B : Bs) {
    size_t count = (size_t)256 * B;
    // deterministic int8 operands in [-64,63] (Kyber-style digits)
    std::vector<int8_t> hA(256 * 256), hB(count);
    uint64_t s = 123456789 ^ (uint64_t)B;
    auto nxt = [&]() { s ^= s << 13; s ^= s >> 7; s ^= s << 17; return (int)(s % 128) - 64; };
    for (auto& v : hA) v = (int8_t)nxt();
    for (auto& v : hB) v = (int8_t)nxt();
    int8_t *dA, *dB, *dAc; int *dC, *dC2, *dC3;
    CK(cudaMalloc(&dA, hA.size()));
    CK(cudaMalloc(&dAc, hA.size()));
    CK(cudaMalloc(&dB, count));
    CK(cudaMalloc(&dC, count * 4));
    CK(cudaMalloc(&dC2, count * 4));
    CK(cudaMalloc(&dC3, count * 4));
    CK(cudaMemcpy(dA, hA.data(), hA.size(), cudaMemcpyHostToDevice));
    // cuBLAS OP_N reads column-major: upload hA^T so cuBLAS computes hA*hB.
    std::vector<int8_t> hAT(hA.size());
    for (int i = 0; i < 256; i++)
      for (int j = 0; j < 256; j++)
        hAT[j * 256 + i] = hA[i * 256 + j];
    CK(cudaMemcpy(dAc, hAT.data(), hA.size(), cudaMemcpyHostToDevice));
    CK(cudaMemcpy(dB, hB.data(), count, cudaMemcpyHostToDevice));
    int alpha = 1, beta = 0;
    cublasStatus_t st = cublasGemmEx(h, CUBLAS_OP_N, CUBLAS_OP_N, 256, B, 256,
        &alpha, dAc, CUDA_R_8I, 256, dB, CUDA_R_8I, 256, &beta, dC, CUDA_R_32I,
        256, CUBLAS_COMPUTE_32I, CUBLAS_GEMM_DEFAULT);
    CK(cudaDeviceSynchronize());
    // host exact reference (int64)
    std::vector<int> expect(count);
    for (int col = 0; col < B; col++)
      for (int row = 0; row < 256; row++) {
        int64_t acc = 0;
        for (int k = 0; k < 256; k++)
          acc += (int)hA[row * 256 + k] * (int)hB[col * 256 + k];
        expect[row + (size_t)col * 256] = (int)acc;
      }
    std::vector<int> got(count), got2(count), got3(count);
    bool cublas_ok = false;
    if (st == CUBLAS_STATUS_SUCCESS) {
      CK(cudaMemcpy(got.data(), dC, count * 4, cudaMemcpyDeviceToHost));
      cublas_ok = (got == expect);
    }
    launch_imma(dA, dB, dC2, B);
    launch_simt_gemm(dA, dB, dC3, B);
    CK(cudaDeviceSynchronize());
    CK(cudaGetLastError());
    CK(cudaMemcpy(got2.data(), dC2, count * 4, cudaMemcpyDeviceToHost));
    CK(cudaMemcpy(got3.data(), dC3, count * 4, cudaMemcpyDeviceToHost));
    bool mma_ok = (got2 == expect), simt_ok = (got3 == expect);
    // single-limb GEMM timings (median of 20, warmup 5)
    double tc = -1, tm = -1, ts = -1;
    if (st == CUBLAS_STATUS_SUCCESS) {
      auto t = time_launches([&]() {
        cublasGemmEx(h, CUBLAS_OP_N, CUBLAS_OP_N, 256, B, 256, &alpha,
            dAc, CUDA_R_8I, 256, dB, CUDA_R_8I, 256, &beta, dC, CUDA_R_32I,
            256, CUBLAS_COMPUTE_32I, CUBLAS_GEMM_DEFAULT);
      }, 5, 20);
      tc = t.median_us;
    }
    {
      auto t = time_launches([&]() { launch_imma(dA, dB, dC2, B); }, 5, 20);
      tm = t.median_us;
    }
    {
      auto t = time_launches([&]() { launch_simt_gemm(dA, dB, dC3, B); }, 5, 20);
      ts = t.median_us;
    }
    if (!first) printf(", ");
    first = false;
    printf("{\"B\": %d, \"cublas_status\": %d, \"cublas_exact\": %s, "
           "\"mma_exact\": %s, \"simt_control_exact\": %s, "
           "\"cublas_us\": %.3f, \"mma_us\": %.3f, \"simt_control_us\": %.3f}",
           B, (int)st, cublas_ok ? "true" : "false", mma_ok ? "true" : "false",
           simt_ok ? "true" : "false", tc, tm, ts);
    cudaFree(dA); cudaFree(dAc); cudaFree(dB); cudaFree(dC); cudaFree(dC2); cudaFree(dC3);
  }
  cublasDestroy(h);
  printf("], ");
  gpu_info_json(stdout);
  printf("}\n");
  return 0;
}

int main(int argc, char** argv) {
  if (argc < 2) {
    fprintf(stderr,
        "usage: pqc_ntt <mode> ...\n"
        "  info\n"
        "  probe\n"
        "  ntt <kyber|dilithium> <B> <in.bin> <out.bin> [badzeta]\n"
        "  conv <kyber|dilithium> <B> <in.bin(a||b)> <out.bin>\n"
        "  gemm <kyber|dilithium> <B> <in.bin> <out.bin> [cublas|mma] [bad]\n"
        "  bench <kyber|dilithium> <B> <simt-ntt|simt-conv|gemm-cublas|gemm-mma> [launches]\n"
        "  benchx <kyber|dilithium> <B> <simt-ntt|gemm-cublas|gemm-mma>\n");
    return 1;
  }
  CK(cudaSetDevice(0));
  std::string mode = argv[1];
  if (mode == "info") {
    printf("{");
    gpu_info_json(stdout);
    int v = 0;
    cublasHandle_t h; CB(cublasCreate(&h));
    CB(cublasGetVersion(h, &v));
    cublasDestroy(h);
    printf(", \"cublas_version\": %d}\n", v);
    return 0;
  }
  if (mode == "probe") return mode_probe();
  if (mode == "ntt") return mode_ntt(argc, argv, 0);
  if (mode == "conv") return mode_ntt(argc, argv, 2);
  if (mode == "gemm") return mode_gemm(argc, argv);
  if (mode == "bench") return mode_bench(argc, argv);
  if (mode == "benchx") return mode_benchx(argc, argv);
  fprintf(stderr, "unknown mode %s\n", mode.c_str());
  return 1;
}
