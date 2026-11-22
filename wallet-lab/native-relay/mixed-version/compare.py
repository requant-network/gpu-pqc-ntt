"""Check native wire compatibility and an in-place upgrade on the PQC lab chain."""

import argparse
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cross-host"))
from run import Session, phase, process, remote_producer, rpc, verify_blocks, wait_for


PIPES = dict(stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)


def batch(producer, first, count):
    rows = [producer.receive() for _ in range(count)]
    assert [row["height"] for row in rows] == list(range(first, first + count))
    assert all(row["transfers"] == 80 for row in rows)
    return rows


def expect_block_error(port, block, message):
    try:
        rpc(port, "getblock", block)
    except RuntimeError as exc:
        assert str(exc) == message
        return str(exc)
    raise AssertionError("legacy node served a block it cannot decode")


def run(upgraded, legacy, remote=None):
    with tempfile.TemporaryDirectory(prefix="requant-mixed-version-") as tmp:
        root = Path(tmp)
        work = root / "receiver"
        with ExitStack() as stack:
            if remote:
                producer, tunnel, producer_p2p = stack.enter_context(remote_producer(*remote))
                stack.enter_context(process(tunnel))
            else:
                child = stack.enter_context(
                    process([upgraded, "produce", str(root / "producer")], **PIPES)
                )
                producer = Session(child)
                ready = producer.receive()
                assert ready["ready"] == "produce"
                producer_p2p = f"127.0.0.1:{ready['p2p']}"

            peer = [legacy, str(work), producer_p2p]
            stderr_path = root / "baseline-peer.log"
            with stderr_path.open("wb") as err, process(peer, stderr=err, **PIPES) as child:
                observer = Session(child)
                ready = observer.receive()
                assert ready["ready"] == "legacy"
                port = ready["rpc"]
                wait_for(port, lambda info: info["peers"] == 1)
                funding, _ = phase(producer, port, "fund", 4, 1, (0, 0, 0, 1))
                prefix_audit = rpc(port, "auditsupply")
                verify_blocks(port, funding)

                producer.send("online")
                first_mixed = batch(producer, 5, 1)
                headers = wait_for(port, lambda info: info["headers"] >= 5)
                assert headers["height"] == 4 and headers["tip"] == funding[-1]["tip"]
                mixed = first_mixed + batch(producer, 6, 29)
                before_upgrade = rpc(port, "getinfo")
                assert before_upgrade["headers"] >= 5 and before_upgrade["height"] == 4
                assert rpc(port, "auditsupply") == prefix_audit
                expect_block_error(port, first_mixed[0]["tip"], "unknown block")
                observer.stop()

            legacy_log = stderr_path.read_text(encoding="utf-8")
            refused_wires = sum("malformed block" in line for line in legacy_log.splitlines())
            start = perf_counter()
            with process([upgraded, "receive", str(work), producer_p2p], **PIPES) as child:
                receiver = Session(child)
                ready = receiver.receive()
                assert ready["ready"] == "receive"
                port = ready["rpc"]
                catchup = wait_for(port, lambda info: info["tip"] == mixed[-1]["tip"])
                assert catchup["height"] == 34
                catchup_ms = round((perf_counter() - start) * 1000, 3)
                verify_blocks(port, funding + mixed)
                producer.send("offline")
                tail = batch(producer, 35, 12)
                producer.send("resumed")
                tail += batch(producer, 47, 18)
                expected_audit = producer.receive()["audit"]
                assert expected_audit["ok"] and expected_audit["height"] == 64
                wait_for(port, lambda info: info["tip"] == expected_audit["tip"])
                assert rpc(port, "auditsupply") == expected_audit
                verify_blocks(port, funding + mixed + tail)
                receiver.stop()
                producer.stop()

        # Read a copy of the block store from genesis without the newer snapshot.
        replay = root / "legacy-replay" / "pqc-lab"
        replay.mkdir(parents=True)
        shutil.copyfile(work / "pqc-lab" / "blocks.dat", replay / "blocks.dat")
        with process([legacy, str(replay.parent)], stderr=subprocess.DEVNULL, **PIPES) as child:
            observer = Session(child)
            port = observer.receive()["rpc"]
            old_replay = rpc(port, "getinfo")
            assert old_replay["height"] == 4 and old_replay["peers"] == 0
            assert rpc(port, "auditsupply") == prefix_audit
            verify_blocks(port, funding)
            observer.stop()

        # Probe rollback on a separate copy containing the upgraded snapshot.
        rollback = root / "rollback"
        shutil.copytree(work, rollback)
        files = [rollback / "pqc-lab" / name for name in ("blocks.dat", "chainstate.bin")]
        before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
        with process([legacy, str(rollback)], **PIPES) as child:
            observer = Session(child)
            port = observer.receive()["rpc"]
            old_snapshot = rpc(port, "getinfo")
            assert old_snapshot["peers"] == 0
            assert old_snapshot["height"] == old_snapshot["snapshot_height"] == 64
            old_audit = rpc(port, "auditsupply")
            assert old_audit == expected_audit
            verify_blocks(port, funding)
            unreadable = {
                row["height"]: expect_block_error(port, row["tip"], "block body not stored")
                for row in (mixed[0], tail[-1])
            }
            observer.stop()
        changed = [
            path.name for path in files
            if hashlib.sha256(path.read_bytes()).hexdigest() != before[path]
        ]
        assert "blocks.dat" not in changed

        manifest = funding + mixed + tail
        for directory in (work, rollback):
            with process([upgraded, "receive", str(directory)], **PIPES) as child:
                receiver = Session(child)
                port = receiver.receive()["rpc"]
                assert rpc(port, "getinfo")["peers"] == 0
                assert rpc(port, "auditsupply") == expected_audit
                verify_blocks(port, manifest)
                receiver.stop()

    return {
        "transport": "p2p_tcp_over_ssh_forward" if remote else "p2p_tcp_loopback",
        "legacy_accepted_prefix_height": 4,
        "mixed_block_height": first_mixed[0]["height"],
        "first_mixed_block_sha256": first_mixed[0]["block_sha256"],
        "legacy_headers_height_before_upgrade": before_upgrade["headers"],
        "legacy_peer_log_malformed_block_messages": refused_wires,
        "legacy_audit_unchanged_after_mixed_blocks": prefix_audit,
        "upgrade_bootstrap_height": catchup["snapshot_height"],
        "upgrade_matched_tip_height": catchup["height"],
        "upgrade_elapsed_ms": catchup_ms,
        "legacy_raw_store_replay_height": old_replay["height"],
        "legacy_full_store_rollback": {
            "reported_height": old_snapshot["height"],
            "restored_snapshot_height": old_snapshot["snapshot_height"],
            "audit": old_audit,
            "getblock_errors": unreadable,
            "changed_store_files": changed,
            "upgraded_wire_matches_after_reopen": len(manifest),
        },
        "upgraded_wire_matches": len(manifest),
        "upgraded_audit_after_reopen": expected_audit,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("upgraded", type=Path)
    parser.add_argument("legacy", type=Path)
    parser.add_argument("--remote", nargs=4, metavar=("HOST", "BINARY", "SCRATCH", "IDENTITY"))
    args = parser.parse_args()
    remote = None
    if args.remote:
        host, binary, scratch, identity = args.remote
        binary, scratch = PurePosixPath(binary), PurePosixPath(scratch)
        if host.startswith("-") or not (binary.is_absolute() and scratch.is_absolute()):
            parser.error("expected an SSH destination and absolute Linux paths")
        remote = (host, binary, scratch, Path(identity))
    print(json.dumps(run(str(args.upgraded.resolve()), str(args.legacy.resolve()), remote), indent=2))
