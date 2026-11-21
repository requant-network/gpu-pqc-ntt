"""Rebuild the paired timing summary from the three retained wallet logs."""
from pathlib import Path
import collections
import json
import re
import statistics


def main():
    samples = collections.defaultdict(list)
    first_calls = []
    here = Path(__file__).resolve().parent
    for index in range(1, 4):
        log = (here / f"wallet-{index}.log.txt").read_text()
        first_calls.append(float(re.search(r"cuda_first_call_ms=([0-9.]+)", log)[1]))
        for cohort, batch, trial, count, cpu, gpu in re.findall(
            r"cohort=(\S+) cuda_batch=(\d+) pass=(\d+) n=(\d+) cpu_us=([0-9.]+) gpu_us=([0-9.]+)",
            log,
        ):
            assert int(count) == 256 and int(trial) < 6
            samples[cohort, int(batch)].append((float(cpu), float(gpu)))
    assert len(samples) == 8 and len(first_calls) == 3
    records = []
    for (cohort, batch), values in sorted(samples.items()):
        assert len(values) == 18
        cpu, gpu = map(statistics.median, zip(*values))
        records.append(dict(
            cohort=cohort, batch=batch, passes=len(values), cpu_us=round(cpu, 3), cuda_us=round(gpu, 3),
            speedup=round(cpu / gpu, 3), cpu_tx_per_s=round(1e6 / cpu), cuda_tx_per_s=round(1e6 / gpu),
        ))
    print(json.dumps(dict(
        rust="1.98.1", cuda="13.3.73", gpu="GeForce RTX 5060 Ti", cpu="Ryzen 5 5500",
        cold_first_call_ms=first_calls, warm=records,
    ), indent=2))


if __name__ == "__main__":
    main()
