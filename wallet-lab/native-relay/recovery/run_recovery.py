"""Validate the two recovery paths on an isolated, synthetic ReQuant chain."""
import argparse
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cross-host"))
from run import Session, phase, process, rpc, verify_blocks, wait_for

PIPES = dict(stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)


@contextmanager
def node(argv, role):
    with process([str(arg) for arg in argv], **PIPES) as child:
        session = Session(child)
        ready = session.receive()
        assert ready["ready"] == role
        yield session, ready
        session.stop()


def inventory(root):
    files = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            with path.open("rb") as stream:
                checksum = hashlib.file_digest(stream, "sha256").hexdigest()
            files[path.relative_to(root).as_posix()] = {"bytes": path.stat().st_size, "sha256": checksum}
    return files


def copy_verified(source, target, expected):
    if inventory(source) != expected:
        raise ValueError("backup inventory mismatch")
    shutil.copytree(source, target)
    if inventory(target) != expected:
        raise ValueError("restored copy inventory mismatch")


def check_state(port, rows, audit):
    tip = wait_for(port, lambda info: info["tip"] == rows[-1]["tip"])
    assert tip["height"] == rows[-1]["height"]
    assert rpc(port, "auditsupply") == audit
    verify_blocks(port, rows)
    return tip


def run(upgraded, legacy):
    with tempfile.TemporaryDirectory(prefix="requant-safe-recovery-") as directory:
        root = Path(directory)
        backup, incident = root / "backup", root / "incident"
        case_copy, damaged = root / "case-copy", root / "damaged"
        restored, empty = root / "restored", root / "resync"
        with node([upgraded, "produce", root / "source"], "produce") as (producer, source):
            source_port = source["rpc"]
            peer = f"127.0.0.1:{source['p2p']}"
            with node([legacy, backup, peer], "legacy") as (_, ready):
                wait_for(ready["rpc"], lambda info: info["peers"] == 1)
                initial, _ = phase(producer, ready["rpc"], "fund", 4, 1, (0, 0, 0, 1))
                initial_audit = rpc(source_port, "auditsupply")
                check_state(ready["rpc"], initial, initial_audit)
            wait_for(source_port, lambda info: info["peers"] == 0)
            backup_files = inventory(backup)
            assert {"pqc-lab/blocks.dat", "pqc-lab/chainstate.bin"} <= backup_files.keys()

            copy_verified(backup, damaged, backup_files)
            with (damaged / "pqc-lab/blocks.dat").open("ab") as stream:
                stream.write(b"synthetic copy damage")
            try:
                copy_verified(damaged, restored, backup_files)
            except ValueError as error:
                assert str(error) == "backup inventory mismatch" and not restored.exists()
            else:
                raise AssertionError("the altered backup passed the restore gate")

            copy_verified(backup, incident, backup_files)
            with node([upgraded, "receive", incident, peer], "receive") as (_, ready):
                wait_for(ready["rpc"], lambda info: info["peers"] == 1)
                online, _ = phase(producer, ready["rpc"], "online", 30, 5, 80)
                check_state(ready["rpc"], initial + online, rpc(source_port, "auditsupply"))
            wait_for(source_port, lambda info: info["peers"] == 0)
            incident_files = inventory(incident)
            copy_verified(incident, case_copy, incident_files)

            offline, _ = phase(producer, source_port, "offline", 12, 35, 80)
            checkpoint_rows = initial + online + offline
            checkpoint_audit = rpc(source_port, "auditsupply")

            # The copied backup must pass an offline intake before connecting to a peer.
            copy_verified(backup, restored, backup_files)
            with node([upgraded, "receive", restored], "receive") as (_, ready):
                startup = check_state(ready["rpc"], initial, initial_audit)
                assert startup["peers"] == 0 and startup["snapshot_height"] == 4

            started = perf_counter()
            with node([upgraded, "receive", empty, peer], "receive") as (_, ready):
                tip = check_state(ready["rpc"], checkpoint_rows, checkpoint_audit)
                assert tip["snapshot_height"] is None and tip["peers"] == 1
                clean_ms = round((perf_counter() - started) * 1000, 3)
            wait_for(source_port, lambda info: info["peers"] == 0)
            with node([upgraded, "receive", empty], "receive") as (_, ready):
                tip = check_state(ready["rpc"], checkpoint_rows, checkpoint_audit)
                assert tip["snapshot_height"] == 46 and tip["peers"] == 0

            started = perf_counter()
            with node([upgraded, "receive", restored, peer], "receive") as (_, ready):
                tip = check_state(ready["rpc"], checkpoint_rows, checkpoint_audit)
                assert tip["snapshot_height"] == 4 and tip["peers"] == 1
                backup_ms = round((perf_counter() - started) * 1000, 3)
                tail, _ = phase(producer, ready["rpc"], "resumed", 18, 47, 80)
                rows = checkpoint_rows + tail
                audit = producer.receive()["audit"]
                assert audit["ok"] and audit["height"] == 64
                check_state(ready["rpc"], rows, audit)

        with node([upgraded, "receive", restored], "receive") as (_, ready):
            tip = check_state(ready["rpc"], rows, audit)
            assert tip["snapshot_height"] == 64 and tip["peers"] == 0

        assert inventory(backup) == backup_files
        assert inventory(incident) == inventory(case_copy) == incident_files
        return {
            "chain": "pqc-lab",
            "transport": "local_tcp",
            "backup": {"height": 4, "inventory": backup_files, "audit": initial_audit},
            "preserved_incident": {"height": 34, "inventory": incident_files},
            "altered_backup_refused_before_open": True,
            "checkpoint": {"height": 46, "audit": checkpoint_audit},
            "clean_sync": {"milliseconds_to_full_check": clean_ms, "wires_reopened": len(checkpoint_rows)},
            "from_backup": {"milliseconds_to_full_check": backup_ms, "wires_reopened": len(rows)},
            "latest": {"height": 64, "audit": audit, "blocks": rows},
        }


if __name__ == "__main__":
    if not __debug__:
        raise RuntimeError("enable assertions to run the recovery checks")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("upgraded", type=Path)
    parser.add_argument("legacy", type=Path)
    args = parser.parse_args()
    print(json.dumps(run(args.upgraded.resolve(strict=True), args.legacy.resolve(strict=True)), indent=2))
