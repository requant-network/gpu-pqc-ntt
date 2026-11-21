"""Check native wire compatibility and an in-place upgrade on the PQC lab chain."""

import argparse
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


def batch(producer, first, count):
    rows = [producer.receive() for _ in range(count)]
    assert [row["height"] for row in rows] == list(range(first, first + count))
    assert all(row["transfers"] == 80 for row in rows)
    return rows


def run(upgraded, legacy):
    with tempfile.TemporaryDirectory(prefix="requant-mixed-version-") as tmp:
        root = Path(tmp)
        work = root / "receiver"
        with process([upgraded, "produce", str(root / "producer")], **PIPES) as child:
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
                try:
                    rpc(port, "getblock", first_mixed[0]["tip"])
                except RuntimeError as exc:
                    assert str(exc) == "unknown block"
                else:
                    raise RuntimeError("legacy node served a block it cannot decode")
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

        with process([upgraded, "receive", str(work)], **PIPES) as child:
            receiver = Session(child)
            port = receiver.receive()["rpc"]
            assert rpc(port, "getinfo")["peers"] == 0
            assert rpc(port, "auditsupply") == expected_audit
            verify_blocks(port, funding + mixed + tail)
            receiver.stop()

    return {
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
        "upgraded_wire_matches": len(funding + mixed + tail),
        "upgraded_audit_after_reopen": expected_audit,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("upgraded", type=Path)
    parser.add_argument("legacy", type=Path)
    args = parser.parse_args()
    print(json.dumps(run(str(args.upgraded.resolve()), str(args.legacy.resolve())), indent=2))
