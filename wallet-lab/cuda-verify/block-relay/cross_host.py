"""Run a Windows producer and a Linux PQC receiver across a private SSH link."""

import argparse
from contextlib import ExitStack, contextmanager
import hashlib
import json
from pathlib import Path, PurePosixPath
from queue import Queue
import re
import shlex
import socket
import subprocess
import tempfile
from threading import Thread
from time import perf_counter, sleep

from measure import PIPES, signatures
from run import Session, phase, process, rpc, verify_blocks, wait_for


def remote_output(ssh, command):
    return subprocess.check_output([*ssh[:-1], "-T", ssh[-1], shlex.join(command)],
                                   text=True, timeout=10).strip()


def stderr_lines(child, lines):
    for line in child.stderr:
        lines.put(line)
    lines.put(None)


@contextmanager
def producer_link(ssh, producer_port, remote_port=0):
    route = f"127.0.0.1:{remote_port}:127.0.0.1:{producer_port}"
    command = [*ssh[:-1], "-NT", "-o", "ExitOnForwardFailure=yes", "-R", route, ssh[-1]]
    with process(command, stderr=subprocess.PIPE, text=True) as child:
        lines = Queue()
        Thread(target=stderr_lines, args=(child, lines), daemon=True).start()
        if remote_port == 0:
            line = lines.get(timeout=10)
            match = re.search(r"Allocated port (\d+) for remote forward", line or "")
            if not match:
                raise RuntimeError(f"reverse forward not established: {line!r}")
            remote_port = int(match[1])
        deadline = perf_counter() + 8
        while perf_counter() < deadline and child.poll() is None:
            bound = remote_output(ssh, ["ss", "-Hlt", f"( sport = :{remote_port} )"])
            if bound:
                entries = bound.splitlines()
                if len(entries) != 1 or entries[0].split()[3] != f"127.0.0.1:{remote_port}":
                    raise RuntimeError("unexpected reverse listener binding")
                yield remote_port
                return
            sleep(0.04)
        raise RuntimeError("reverse listener unavailable")


@contextmanager
def rpc_link(ssh, remote_port):
    with socket.socket() as reserve:
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]
    route = f"127.0.0.1:{port}:127.0.0.1:{remote_port}"
    command = [*ssh[:-1], "-NT", "-o", "ExitOnForwardFailure=yes", "-L", route, ssh[-1]]
    with process(command) as child:
        deadline = perf_counter() + 8
        while perf_counter() < deadline and child.poll() is None:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                sleep(0.04)
        else:
            raise RuntimeError("RPC forward unavailable")
        yield port


def receiver_cmd(args, ssh, directory, mode, peer=None):
    observed = "" if mode == "reopen" else "cpu" if mode == "cpu" else "cuda"
    device = args.gpu_uuid if mode == "cuda" else ""
    env = ["env", f"LD_LIBRARY_PATH={args.lib_dir}", f"CUDA_VISIBLE_DEVICES={device}",
           f"REQUANT_PQC_BLOCK_LAB={observed}"]
    cmd = ["flock", "-w", "10", "/tmp/requant-new-studies-gpu.lock", *env,
           str(args.linux_bin), "receive", directory]
    if peer is not None:
        cmd.append(f"127.0.0.1:{peer}")
    return [*ssh[:-1], "-T", ssh[-1], shlex.join(cmd)]


def trial(args, ssh, directory, local, mode):
    receiver_log = local / "receiver.log"
    with process([args.windows_bin, "produce", local / "producer"], **PIPES) as child:
        producer = Session(child)
        ready = producer.receive()
        assert ready["ready"] == "produce"
        producer_port = ready["p2p"]
        with receiver_log.open("wb") as log:
            with ExitStack() as link:
                peer = link.enter_context(producer_link(ssh, producer_port))
                cmd = receiver_cmd(args, ssh, directory, mode, peer)
                with process(cmd, **PIPES, stderr=log) as remote:
                    receiver = Session(remote)
                    remote_info = receiver.receive()
                    assert remote_info["ready"] == "receive"
                    # RPC stays up while the separate node-to-node stream is interrupted.
                    with rpc_link(ssh, remote_info["rpc"]) as port:
                        wait_for(port, lambda info: info["peers"] == 1)
                        funding, _ = phase(producer, port, "fund", 4, 1, (0, 0, 0, 1))
                        online, online_time = phase(producer, port, "online", 30, 5, 80)
                        result = resume(ssh, producer, producer_port, peer, link,
                                        port, funding, online, online_time)
                        receiver.stop()
        producer.stop()
    data = signatures(receiver_log, mode)
    with process(receiver_cmd(args, ssh, directory, "reopen"), **PIPES,
                 stderr=subprocess.DEVNULL) as remote:
        session = Session(remote)
        with rpc_link(ssh, session.receive()["rpc"]) as port:
            info = rpc(port, "getinfo")
            assert info["peers"] == 0 and info["tip"] == result["audit"]["tip"]
            assert rpc(port, "auditsupply") == result["audit"]
            verify_blocks(port, result["blocks"])
        session.stop()
    return {"mode": mode, "receiver_signature_checks": data, "reopen_audit_match": True, **result}


def resume(ssh, producer, producer_port, peer, link, port, funding, online, online_time):
    # Replace the only native data route while retaining the receiver and its RPC tunnel.
    link.close()
    disconnected = wait_for(port, lambda info: info["peers"] == 0)
    assert disconnected["height"] == 34 and disconnected["tip"] == online[-1]["tip"]
    offline, offline_time = phase(producer, port, "offline", 12, 35, 80)
    stayed = rpc(port, "getinfo")
    assert stayed["peers"] == 0 and stayed["tip"] == online[-1]["tip"]
    start = perf_counter()
    with producer_link(ssh, producer_port, peer):
        caught_up = wait_for(port, lambda info: info["tip"] == offline[-1]["tip"])
        elapsed = perf_counter() - start
        assert caught_up["height"] == 46 and caught_up["peers"] == 1
        resumed, resumed_time = phase(producer, port, "resumed", 18, 47, 80)
        blocks = funding + online + offline + resumed
        audit = producer.receive()["audit"]
        assert audit["ok"] and audit["height"] == 64
        assert rpc(port, "auditsupply") == audit
        verify_blocks(port, blocks)
    return {"blocks": blocks, "audit": audit, "online": online_time, "offline": offline_time,
            "resumed": resumed_time, "receiver_held_height": stayed["height"],
            "catchup_seconds": elapsed, "catchup_height": caught_up["height"]}


def run(args):
    ssh = ["ssh", "-i", str(args.identity), "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", args.remote]
    campaign = remote_output(ssh, ["mktemp", "-d", str(args.remote_root / "pqc-reverse.XXXXXXXX")])
    assert PurePosixPath(campaign).parent == args.remote_root
    trials = []
    with tempfile.TemporaryDirectory(prefix="pqc-reverse-") as temp:
        root = Path(temp).resolve()
        for i, modes in enumerate((("cpu", "cuda", "fallback"), ("fallback", "cuda", "cpu")), 1):
            for mode in modes:
                local = root / f"{i}-{mode}"
                local.mkdir()
                directory = str(PurePosixPath(campaign) / local.name)
                result = trial(args, ssh, directory, local, mode)
                trials.append({"trial": i, **result})
                print(f"{i} {mode}: caught up 12 blocks in {result['catchup_seconds']:.3f} s; "
                      "all 64 wires matched after restart", flush=True)
    return {"windows_producer_sha256": hashlib.sha256(args.windows_bin.read_bytes()).hexdigest(), "trials": trials}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("windows_bin", type=lambda path: Path(path).resolve())
    parser.add_argument("remote")
    parser.add_argument("linux_bin", type=PurePosixPath)
    parser.add_argument("remote_root", type=PurePosixPath)
    parser.add_argument("lib_dir")
    parser.add_argument("gpu_uuid")
    parser.add_argument("identity", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.remote.startswith("-") or not args.linux_bin.is_absolute() or not args.remote_root.is_absolute():
        parser.error("expected an SSH destination and absolute Linux paths")
    report = run(args)
    with args.output.open("x", encoding="utf-8") as out:
        json.dump(report, out, indent=2)
        out.write("\n")
