"""Exercise one portable backup through Linux process and container memory limits."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import secrets
import shutil
import statistics
import subprocess
import tempfile
from time import perf_counter

from run import TEST_ARGS, metrics


WORKSPACE_ERROR = b"Argon2id workspace unavailable"
AUTH_ERROR = b"backup authentication failed"
MIB = 1 << 20


def command(argv, *, check=True, **kwargs):
    return subprocess.run(argv, check=check, capture_output=True, timeout=30, **kwargs)


def fixture(binary, folder, phase, code, owner="", limit=None):
    env = dict(os.environ, MALLOC_ARENA_MAX="1", RQPL_PORTABLE_DIR=str(folder),
               RQPL_PORTABLE_PHASE=phase, RQPL_PORTABLE_OWNER=owner)

    def constrain():
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        if limit is not None:
            resource.setrlimit(resource.RLIMIT_AS, (limit * MIB, limit * MIB))

    argv = [binary, *TEST_ARGS]
    rss = folder / "peak-rss.txt"
    if limit == 96:
        argv = ["/usr/bin/time", "-f", "%M", "-o", rss, *argv]
    proc = command(argv, check=False, input=code, env=env, preexec_fn=constrain)
    peak_kib = int(rss.read_text()) if limit == 96 and proc.returncode == 0 else None
    return proc, peak_kib


def in_container(binary, folder, code, owner, limit, image):
    cid = command([
        "docker", "create", "--pull", "never", "--network", "none", "--read-only",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--ulimit", "core=0:0",
        "--memory", f"{limit}m", "--memory-swap", f"{limit}m", "--cpus", "2",
        "--user", f"{os.getuid()}:{os.getgid()}", "-i",
        "--mount", f"type=bind,source={binary},target=/wallet-test,readonly",
        "--mount", f"type=bind,source={folder},target=/vault",
        "--env", "RQPL_PORTABLE_DIR=/vault", "--env", "RQPL_PORTABLE_PHASE=restore",
        "--env", f"RQPL_PORTABLE_OWNER={owner}", image, "/wallet-test", *TEST_ARGS,
    ]).stdout.decode().strip()
    if not re.fullmatch(r"[0-9a-f]{64}", cid):
        raise RuntimeError("invalid isolated container ID")
    try:
        start = perf_counter()
        proc = command(["docker", "start", "-ai", cid], check=False, input=code)
        elapsed = perf_counter() - start
        info = json.loads(command(["docker", "inspect", cid]).stdout)[0]
        config, status = info["HostConfig"], info["State"]
        assert config["Memory"] == config["MemorySwap"] == limit * MIB and config["NetworkMode"] == "none"
        assert not status["Running"]
        return proc, status["ExitCode"], status["OOMKilled"], elapsed
    finally:
        command(["docker", "rm", "-f", cid])


def trial(args, check_original):
    code = bytearray(secrets.token_bytes(32))
    try:
        with tempfile.TemporaryDirectory(prefix="requant-backup-memory-") as scratch:
            root = Path(scratch)
            before = root / "limited-export"
            before.mkdir()
            suppressed, _ = fixture(args.fixed, before, "export", code, limit=64)
            assert suppressed.returncode == 101 and WORKSPACE_ERROR in suppressed.stderr
            assert not list(before.iterdir()), "partial backup appeared after refused allocation"

            origin = root / "origin"
            origin.mkdir()
            sealed, _ = fixture(args.fixed, origin, "export", code)
            assert sealed.returncode == 0, sealed.stderr.decode()
            owner_bytes = (origin / "owner.bin").read_bytes()
            assert len(owner_bytes) == 32
            owner = owner_bytes.hex()
            backup = (origin / "wallet.backup").read_bytes()
            wire = (origin / "donor.bin").read_bytes()
            assert len(backup) == 129 and len(wire) == 3924

            address = root / "address"
            container = root / "container"
            for directory in (address, container):
                directory.mkdir()
                shutil.copyfile(origin / "wallet.backup", directory / "wallet.backup")
            original_exit = None
            if check_original:
                legacy, _ = fixture(args.original, address, "restore", code, owner, 64)
                assert legacy.returncode == -6 and b"memory allocation of 67108864 bytes failed" in legacy.stderr
                assert not (address / "restored.bin").exists()
                original_exit = legacy.returncode

            denied, _ = fixture(args.fixed, address, "restore", code, owner, 64)
            assert denied.returncode == 101 and WORKSPACE_ERROR in denied.stderr
            assert not (address / "restored.bin").exists()
            assert (address / "wallet.backup").read_bytes() == backup

            wrong = code.copy()
            wrong[0] ^= 1
            try:
                rejected, _ = fixture(args.fixed, address, "restore", wrong, owner, 96)
            finally:
                wrong[:] = bytes(len(wrong))
            assert rejected.returncode == 101 and AUTH_ERROR in rejected.stderr
            assert not (address / "restored.bin").exists()
            valid, peak_kib = fixture(args.fixed, address, "restore", code, owner, 96)
            assert valid.returncode == 0 and peak_kib is not None
            assert (address / "restored.bin").read_bytes() == wire
            assert (address / "wallet.backup").read_bytes() == backup

            _, limited_exit, oom, _ = in_container(args.fixed, container, code, owner, 64, args.image)
            assert oom and limited_exit == 137
            assert not (container / "restored.bin").exists()
            assert (container / "wallet.backup").read_bytes() == backup
            restored, restored_exit, oom_retry, wall_seconds = in_container(
                args.fixed, container, code, owner, 96, args.image,
            )
            assert restored.returncode == restored_exit == 0 and not oom_retry
            assert (container / "restored.bin").read_bytes() == wire
            assert (container / "wallet.backup").read_bytes() == backup
            return dict(
                envelope_bytes=len(backup), confirmed_wire_bytes=len(wire),
                wire_sha256=hashlib.sha256(wire).hexdigest(),
                original_address_64_exit=original_exit, fixed_address_64_export_exit=suppressed.returncode,
                fixed_address_64_restore_exit=denied.returncode, address_96_wrong_code_exit=rejected.returncode,
                address_96_peak_rss_kib=peak_kib,
                address_96_restore_ms=float(metrics(valid.stdout, "restore_ms=")["restore_ms"]),
                container_64_exit=limited_exit, container_64_oom=oom,
                container_96_restore_ms=float(metrics(restored.stdout, "restore_ms=")["restore_ms"]),
                container_96_host_wall_s=wall_seconds, same_backup_and_wire=True,
            )
    finally:
        code[:] = bytes(len(code))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixed", type=Path)
    parser.add_argument("original", type=Path)
    parser.add_argument("--image", default="ubuntu:24.04")
    args = parser.parse_args()
    args.fixed = args.fixed.resolve(strict=True)
    args.original = args.original.resolve(strict=True)
    image_id = command(["docker", "image", "inspect", "--format", "{{.Id}}", args.image]).stdout.decode().strip()
    args.image = image_id
    rows = [trial(args, check_original=i == 0) for i in range(3)]
    assert len({row["wire_sha256"] for row in rows}) == len(rows)
    summary = dict(
        image=image_id, workspace_mib=64, trials=rows,
        median_address_restore_ms=statistics.median(row["address_96_restore_ms"] for row in rows),
        median_container_restore_ms=statistics.median(row["container_96_restore_ms"] for row in rows),
        median_address_rss_kib=statistics.median(row["address_96_peak_rss_kib"] for row in rows),
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
