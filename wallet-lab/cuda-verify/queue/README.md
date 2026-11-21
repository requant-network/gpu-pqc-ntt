# Validation latency under synthetic arrivals

November 21, 2026. The native wallet lab measured every validation call
on the same Ryzen 5 5500 and RTX 5060 Ti used in the
[mixed-signature trial](../mixed/README.md). A virtual FIFO queue applied
those observed service times to reproducible arrival schedules. Each of
three fresh processes warmed CUDA, then replayed 1,024 wires per schedule,
signature mix and policy. Every result matched the expected transaction fee.

The schedules deliver one wire every 1 ms, one every 100 µs, or bursts of
32 at 10 ms intervals. The direct CPU path releases one verdict per call.
The mixed path can take up to 32 available wires and releases all verdicts
when the batch completes. It dispatches when full or after the first
pending wire's wait limit, subject to completion of the previous call.
Immediate dispatch uses a zero wait limit. The harness also tried 250 µs,
1 ms and 4 ms. The table gives the median modeled p95 arrival-to-verdict
latency across the three runs, in milliseconds.

| Arrivals | ML-DSA | CPU | Immediate | Wait 250 µs | Wait 1 ms | Wait 4 ms |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1,000/s, smooth | 25% | 0.163 | 0.134 | 0.383 | 1.179 | 4.338 |
| 1,000/s, smooth | 50% | 0.168 | 0.136 | 0.388 | 1.240 | 4.428 |
| 10,000/s, smooth | 25% | 0.163 | 0.133 | 0.477 | 1.699 | 4.972 |
| 10,000/s, smooth | 50% | 2.248 | 0.167 | 0.540 | 1.837 | 5.427 |
| 3,200/s, bursts of 32 | 25% | 2.261 | 2.006 | 2.005 | 1.975 | 2.006 |
| 3,200/s, bursts of 32 | 50% | 3.219 | 2.472 | 2.459 | 2.459 | 2.452 |

With 50% ML-DSA and 10,000 arrivals per second, CPU service averaged
102.12 µs per wire, just above the 100 µs arrival interval. The finite
replay accumulated a queue. Immediate dispatch averaged 89.44 µs of
service per wire and kept the modeled p95 at 0.167 ms. Waiting up to
4 ms reduced compute to 76.12 µs per wire and raised p95 to 5.427 ms.

At 50% ML-DSA, full bursts reduced p95 from 3.219 to 2.472 ms with
immediate dispatch. Median latency grew from 1.648 to 2.432 ms because
the mixed path released the whole batch at once. All wait policies saw
the same 32 wires ready at each burst, so differences across those runs
reflect measured execution variation.

Keep immediate dispatch as the latency baseline within these schedules.
The experiment uses a single serial validator, fixed arrivals, intact
transactions and measured warm service. The [contention trial](contention/README.md)
repeats those schedules with a second CUDA process. An actual node needs
measurements with its scheduler, traffic and cold initialization.
[Median percentiles and service times](results.json) · [Run 1](queue-1.log.txt) ·
[Run 2](queue-2.log.txt) · [Run 3](queue-3.log.txt).

## Reproduce

Use the [same build](../README.md#reproduce) and choose the intended device.
The measured `mixed_checks.rs` SHA-256 was
`e114172519a0605236789ebfcf9462400fbbdedc6a1dd2e1a8c7f95eae529387`.
From the native checkout, run the following command in three fresh
processes. Retain the outputs as `queue-1.log.txt` through `queue-3.log.txt`
and rebuild the result with `python3 summarize.py --queue` from the parent
directory.

```sh
cargo test --offline --locked --release -p requant-consensus --test pqc_wallet \
  mixed::virtual_arrival_queue -- --exact --nocapture
```
