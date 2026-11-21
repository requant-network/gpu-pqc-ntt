// Reuse the measured SIMT butterflies and exact modular arithmetic.
#define main pqc_ntt_benchmark_main
#include "../../cuda/pqc_ntt.cu"
#undef main

#include <mutex>

namespace {
constexpr int Outputs = 4;
constexpr int MatrixPolys = 16;
constexpr int ForwardPolys = 5;
constexpr int InputPolys = MatrixPolys + Outputs + ForwardPolys;
constexpr int MaxBatch = 128;

__global__ void forward_batch(int *inputs, const int *zetas) {
    __shared__ int values[N];
    const int witness = blockIdx.x / ForwardPolys;
    const int poly = blockIdx.x % ForwardPolys;
    int *row = inputs + (witness * InputPolys + MatrixPolys + Outputs + poly) * N;
    values[threadIdx.x] = row[threadIdx.x];
    ntt_shared<QD, true>(values, zetas);
    row[threadIdx.x] = values[threadIdx.x];
}

__global__ void reconstruct_batch(const int *inputs, int *outputs, const int *zetas) {
    __shared__ int values[N];
    const int witness = blockIdx.x / Outputs;
    const int row = blockIdx.x % Outputs;
    const int coefficient = threadIdx.x;
    const int *base = inputs + witness * InputPolys * N + coefficient;
    const int *forward = base + (MatrixPolys + Outputs) * N;
    int sum = 0;
    for (int col = 0; col < Outputs; ++col) {
        const int a = base[(row * Outputs + col) * N];
        sum = addm<QD>(sum, red<QD>(static_cast<int64_t>(a) * forward[col * N]));
    }
    const int t1 = base[(MatrixPolys + row) * N];
    const int c = forward[Outputs * N];
    values[coefficient] = subm<QD>(sum, red<QD>(static_cast<int64_t>(c) * t1));
    intt_shared<QD, true>(values, zetas, 8347681);
    outputs[blockIdx.x * N + coefficient] = values[coefficient];
}

struct Buffers {
    int *inputs = nullptr;
    int *outputs = nullptr;
    int *zetas = nullptr;
    cudaError_t status;

    Buffers() {
        status = cudaMalloc(&inputs, MaxBatch * InputPolys * N * sizeof(int));
        if (status != cudaSuccess) return;
        status = cudaMalloc(&outputs, MaxBatch * Outputs * N * sizeof(int));
        if (status != cudaSuccess) return;
        status = cudaMalloc(&zetas, N * sizeof(int));
        if (status != cudaSuccess) return;
        const auto roots = make_zetas(DILITHIUM, false);
        status = cudaMemcpy(zetas, roots.data(), N * sizeof(int), cudaMemcpyHostToDevice);
    }

    ~Buffers() {
        if (zetas) cudaFree(zetas);
        if (outputs) cudaFree(outputs);
        if (inputs) cudaFree(inputs);
    }
};
}

extern "C" int pqc_cuda_reconstruct(const uint32_t *input, uint32_t *output, int count) {
    static_assert(sizeof(int) == sizeof(uint32_t));
    if (count < 1 || count > MaxBatch) return cudaErrorInvalidValue;
    static std::mutex lock;
    std::lock_guard<std::mutex> guard(lock);
    static Buffers buffers;
    if (buffers.status != cudaSuccess) return buffers.status;
    cudaError_t status = cudaMemcpy(buffers.inputs, input, count * InputPolys * N * sizeof(int),
                                    cudaMemcpyHostToDevice);
    if (status != cudaSuccess) return status;
    forward_batch<<<count * ForwardPolys, N>>>(buffers.inputs, buffers.zetas);
    status = cudaGetLastError();
    if (status != cudaSuccess) return status;
    reconstruct_batch<<<count * Outputs, N>>>(buffers.inputs, buffers.outputs, buffers.zetas);
    status = cudaGetLastError();
    if (status != cudaSuccess) return status;
    return cudaMemcpy(output, buffers.outputs, count * Outputs * N * sizeof(int), cudaMemcpyDeviceToHost);
}
