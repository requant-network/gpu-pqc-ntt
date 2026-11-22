"""Check the signed Linux releases on an ordinary regtest backup."""

import argparse
from contextlib import contextmanager
from hashlib import sha256
import json
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from urllib.request import Request, urlopen


KEY = bytes.fromhex("48671c99652923367341d74ee8dbef4b861856c0bf54fbb08e6b8d9419dcb72c")
OLD, NEW = "0.18.2", "0.18.3"
NODE = "requantd-linux-x86_64"
GITHUB = "https://github.com/requant-network/requant/releases/download"
CHECKPOINT = 64
EXTENDED = 68


def digest(data):
    return sha256(data).hexdigest()


def signed_asset(root, version):
    folder = root / version
    manifest = (folder / f"release-{version}.txt").read_bytes()
    signature = bytes.fromhex((folder / f"release-{version}.sig").read_text(encoding="ascii").strip())
    lines = manifest.decode("ascii").splitlines()
    if len(signature) != 64 or lines[:2] != ["requant-release 1", f"version {version}"]:
        raise ValueError("unexpected release document")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp)
        key = path / "key.der"
        message = path / "message"
        sig = path / "signature"
        key.write_bytes(bytes.fromhex("302a300506032b6570032100") + KEY)
        sig.write_bytes(signature)

        def verify(text):
            message.write_bytes(sha256(b"requant/release" + text).digest())
            return subprocess.run([
                "openssl", "pkeyutl", "-verify", "-pubin", "-keyform", "DER",
                "-inkey", key, "-rawin", "-in", message, "-sigfile", sig,
            ], capture_output=True, check=False, timeout=10).returncode

        if verify(manifest) != 0 or verify(manifest + b"\n") != 1:
            raise ValueError("release signature or its refusal control failed")
    [asset] = [line.split() for line in lines[2:] if line.startswith("asset linux-x86_64 ")]
    _, _, expected, url = asset
    if url != f"{GITHUB}/v{version}/{NODE}" or digest((folder / NODE).read_bytes()) != expected:
        raise ValueError("download does not match the signed Linux asset")
    return {"version": version, "manifest_sha256": digest(manifest),
            "signature_sha256": digest(signature), "linux_sha256": expected,
            "modified_manifest_refused": True}


def rpc(port, method, *params):
    wire = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    request = Request(f"http://127.0.0.1:{port}/", data=wire, headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=2) as response:
        result = json.load(response)
    if result.get("error") is not None:
        raise ValueError(f"{method} refused: {result['error']}")
    return result["result"]


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def wait_for(operation):
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        value = operation()
        if value:
            return value
        time.sleep(0.025)
    raise TimeoutError("isolated regtest node missed its expected checkpoint")


@contextmanager
def node(binary, target, log, miner=None, peer=None):
    port, p2p = free_port(), free_port()
    while p2p == port:
        p2p = free_port()
    argv = [
        binary, "--network", "regtest", "--no-discover", "--datadir", target,
        "--rpc", f"127.0.0.1:{port}", "--listen", f"127.0.0.1:{p2p}", "--threads", "1",
    ]
    if miner:
        argv += ["--mine", miner, "--mine-interval-ms", "400"]
    if peer:
        argv += ["--connect", f"127.0.0.1:{peer}"]
    with log.open("ab") as messages:
        process = subprocess.Popen(argv, stdout=messages, stderr=messages)

        def started():
            if process.poll() is not None:
                raise RuntimeError(f"node exited before startup: {log.name}")
            try:
                return rpc(port, "getinfo")
            except OSError:
                return None

        try:
            wait_for(started)
            yield port, p2p
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
            try:
                status = process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
                raise
            if status != 0:
                raise RuntimeError(f"node shutdown failed: {log.name}")


def wallet(tool, port, *args):
    command = [tool, "--network", "regtest"]
    if port:
        command += ["--rpc", f"127.0.0.1:{port}"]
    result = subprocess.run(command + list(args), capture_output=True, text=True, timeout=12, check=False)
    if result.returncode:
        raise RuntimeError(f"disposable regtest wallet command failed: {args[0]}")
    return result.stdout


def block_set(port, version, height, txid, offline=False):
    info = rpc(port, "getinfo")
    if (info["network"], info["version"], info["height"]) != ("regtest", version, height):
        raise ValueError("unexpected release or checkpoint")
    if offline and (info["peers"] != 0 or info["snapshot_height"] != height):
        raise ValueError("expected an offline reopen from the exact snapshot")
    audit = rpc(port, "auditsupply")
    if audit["ok"] is not True or audit["tip"] != info["tip"]:
        raise ValueError("checkpoint audit did not match the active tip")
    bodies = []
    transfers = 0
    for n in range(1, height + 1):
        block = rpc(port, "getblock", n)
        if block["best_chain"] is not True or block["height"] != n:
            raise ValueError("expected the active block body at each height")
        bodies.append([block["id"], digest(bytes.fromhex(block["hex"]))])
        transfers += block["txids"].count(txid)
    if transfers != 1:
        raise ValueError("expected one confirmed synthetic wallet payment")
    return {"height": height, "tip": info["tip"], "audit": audit, "bodies": bodies}


def inventory(folder):
    files = {}
    for path in sorted(folder.rglob("*")):
        if path.is_symlink():
            raise ValueError("unexpected link in the regtest backup")
        if path.is_file():
            data = path.read_bytes()
            files[str(path.relative_to(folder))] = {"size": len(data), "sha256": digest(data)}
    if not {"regtest/blocks.dat", "regtest/chainstate.bin"} <= files.keys():
        raise ValueError("backup is missing its block log or snapshot")
    return files


def rehearse(assets, tool, scratch, approved):
    records = json.loads(approved.read_bytes())
    if [row["version"] for row in records] != [OLD, NEW]:
        raise ValueError("expected both signed release records")
    old, new = (assets / version / NODE for version in (OLD, NEW))
    for binary, record in zip((old, new), records, strict=True):
        if digest(binary.read_bytes()) != record["linux_sha256"] or record["modified_manifest_refused"] is not True:
            raise ValueError("binary no longer matches release verification")
        version = subprocess.check_output([binary, "--version"], text=True, timeout=5).strip()
        if version != f"requantd/{record['version']}":
            raise ValueError("binary reports an unexpected release")
    scratch.mkdir(mode=0o700)
    funding, destination = scratch / "funding.wallet", scratch / "recipient.wallet"
    payer_watch = scratch / "funding.watch"
    wallet(tool, None, "create", funding, "--no-passphrase")
    address = wallet(tool, None, "create", destination, "--no-passphrase")
    [payee] = re.findall(r"^address\s+(\S+)$", address, re.MULTILINE)
    wallet(tool, None, "watchonly", funding, payer_watch)
    miner = json.loads(payer_watch.read_bytes())["receive"][0]
    original, backup = scratch / "initial", scratch / "sealed"
    upgraded, restored = scratch / "upgraded", scratch / "restored"
    with node(old, original, scratch / "old.log", miner) as (port, _):
        wait_for(lambda: rpc(port, "getinfo")["height"] >= 8)
        payment = wallet(tool, port, "send", funding, payee, "1", "--yes")
        [txid] = re.findall(r"^sent, txid ([a-f0-9]{64})$", payment, re.MULTILINE)
        wait_for(lambda: rpc(port, "getinfo")["height"] >= CHECKPOINT)
    shutil.copytree(original, backup)
    sealed = inventory(backup)
    shutil.copytree(backup, upgraded)
    shutil.copytree(backup, restored)
    if inventory(upgraded) != sealed or inventory(restored) != sealed:
        raise ValueError("a working copy failed intake verification")

    with node(old, restored, scratch / "restored.log") as (port, _):
        checkpoint = block_set(port, OLD, CHECKPOINT, txid, offline=True)
    with node(new, upgraded, scratch / "upgraded.log") as (port, _):
        if block_set(port, NEW, CHECKPOINT, txid, offline=True) != checkpoint:
            raise ValueError("the upgraded copy diverged at its initial checkpoint")
    with node(new, upgraded, scratch / "upgraded.log", miner) as (port, _):
        wait_for(lambda: rpc(port, "getinfo")["height"] >= EXTENDED)
    with node(new, upgraded, scratch / "upgraded.log") as (port, peer):
        extended = block_set(port, NEW, EXTENDED, txid, offline=True)
        with node(old, restored, scratch / "restored.log", peer=peer) as (restoring, _):
            wait_for(lambda: rpc(restoring, "getinfo")["tip"] == extended["tip"])
            if block_set(restoring, OLD, EXTENDED, txid) != extended:
                raise ValueError("restored node diverged after synchronization")
    with node(old, restored, scratch / "restored.log") as (port, _):
        if block_set(port, OLD, EXTENDED, txid, offline=True) != extended:
            raise ValueError("restored node diverged after offline restart")
    if inventory(backup) != sealed:
        raise ValueError("the sealed backup changed")
    print(json.dumps({
        "payment_txid": txid, "checkpoint": checkpoint, "extended": extended,
        "verified_backup_inventory": sealed, "synced_and_reopened": True,
    }, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("assets", type=Path)
    run = sub.add_parser("run")
    for argument in ("assets", "wallet", "scratch", "approved"):
        run.add_argument(argument, type=Path)
    args = parser.parse_args()
    if args.mode == "verify":
        print(json.dumps([signed_asset(args.assets, version) for version in (OLD, NEW)], indent=2))
    else:
        rehearse(args.assets.resolve(), args.wallet.resolve(), args.scratch.resolve(), args.approved)


if __name__ == "__main__":
    main()
