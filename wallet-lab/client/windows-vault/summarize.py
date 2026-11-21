"""Build the Windows DPAPI fixture summary from the three retained runs."""
import json
from pathlib import Path
import re
from statistics import median


here = Path(__file__).resolve().parent
controls = {"bit-flip", "truncated", "wrong-format", "oversize", "wrong-purpose", "other-wallet", "missing"}
samples = []
sealed_sizes = []
for run in range(1, 4):
    log = (here / f"vault-{run}.log.txt").read_text()
    assert "test result: ok. 1 passed; 0 failed" in log
    assert "wallet_restarts=5 same_signed_bytes=true protected_copy=true no_overwrite=true confirmed=true altered_payment_refused=true" in log
    observed = re.findall(r"^refused=(\S+) signed_output=false$", log, re.MULTILINE)
    assert len(observed) == len(controls) and set(observed) == controls
    sealed_sizes.append(int(re.search(r"^sealed_bytes=(\d+)$", log, re.MULTILINE)[1]))
    rows = re.findall(r"trial=(\d+) wallet_open_us=([\d.]+) sign_encode_us=([\d.]+) "
                      r"process_ms=([\d.]+) wire_bytes=3924", log)
    assert [int(row[0]) for row in rows] == list(range(1, 6))
    samples.extend(tuple(map(float, row[1:])) for row in rows)

names = ("wallet_open_us", "sign_encode_us", "process_ms")
summary = {name: dict(median=median(values), minimum=min(values), maximum=max(values))
           for name, values in zip(names, zip(*samples), strict=True)}
print(json.dumps(dict(processes=len(samples), sealed_sizes=sealed_sizes, refused=len(controls) * 3,
                      wire_bytes=3924, timings=summary), indent=2))
