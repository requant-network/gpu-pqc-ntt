"""Run three synthetic Windows-to-Linux wallet restores over authenticated SSH."""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import shlex
import statistics
import subprocess
import tempfile


TEST_ARGS = ("portable_backup::portable_child", "--exact", "--ignored", "--nocapture")


def run(args, *, check=True, **kwargs):
    return subprocess.run(args, check=check, capture_output=True, timeout=90, **kwargs)


def metrics(output, prefix):
    line, = (row for row in output.decode().splitlines() if row.startswith(prefix))
    return dict(field.split("=") for field in line.split())


def trial(args, verify_refusals):
    transport = ["-i", str(args.identity), "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes"]

    def remote(*command, **kwargs):
        return run(["ssh", "-T", *transport, args.receiver, shlex.join(command)], **kwargs)

    remote_dir = PurePosixPath(remote(
        "mktemp", "-d", "--", str(args.remote_root / "portable.XXXXXXXX")
    ).stdout.decode().strip())
    files = ["wallet.backup", "restored.bin"]
    code = bytearray(secrets.token_bytes(32))
    try:
        with tempfile.TemporaryDirectory(prefix="requant-portable-") as local_dir:
            folder = Path(local_dir)
            exported = run(
                [str(args.sender_bin), *TEST_ARGS],
                env={**os.environ, "RQPL_PORTABLE_DIR": local_dir, "RQPL_PORTABLE_PHASE": "export"},
                input=code,
            )
            owner = (folder / "owner.bin").read_bytes()
            if len(owner) != 32:
                raise RuntimeError("invalid sender owner pin")
            backup = folder / "wallet.backup"
            run(["scp", *transport, str(backup), f"{args.receiver}:{remote_dir}/"])
            command = (
                "env", f"RQPL_PORTABLE_DIR={remote_dir}", "RQPL_PORTABLE_PHASE=restore",
                f"RQPL_PORTABLE_OWNER={owner.hex()}", str(args.receiver_bin), *TEST_ARGS,
            )
            refused = []
            if verify_refusals:
                wrong_code = code.copy()
                wrong_code[0] ^= 1
                try:
                    bad = remote(*command, input=wrong_code, check=False)
                finally:
                    wrong_code[:] = bytes(32)
                if bad.returncode != 101 or b"backup authentication failed" not in bad.stderr:
                    raise RuntimeError("receiver failed to reject a wrong recovery code")
                remote("test", "!", "-e", str(remote_dir / "restored.bin"))
                refused.append("wrong_recovery_code")

                bad = remote(*command, input=memoryview(code)[1:], check=False)
                if bad.returncode != 101 or b"UnexpectedEof" not in bad.stderr:
                    raise RuntimeError("receiver failed to reject short recovery input")
                remote("test", "!", "-e", str(remote_dir / "restored.bin"))
                refused.append("short_recovery_input")

            restored = remote(*command, input=code)
            wire = remote("cat", "--", str(remote_dir / "restored.bin")).stdout
            expected = (folder / "donor.bin").read_bytes()
            if wire != expected:
                raise RuntimeError("restored wallet did not reproduce the sender transaction")
            export = metrics(exported.stdout, "export_ms=")
            restore = metrics(restored.stdout, "restore_ms=")
            if int(export["envelope_bytes"]) != 129 or len(wire) != 3_924:
                raise RuntimeError("unexpected backup or transaction size")
            if any(int(row["confirmed_wire_bytes"]) != len(wire) for row in (export, restore)):
                raise RuntimeError("unexpected confirmation result")
            return {
                "envelope_bytes": int(export["envelope_bytes"]),
                "confirmed_wire_bytes": len(wire),
                "export_ms": float(export["export_ms"]),
                "restore_ms": float(restore["restore_ms"]),
                "wire_sha256": hashlib.sha256(wire).hexdigest(),
                "receiver_refusals_without_wire": refused,
            }
    finally:
        code[:] = bytes(32)
        remote("rm", "-f", "--", *(str(remote_dir / name) for name in files))
        remote("rmdir", "--", str(remote_dir))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sender_bin", type=Path)
    parser.add_argument("receiver")
    parser.add_argument("receiver_bin", type=PurePosixPath)
    parser.add_argument("remote_root", type=PurePosixPath)
    parser.add_argument("identity", type=Path)
    args = parser.parse_args()
    if not args.remote_root.is_absolute() or not args.receiver_bin.is_absolute():
        parser.error("receiver paths must be absolute")
    if args.receiver.startswith("-"):
        parser.error("receiver must be an SSH destination")
    results = [trial(args, verify_refusals=i == 0) for i in range(3)]
    if len({row["wire_sha256"] for row in results}) != len(results):
        raise RuntimeError("expected a distinct wallet transaction for each trial")
    print(json.dumps({
        "trials": results,
        "median_export_ms": statistics.median(row["export_ms"] for row in results),
        "median_restore_ms": statistics.median(row["restore_ms"] for row in results),
    }, indent=2))


if __name__ == "__main__":
    main()
