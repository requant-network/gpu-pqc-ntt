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


QUEUE_PROFILES = ("smooth_1000", "smooth_10000", "burst_3200")
QUEUE_POLICIES = ("cpu", "ready", "wait250", "wait1000", "wait4000")
QUEUE_METRICS = ("groups", "cuda_calls", "service_us", "p50_us", "p95_us", "p99_us")


def queue_rows(path):
    log = path.read_text()
    assert "test result: ok. 1 passed; 0 failed" in log
    run = {}
    for line in log.splitlines():
        prefix, separator, data = line.partition("queue=")
        if not separator:
            continue
        assert prefix in ("", "test mixed::virtual_arrival_queue ... ")
        row = dict(field.split("=", maxsplit=1) for field in (separator + data).split())
        profile, share, policy = row["queue"], int(row["ml_dsa"]), row["policy"]
        key = (profile, share, policy)
        assert key not in run and int(row["n"]) == 1024
        assert 0 <= int(row["cuda_calls"]) <= int(row["groups"]) <= 1024
        assert (int(row["cuda_calls"]) == 0) == (policy == "cpu")
        assert 0 < float(row["p50_us"]) <= float(row["p95_us"]) <= float(row["p99_us"])
        run[key] = {name: float(row[name]) for name in QUEUE_METRICS}
    assert set(run) == {(p, s, t) for p in QUEUE_PROFILES for s in (25, 50) for t in QUEUE_POLICIES}
    return run


def summarize_queue():
    folder = Path(__file__).resolve().parent / "queue"
    runs = [queue_rows(folder / f"queue-{index}.log.txt") for index in range(1, 4)]
    records = []
    for profile in QUEUE_PROFILES:
        for share in (25, 50):
            for policy in QUEUE_POLICIES:
                samples = [run[profile, share, policy] for run in runs]
                stats = {name: round(statistics.median(sample[name] for sample in samples), 3)
                         for name in QUEUE_METRICS}
                records.append(dict(profile=profile, ml_dsa_percent=share, policy=policy, **stats))
    print(json.dumps(dict(arrivals_per_run=1024, processes=3, warm=records), indent=2))


def summarize_contention():
    folder = Path(__file__).resolve().parent / "queue" / "contention"
    idle, busy, load = [], [], []
    for index in range(1, 5):
        idle.append(queue_rows(folder / f"idle-{index}.log.txt"))
        busy.append(queue_rows(folder / f"busy-{index}.log.txt"))
        report = dict(field.split("=", maxsplit=1) for field in (folder / f"load-{index}.log.txt").read_text().split())
        assert int(report["load_batch"]) == 32 and int(report["calls"]) > 0 and float(report["seconds"]) > 0
        load.append(dict(calls=int(report["calls"]), seconds=float(report["seconds"]), digest=report["digest"]))
    assert len({run["digest"] for run in load}) == 1
    records = []
    for profile, share, policy in idle[0]:
        row = dict(profile=profile, ml_dsa_percent=share, policy=policy)
        for mode, runs in (("idle", idle), ("busy", busy)):
            stats = {name: round(statistics.median(run[profile, share, policy][name] for run in runs), 3)
                     for name in ("groups", "service_us", "p50_us", "p95_us")}
            row[mode] = stats
        records.append(row)
    print(json.dumps(dict(pairs=4, arrivals_per_run=1024, load=load, warm=records), indent=2))


if __name__ == "__main__":
    if sys.argv[1:] == ["--contention"]:
        summarize_contention()
    elif sys.argv[1:] == ["--queue"]:
        summarize_queue()
    elif sys.argv[1:] == ["--mixed"]:
        summarize_mixed()
    elif len(sys.argv) == 1:
        main()
    else:
        raise SystemExit("usage: summarize.py [--mixed | --queue | --contention]")
