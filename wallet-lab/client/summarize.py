"""Rebuild the Windows client timing summary from the five wallet logs."""
from pathlib import Path
import json
import re
import statistics


here = Path(__file__).resolve().parent
schemes = {"ed25519": 179, "ml_dsa_44": 3924}
samples = {scheme: {} for scheme in schemes}
for process in range(1, 6):
    log = (here / f"wallet-{process}.log.txt").read_text()
    assert "test result: ok. 1 passed; 0 failed" in log
    assert "native_migration=accepted restored_spend=accepted same_block_legacy=accepted negative_controls=18" in log
    size = re.search(r"native_size_bytes ed25519=(\d+) ml_dsa_44=(\d+)", log)
    assert tuple(map(int, size.groups())) == tuple(schemes.values())
    for scheme, trial, count, sign, check in re.findall(
        r"bench scheme=(\w+) trial=(\d+) n=(\d+) sign_encode_us=([\d.]+) decode_validate_us=([\d.]+)", log
    ):
        key = (process, int(trial))
        assert 0 <= key[1] < 5 and int(count) == 128 and key not in samples[scheme]
        samples[scheme][key] = float(sign), float(check)

result = {}
for scheme, rows in samples.items():
    assert len(rows) == 25
    sign, check = zip(*rows.values())
    result[scheme] = dict(
        bytes=schemes[scheme], trials=len(rows), transactions=len(rows) * 128,
        sign_encode_us=round(statistics.median(sign), 3),
        sign_trial_range_us=(min(sign), max(sign)),
        decode_validate_us=round(statistics.median(check), 3),
    )
print(json.dumps(result, indent=2))
