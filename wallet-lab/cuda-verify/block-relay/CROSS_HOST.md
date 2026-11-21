# Windows producer and Linux CUDA receiver

The post-quantum wallet lab sends mixed native blocks from an
independent Windows x86-64 producer to a Linux x86-64 receiver.
The receiver uses the [lab block-signature selector](README.md)
on the actual P2P acceptance path.
The earlier [cross-host experiment](../../native-relay/cross-host/README.md)
used Linux as the producer and Windows as the CPU receiver.

## Protocol

Run CPU, CUDA and CUDA with the device hidden, then repeat the
three modes in reverse order. Each trial starts a fresh
disposable chain on Windows and a fresh Linux receiver on the
Ryzen 5 5500 server. The CUDA path exposes the server's RTX 5060 Ti.
The Windows Ryzen 7 8745HS signs and serves the same mix of 64
ML-DSA and 16 Ed25519 transfers per load block in every mode.

The Windows orchestrator uses two authenticated SSH connections.
A reverse forward exposes the Windows producer's P2P listener to
the Linux receiver. The initial port is allocated by SSH, and
the runner verifies the server-side listener binds only to IPv4
loopback. A separate forward gives the orchestrator access to the
Linux node's loopback RPC for observations.

1. Sync the four funding-prefix blocks and 30 load blocks over native P2P.
2. Stop the reverse forward and observe that the Linux peer session closes.
   Keep the Linux process and RPC running. Produce 12 more load blocks and
   verify that the Linux node still reports the exact height-34 tip.
3. Restore the reverse forward at the same port. Wait for the Linux receiver
   to reach the producer's exact height-46 tip, then produce 18 more blocks.
4. Compare all 64 encoded blocks and the supply/UTXO audit. Restart the
   receiver in its ordinary CPU mode with no peers and repeat the comparisons.

The catch-up metric includes starting the replacement SSH forward,
verifying the server-side bind, peer reconnection, data transfer,
native acceptance and RPC observation. Per-block signature timings
use the Linux receiver's monotonic clock and cover the signature
phase inside native acceptance. The receiver's CPU mode and CUDA
mode use the same Linux binary.

## Results

| Linux receiver | Warm block signature time, trial medians | Restore link and catch up 12 blocks |
| --- | ---: | ---: |
| CPU | 10.444–10.446 ms | 0.630–0.912 s |
| CUDA | 7.497–7.498 ms | 0.627–0.644 s |
| Device hidden | 10.449–10.472 ms | 0.631–0.770 s |

Each run carried 4,800 mixed load transfers in 15,255,280 encoded
block bytes. All six receivers stayed at the exact height-34 tip
through the partition, caught up to height 46, and matched all
64 wires and the UTXO supply audit at height 64. The same checks
passed after reopening each store. Sampled height lag in the two
connected windows reached one block in all six trials.

Each CUDA receiver checked 3,840 ML-DSA signatures on the GPU.
Warm signature verification took about 28% less time per block
than the CPU path. CUDA's first mixed block spent 170–175 ms in
signature verification. With the device hidden, the receiver
attempted CUDA once and checked the remaining 59 mixed blocks
directly on CPU, including the blocks received after reconnecting.
The catch-up ranges overlap for these two runs per mode.
[Encoded block digests, timing samples and audits](cross-host/report.json) ·
[Binary and toolchain pins](cross-host/environment.txt)

## Reproduce

Build the Windows CPU `relay_lab.exe` from the
[cross-host setup](../../native-relay/cross-host/README.md#reproduce)
and build the Linux receiver from the
[CUDA block lab setup](README.md#reproduce).
Use an existing trusted SSH route with reverse and local forwarding
enabled. Point the runner at the installed CUDA verification library
directory and CUDA runtime library directory on Linux. The Linux
receiver takes the shared lab GPU lock automatically. The paths
below are argument placeholders.

```powershell
python -B wallet-lab/cuda-verify/block-relay/cross_host.py `
    WINDOWS_RELAY_EXE USER@HOST /LINUX/RELAY_BIN /LINUX/SCRATCH `
    '/CUDA/LAB:/CUDA/RUNTIME' GPU_UUID /PATH/TO/SSH_IDENTITY new-cross-host.json
```

The runner keeps one Linux experiment directory with the six
disposable receiver stores. The Windows producer stores and local
logs use a temporary directory.
