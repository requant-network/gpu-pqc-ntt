"""Exercise two native lab nodes on different hosts via an SSH TCP forward."""

import argparse
from contextlib import contextmanager
import hashlib
import http.client
import json
from pathlib import Path, PurePosixPath
from queue import Queue
import shlex
import socket
import subprocess
import tempfile
from threading import Thread
from time import perf_counter, sleep


@contextmanager
def process(argv, **kwargs):
    with subprocess.Popen(argv, **kwargs) as child:
        try:
            yield child
        finally:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)


class Session:
    def __init__(self, child):
        self.child = child
        self.lines = Queue()
        Thread(target=self.read, daemon=True).start()

    def read(self):
        for line in self.child.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def send(self, command):
        self.child.stdin.write(command + "\n")
        self.child.stdin.flush()

    def receive(self):
        line = self.lines.get(timeout=20)
        if line is None:
            raise RuntimeError("native fixture exited before reporting the expected event")
        return json.loads(line)

    def stop(self):
        self.send("stop")
        if self.child.wait(timeout=10) != 0:
            raise RuntimeError("native fixture failed during shutdown")


def rpc(port, method, *params):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        body = json.dumps({"method": method, "params": params})
        connection.request("POST", "/", body)
        result = json.load(connection.getresponse())
        if result["error"] is not None:
            raise RuntimeError(result["error"])
        return result["result"]
    finally:
        connection.close()


def wait_for(port, condition):
    deadline = perf_counter() + 20
    while perf_counter() < deadline:
        info = rpc(port, "getinfo")
        assert info["network"] == "pqc-lab"
        if condition(info):
            return info
        sleep(0.02)
    raise TimeoutError("receiver failed to reach the expected height or peer state")


def phase(producer, rpc_port, label, count, first_height, transfer_count):
    started = perf_counter()
    producer.send(label)
    rows = []
    observed_lag = []
    for index in range(count):
        row = producer.receive()
        assert row["height"] == first_height + index
        if isinstance(transfer_count, tuple):
            assert row["transfers"] == transfer_count[index]
        else:
            assert row["transfers"] == transfer_count
        assert row["peers"] == int(label != "offline")
        rows.append(row)
        if index % 5 == 4 and label != "offline":
            receiver = rpc(rpc_port, "getinfo")
            observed_lag.append(row["height"] - receiver["height"])

    timing = {"producer_ms": round((perf_counter() - started) * 1000, 3)}
    if label != "offline":
        wait_for(rpc_port, lambda info: info["tip"] == rows[-1]["tip"])
        if observed_lag:
            timing["sampled_max_lag_blocks"] = max(observed_lag)
    return rows, timing


def verify_blocks(port, manifest):
    for row in manifest:
        block = rpc(port, "getblock", row["height"])
        wire = bytes.fromhex(block["hex"])
        assert block["best_chain"] and block["id"] == row["tip"]
        assert block["txs"] == row["transfers"] + 1
        assert len(wire) == row["block_bytes"]
        assert hashlib.sha256(wire).hexdigest() == row["block_sha256"]


@contextmanager
def remote_producer(remote, linux_bin, remote_root, identity):
    ssh = [
        "ssh", "-i", str(identity), "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=yes",
    ]
    command = shlex.join([
        "mktemp", "-d", "--", str(remote_root / "relay-run.XXXXXXXX")
    ])
    linux_dir = subprocess.check_output(
        [*ssh, "-T", remote, command], text=True, timeout=10
    ).strip()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        forwarded = sock.getsockname()[1]

    pipes = dict(stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
    linux_cmd = shlex.join([str(linux_bin), "produce", linux_dir])
    with process([*ssh, "-T", remote, linux_cmd], **pipes) as linux:
        producer = Session(linux)
        ready = producer.receive()
        assert ready["ready"] == "produce"
        tunnel = [
            *ssh, "-NT", "-o", "ExitOnForwardFailure=yes", "-L",
            f"127.0.0.1:{forwarded}:127.0.0.1:{ready['p2p']}", remote,
        ]
        yield producer, tunnel, f"127.0.0.1:{forwarded}"


def run(args):
    pipes = dict(stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
    with tempfile.TemporaryDirectory(prefix="requant-relay-") as windows_dir:
        with remote_producer(args.remote, args.linux_bin, args.remote_root, args.identity) as link:
            producer, tunnel, peer = link
            windows = [str(args.windows_bin), "receive", windows_dir]
            with process([*windows, peer], **pipes) as local:
                receiver = Session(local)
                ready = receiver.receive()
                assert ready["ready"] == "receive"
                port = ready["rpc"]

                with process(tunnel):
                    wait_for(port, lambda info: info["peers"] == 1)
                    funding, _ = phase(producer, port, "fund", 4, 1, (0, 0, 0, 1))
                    online, online_timing = phase(producer, port, "online", 30, 5, 80)

                disconnected = wait_for(port, lambda info: info["peers"] == 0)
                assert disconnected["height"] == 34 and disconnected["tip"] == online[-1]["tip"]
                offline, offline_timing = phase(producer, port, "offline", 12, 35, 80)
                stayed = rpc(port, "getinfo")
                assert stayed["tip"] == disconnected["tip"] and stayed["peers"] == 0

                started = perf_counter()
                with process(tunnel):
                    recovered = wait_for(port, lambda info: info["tip"] == offline[-1]["tip"])
                    assert recovered["height"] == 46 and recovered["peers"] == 1
                    reconnect_ms = round((perf_counter() - started) * 1000, 3)
                    resumed, resumed_timing = phase(producer, port, "resumed", 18, 47, 80)
                    manifest = funding + online + offline + resumed
                    audit = producer.receive()["audit"]
                    assert audit["ok"] and audit["height"] == 64
                    assert rpc(port, "auditsupply") == audit
                    verify_blocks(port, manifest)
                    receiver.stop()
                    producer.stop()

        with process(windows, **pipes) as local:
            receiver = Session(local)
            reopened_port = receiver.receive()["rpc"]
            reopened = rpc(reopened_port, "getinfo")
            assert reopened["peers"] == 0 and reopened["tip"] == audit["tip"]
            assert rpc(reopened_port, "auditsupply") == audit
            verify_blocks(reopened_port, manifest)
            receiver.stop()

    return {
        "transport": "p2p_tcp_over_ssh_forward",
        "load_blocks": 60,
        "ml_dsa_spends": 60 * 64,
        "ed25519_spends": 60 * 16,
        "load_block_bytes": sum(row["block_bytes"] for row in manifest[4:]),
        "online": online_timing,
        "offline": {**offline_timing, "receiver_held_height": disconnected["height"]},
        "reconnected": {"height": recovered["height"], "elapsed_ms": reconnect_ms},
        "resumed": resumed_timing,
        "latest_utxo_audit": audit,
        "matched_block_wires_live_and_reopened": len(manifest),
        "blocks": manifest,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("windows_bin", type=Path)
    parser.add_argument("remote")
    parser.add_argument("linux_bin", type=PurePosixPath)
    parser.add_argument("remote_root", type=PurePosixPath)
    parser.add_argument("identity", type=Path)
    args = parser.parse_args()
    if args.remote.startswith("-") or not args.linux_bin.is_absolute() or not args.remote_root.is_absolute():
        parser.error("expected an SSH destination and absolute Linux paths")
    print(json.dumps(run(args), indent=2))
