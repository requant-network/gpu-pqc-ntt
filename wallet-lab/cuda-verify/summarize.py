"""Rebuild the paired timing summary from the three retained wallet logs."""
from pathlib import Path
import collections
import json
import re
import statistics
import sys


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


def summarize_mixed():
    folder = Path(__file__).resolve().parent / "mixed"
    samples = collections.defaultdict(dict)
    cohorts = {"ed_only": 0, "pq25": 25, "pq50": 50, "pq50_dirty": 50, "pq100": 100}
    pattern = re.compile(
        r"mixed=(\S+) batch=(\d+) pass=(\d+) n=(\d+) rejected=(\d+) "
        r"cuda_calls=(\d+) cpu_us=([0-9.]+) hybrid_us=([0-9.]+)"
    )
    for index in range(1, 4):
        log = (folder / f"mixed-{index}.log.txt").read_text()
        assert "test result: ok. 1 passed; 0 failed" in log
        assert "backend=Cuda cases=9 rotations=9" in log
        for cohort, batch, trial, count, rejected, calls, cpu, hybrid in pattern.findall(log):
            batch, trial, count, rejected, calls = map(int, (batch, trial, count, rejected, calls))
            assert cohort in cohorts and batch in (8, 32, 128)
            assert count == 256 and 0 <= trial < 6
            assert calls == (count // batch if cohorts[cohort] else 0)
            assert rejected == (16 if cohort == "pq50_dirty" else 0)
            key = (index, trial)
            assert key not in samples[cohort, batch]
            samples[cohort, batch][key] = float(cpu), float(hybrid)

    records = []
    for cohort in cohorts:
        for batch in (8, 32, 128):
            values = samples[cohort, batch].values()
            assert len(values) == 18
            cpu, hybrid = map(statistics.median, zip(*values))
            records.append(dict(
                cohort=cohort, ml_dsa_percent=cohorts[cohort], batch=batch, passes=len(values),
                cpu_us=round(cpu, 3), hybrid_us=round(hybrid, 3), speedup=round(cpu / hybrid, 3),
            ))
    print(json.dumps(dict(wires_per_pass=256, compared_verdicts=256 * 18 * len(records), warm=records), indent=2))


if __name__ == "__main__":
    if sys.argv[1:] == ["--mixed"]:
        summarize_mixed()
    elif len(sys.argv) == 1:
        main()
    else:
        raise SystemExit("usage: summarize.py [--mixed]")
