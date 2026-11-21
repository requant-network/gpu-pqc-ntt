# Portable backup recovery under Linux memory limits

## Protocol fixed before measuring

Keep the versioned envelope, 64 MiB Argon2id workspace, three passes,
four lanes and all authentication checks. Make workspace reservation
fallible. Test allocation refusal during both export and recovery
under a 64 MiB process address-space limit. Compare the original
binary on one recovery at the same limit.

Generate three fresh laboratory wallets. After each refused recovery,
verify that the encrypted backup is byte-identical and that no
transaction file was created. Retry with a 96 MiB address-space limit
using the same backup and recovery code. Also refuse the wrong code
under that limit.

Test 64 and 96 MiB for the same backup under Linux cgroup memory and
swap limits in an offline container. Distinguish a controlled allocation
refusal from kernel OOM termination. Confirm the recovered transaction on
the isolated chain and require a byte-for-byte match to the donor
transaction in both successful recovery paths. Retain results and public
transaction digests. The temporary wallets and recovery codes leave no
files in the research repository.

## Results

| Recovery condition | Observation |
| --- | --- |
| Original binary, 64 MiB address-space limit | One recovery aborted with SIGABRT on the 64 MiB allocation. |
| Updated binary, 64 MiB address-space limit | All three export and three restore attempts returned an out-of-memory error through the fixture. There was no output file. |
| Updated binary, 96 MiB address-space limit | All three wallets recovered the original confirmed wire. Median recovery took 122.60 ms. Peak RSS was 68,224–68,352 KiB. |
| Updated binary, 64 MiB container | The kernel terminated the process in all three containers. Docker reported OOM directly. The backup survived and no signed file appeared. |
| Updated binary, 96 MiB container | All three isolated retries recovered the original wire, with a median recovery time of 121.95 ms. |

Recovery times cover opening the backup, key derivation and checking the
restored owner. They exclude process launch, signing and confirmation.
The address-space limit is virtual memory. The container limit addresses
memory charged to the cgroup. Both ran on the LAN Ryzen 5 5500 with Linux
cgroup v2. A fallible reservation handles an allocator refusal. The kernel
can still terminate a process when it starts touching the reserved memory.

The Rust fixture retained the 64 MiB Argon2id configuration and the
129-byte envelope. No failed run changed the backup or released a signed
transaction. Each successful retry produced the same 3,924-byte wire as
its donor, and confirmed it on the lab chain. All three wrong-code controls
under the 96 MiB process limit were refused. A separate compatibility run
restored three backups from the prior Windows executable on the updated
Linux binary and matched each sender wire. The legacy migration test,
18 transaction controls and 11 portable format and authentication controls
passed on the updated build.

[Memory trial](memory-limits/results.json) · [Windows compatibility](memory-limits/cross-host.json) ·
[Native regressions](memory-limits/regressions.log.txt) · [Environment and source pins](memory-limits/environment.txt).

The 96 MiB result describes the tested Linux runtime and fixture. Mobile
process memory, Windows account isolation and production wallet custody
still require their own checks.

## Reproduce

Build the current and original fixture following the
[portable backup recipe](README.md#reproduce). Keep the original binary
from research commit `04f902e356d3eba3f72c20621595e3a6b9b54d88`.
Run the Linux harness with cgroup v2, an installed Ubuntu 24.04 image, and
local access to Docker. The runner creates isolated ephemeral containers
and supplies recovery codes through stdin.

```sh
python3 -B memory_limits.py /absolute/path/to/updated/pqc_wallet \
  /absolute/path/to/original/pqc_wallet
```

Use the [Windows-to-Linux harness](README.md#reproduce) with the
original Windows binary and updated Linux binary for the format control.
The environment file hashes the protocol section as it was before the run.
