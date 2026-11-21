"""Compare three signature paths on the same native block relay."""

import argparse
import json
import os
from pathlib import Path
from statistics import median
import subprocess
import sys
import tempfile
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "native-relay/cross-host"))
from run import Session, process, rpc, verify_blocks, wait_for


PIPES = dict(stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)


def fixture(conn):
    rows = []
    for command, count in (("fund", 4), ("online", 30), ("offline", 12), ("resumed", 18)):
        conn.send(command)
        for _ in range(count):
            row = conn.receive()
            assert row["height"] == len(rows) + 1
            assert row["transfers"] == (0 if row["height"] < 4 else 1 if row["height"] == 4 else 80)
            assert row["peers"] == 0
            rows.append(row)
    audit = conn.receive()["audit"]
    assert len(rows) == audit["height"] == 64 and audit["ok"] and audit["tip"] == rows[-1]["tip"]
    return rows, audit


def signatures(log, mode):
    rows = []
    for line in log.read_text(encoding="utf-8").splitlines():
        if line.startswith("PQC_BLOCK_SIGS "):
            attrs = dict(part.split("=", 1) for part in line.split()[1:])
            assert attrs.pop("signatures_ok") == "true"
            rows.append({"backend": attrs.pop("backend"), **{name: int(value) for name, value in attrs.items()}})
    assert [r["height"] for r in rows] == list(range(1, 65)), (mode, rows)
    for row in rows:
        if mode == "cpu":
            expected, count = "cpu", 0
        elif row["height"] <= 4:
            expected, count = "cpu-no-pq", 0
        elif mode == "cuda":
            expected, count = "cuda", 64
        elif mode == "retry" or row["height"] == 5:
            expected, count = "cpu-fallback", 0
        else:
            expected, count = "cpu-unavailable", 0
        assert (row["backend"], row["gpu_verified"]) == (expected, count), (mode, row)
    return rows


def sync(binary, directory, peer, mode, env, blocks, audit):
    settings = {**env, "REQUANT_PQC_BLOCK_LAB": "cpu" if mode == "cpu" else "cuda"}
    if mode in ("fallback", "retry"):
        settings["CUDA_VISIBLE_DEVICES"] = ""
    log = directory.with_suffix(".log")
    with log.open("wb") as stderr:
        start = perf_counter()
        with process([binary, "receive", directory, peer], **PIPES, env=settings, stderr=stderr) as child:
            receiver = Session(child)
            ready = receiver.receive()
            assert ready["ready"] == "receive"
            port = ready["rpc"]
            catchup = wait_for(port, lambda info: info["tip"] == audit["tip"])
            elapsed = perf_counter() - start
            assert catchup["height"] == 64 and catchup["peers"] == 1
            assert rpc(port, "auditsupply") == audit
            verify_blocks(port, blocks)
            receiver.stop()
    rows = signatures(log, mode)
    # Reopen using the standard CPU path and verify the persisted native state.
    with process([binary, "receive", directory], **PIPES, env=env, stderr=subprocess.DEVNULL) as child:
        reopened = Session(child)
        port = reopened.receive()["rpc"]
        assert rpc(port, "auditsupply") == audit
        verify_blocks(port, blocks)
        reopened.stop()
    return {"mode": mode, "sync_seconds": elapsed, "signature_checks": rows,
            "first_mixed_signature_ms": rows[4]["elapsed_us"] / 1_000,
            "warm_block_signature_ms": median(row["elapsed_us"] for row in rows[5:]) / 1_000,
            "encoded_blocks_verified": len(blocks), "reopen_audit_match": True}


def run(binary, retry_binary):
    env = dict(os.environ)
    env.pop("REQUANT_PQC_BLOCK_LAB", None)
    with tempfile.TemporaryDirectory(prefix="requant-pqc-gpu-relay-") as tmp:
        root = Path(tmp)
        source = root / "producer"
        with process([binary, "produce", source], **PIPES, env=env, stderr=subprocess.DEVNULL) as child:
            producer = Session(child)
            assert producer.receive()["ready"] == "produce"
            blocks, audit = fixture(producer)
            producer.stop()
        with process([binary, "receive", source], **PIPES, env=env, stderr=subprocess.DEVNULL) as child:
            server = Session(child)
            ready = server.receive()
            assert rpc(ready["rpc"], "auditsupply") == audit
            peer = f"127.0.0.1:{ready['p2p']}"
            trials = []
            modes = ("cpu", "cuda", "fallback", "retry") if retry_binary else ("cpu", "cuda", "fallback")
            for i in range(len(modes)):
                for mode in modes[i:] + modes[:i]:
                    child = retry_binary if mode == "retry" else binary
                    row = sync(child, root / f"{i}-{mode}", peer, mode, env, blocks, audit)
                    trials.append({"trial": i + 1, **row})
                    print(f"{i + 1} {mode}: reached height 64 in {row['sync_seconds']:.3f} s, "
                          f"warm signatures {row['warm_block_signature_ms']:.3f} ms/block", flush=True)
            server.stop()
    return {"blocks": blocks, "audit": audit, "trials": trials}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=lambda path: Path(path).resolve())
    parser.add_argument("output", type=Path)
    parser.add_argument("--retry-binary", type=lambda path: Path(path).resolve())
    args = parser.parse_args()
    report = run(args.binary, args.retry_binary)
    with args.output.open("x", encoding="utf-8") as out:
        json.dump(report, out, indent=2)
        out.write("\n")
