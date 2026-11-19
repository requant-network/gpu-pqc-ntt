#!/usr/bin/env python3
"""Orchestrator for the gpu-pqc-ntt study (stdlib only, runs on the server).

Modes:
  reference  -- exact Python reference selftest (2 seeds, edge cases, matrices)
  probe      -- INT8 GEMM support/exactness/dispatch probe (cuBLAS vs mma.sync
                vs naive SIMT control)
  verify     -- GPU correctness: every coefficient vs the Python reference,
                B in {1,16,256}, 2 seeds + edge cases, both primes, SIMT NTT,
                SIMT convolution, cuBLAS limb-GEMM NTT, mma.sync limb-GEMM NTT;
                negative controls (wrong zeta table / wrong limb base)
  bench      -- resident kernel medians (warmup 10 + 50 launches),
                B in {1,16,256,4096}, both primes, 4 methods
  benchx     -- one transfer-inclusive measurement per (prime, B, method)
  all        -- reference, probe, verify, bench, benchx in order

Every GPU invocation is expected to run under the campaign lock:
  CUDA_VISIBLE_DEVICES=0 flock /tmp/requant-new-studies-gpu.lock -c \
      'python3 run.py all'
run.py additionally pins CUDA_VISIBLE_DEVICES=0 itself and refuses to run if
the detected GPU is not the RTX 5060 Ti (compute cap 12.0).

All results JSON carry: UTC timestamp, GPU name / compute cap / driver / CUDA
runtime / cuBLAS versions, SHA-256 of executed sources + binary, raw counts.
"""

import hashlib
import json
import os
import platform
import struct
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

import reference as ref

ROOT = os.path.dirname(os.path.abspath(__file__))
BINARY = os.path.join(ROOT, "build", "pqc_ntt")
RESULTS = os.path.join(ROOT, "results")
SEEDS = (20261119, 987654321)
BATCHES_VERIFY = (1, 16, 256)
BATCHES_BENCH = (1, 16, 256, 4096)
PRIMES = (("kyber", ref.Q_KYBER), ("dilithium", ref.Q_DILITHIUM))


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def source_hashes():
    files = ["cuda/pqc_ntt.cu", "reference.py", "run.py",
             "tests/test_reference.py"]
    out = {f: sha256_file(os.path.join(ROOT, f)) for f in files}
    if os.path.exists(BINARY):
        out["build/pqc_ntt"] = sha256_file(BINARY)
    return out


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def gpu_env():
    info = json.loads(subprocess.run([BINARY, "info"], check=True,
                                     capture_output=True, text=True).stdout)
    try:
        smi = subprocess.run(
            ["nvidia-smi", "--query-gpu=driver_version",
             "--format=csv,noheader", "-i", "0"],
            check=True, capture_output=True, text=True).stdout.strip()
        info["nvidia_smi_driver"] = smi
    except Exception as e:  # keep going; CUDA driver version is authoritative
        info["nvidia_smi_driver_error"] = str(e)
    info["python"] = platform.python_version()
    return info


def guard_gpu(info):
    if info.get("compute_cap") != "12.0" or "5060" not in info.get("gpu", ""):
        raise SystemExit(f"REFUSING to run: expected RTX 5060 Ti (12.0), "
                         f"got {info.get('gpu')} ({info.get('compute_cap')})")


def run_bin(args):
    r = subprocess.run([BINARY] + args, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"binary failed: {args}\n{r.stderr}")
    return r.stdout


def write_polys(path, polys):
    with open(path, "wb") as f:
        for p in polys:
            f.write(struct.pack("<256i", *p))


def read_polys(path, count):
    with open(path, "rb") as f:
        data = f.read()
    assert len(data) == count * 256 * 4, f"short output {path}"
    return [list(struct.unpack_from("<256i", data, i * 1024))
            for i in range(count)]


def compare(got, expect):
    assert len(got) == len(expect)
    mism = sum(1 for a, b in zip(got, expect)
               for x, y in zip(a, b) if x != y)
    return {"coefficients": len(expect) * 256, "mismatches": mism,
            "exact": mism == 0}


def save_json(name, payload):
    os.makedirs(RESULTS, exist_ok=True)
    path = os.path.join(RESULTS, name)
    with open(path, "w") as f:
        json.dump(payload, f, indent=1)
    print(f"wrote {path}")


def mode_reference(env):
    ref.selftest()  # assert-based; raises on any failure
    save_json("reference_check.json", {
        "utc": utcnow(), "env": env, "source_hashes": source_hashes(),
        "selftest_ok": True, "seeds": list(SEEDS),
        "note": "schoolbook negacyclic ground truth; NTT+pointwise+INTT; "
                "matrix and limb-GEMM cross-checks; edge cases",
    })


def mode_probe(env):
    out = json.loads(run_bin(["probe"]))
    out.update({"utc": utcnow(), "env": env, "source_hashes": source_hashes()})
    save_json("probe.json", out)
    for row in out["probe"]:
        print(f"probe B={row['B']}: cublas status={row['cublas_status']} "
              f"exact={row['cublas_exact']} mma_exact={row['mma_exact']} "
              f"simt_exact={row['simt_control_exact']} "
              f"cublas={row['cublas_us']}us mma={row['mma_us']}us "
              f"simt={row['simt_control_us']}us")


def mode_verify(env):
    import random
    import shutil
    tmp = tempfile.mkdtemp(prefix="pqc-verify-", dir=ROOT)
    ain = os.path.join(tmp, "in.bin")
    outp = os.path.join(tmp, "out.bin")
    rows = []
    negatives = []
    try:
        for pname, q in PRIMES:
            ntt = ref.kyber_ntt if pname == "kyber" else ref.dilithium_ntt
            conv = ref.kyber_conv if pname == "kyber" else ref.dilithium_conv
            for seed in SEEDS:
                rng = random.Random(seed)
                cohorts = []
                for B in BATCHES_VERIFY:
                    a = ref.random_polys(rng, B, q)
                    b = ref.random_polys(rng, B, q)
                    cohorts.append((B, "random", a, b))
                for label, poly in zip(
                        ("zero", "max", "delta0", "max_top", "alternating"),
                        ref.edge_polys(q)):
                    cohorts.append((1, label, [poly], [poly]))
                for B, label, a, b in cohorts:
                    exp_ntt = [ntt(x) for x in a]
                    exp_conv = [conv(x, y) for x, y in zip(a, b)]
                    row = {"prime": pname, "q": q, "seed": seed, "B": B,
                           "cohort": label}
                    write_polys(ain, a)
                    run_bin(["ntt", pname, str(B), ain, outp])
                    row["simt_ntt"] = compare(read_polys(outp, B), exp_ntt)
                    write_polys(ain, a + b)
                    run_bin(["conv", pname, str(B), ain, outp])
                    row["simt_conv"] = compare(read_polys(outp, B), exp_conv)
                    write_polys(ain, a)
                    for route in ("cublas", "mma"):
                        run_bin(["gemm", pname, str(B), ain, outp, route])
                        row[f"gemm_{route}_ntt"] = compare(
                            read_polys(outp, B), exp_ntt)
                    row["all_exact"] = all(
                        row[k]["exact"] for k in
                        ("simt_ntt", "simt_conv", "gemm_cublas_ntt",
                         "gemm_mma_ntt"))
                    rows.append(row)
                    print(f"verify {pname} seed={seed} B={B} {label}: "
                          f"{'OK' if row['all_exact'] else 'FAIL'}",
                          flush=True)
                    if not row["all_exact"]:
                        save_json("verification_FAIL.json",
                                  {"utc": utcnow(), "env": env, "row": row})
                        raise SystemExit("GPU verification FAILED")
            # negative controls: wrong zeta table (SIMT) and wrong limb base
            # (GEMM recombination) must NOT reproduce the reference.
            rng = random.Random(SEEDS[0])
            a = ref.random_polys(rng, 16, q)
            exp = [ntt(x) for x in a]
            write_polys(ain, a)
            run_bin(["ntt", pname, "16", ain, outp, "badzeta"])
            c = compare(read_polys(outp, 16), exp)
            negatives.append({"prime": pname, "control": "simt_badzeta",
                              **c, "rejected": not c["exact"]})
            for route in ("cublas", "mma"):
                run_bin(["gemm", pname, "16", ain, outp, route, "bad"])
                c = compare(read_polys(outp, 16), exp)
                negatives.append({"prime": pname,
                                  "control": f"gemm_{route}_badbase",
                                  **c, "rejected": not c["exact"]})
        all_ok = (all(r["all_exact"] for r in rows)
                  and all(n["rejected"] for n in negatives))
        save_json("verification.json", {
            "utc": utcnow(), "env": env, "source_hashes": source_hashes(),
            "seeds": list(SEEDS), "batches": list(BATCHES_VERIFY),
            "cases": rows, "negative_controls": negatives,
            "total_coefficients_checked": sum(
                r[k]["coefficients"] for r in rows for k in
                ("simt_ntt", "simt_conv", "gemm_cublas_ntt", "gemm_mma_ntt")),
            "all_exact": all_ok,
        })
        print(f"verification: {'ALL EXACT' if all_ok else 'FAILED'} "
              f"({len(rows)} cases, {len(negatives)} negative controls)")
        if not all_ok:
            raise SystemExit("verification FAILED")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def mode_bench(env):
    rows = []
    for pname, q in PRIMES:
        for B in BATCHES_BENCH:
            for method in ("simt-ntt", "simt-conv", "gemm-cublas",
                           "gemm-mma"):
                out = json.loads(run_bin(
                    ["bench", pname, str(B), method, "50"]))
                rows.append(out)
                print(f"bench {pname} B={B} {method}: "
                      f"{out['median_us']:.2f} us "
                      f"({out['polys_per_s']:.0f} poly/s)", flush=True)
    save_json("bench_resident.json", {
        "utc": utcnow(), "env": env, "source_hashes": source_hashes(),
        "boundary": "resident: inputs preloaded on device before timing; "
                    "cudaEvent around pipeline launch(es) only; no H2D/D2H "
                    "inside the timed region; warmup 10 + 50 launches; "
                    "median of per-launch event times",
        "warmup": 10, "launches": 50, "rows": rows,
    })


def mode_benchx(env):
    rows = []
    for pname, q in PRIMES:
        for B in BATCHES_BENCH:
            for method in ("simt-ntt", "gemm-cublas", "gemm-mma"):
                out = json.loads(run_bin(["benchx", pname, str(B), method]))
                rows.append(out)
                print(f"benchx {pname} B={B} {method}: "
                      f"{out['median_us']:.2f} us (transfer-inclusive)",
                      flush=True)
    save_json("bench_transfer.json", {
        "utc": utcnow(), "env": env, "source_hashes": source_hashes(),
        "boundary": "transfer-inclusive: pinned-host H2D of B polynomials + "
                    "resident pipeline + D2H of B results, cudaEvent around "
                    "the whole sequence; 3 warmup reps + 5 timed reps; "
                    "median reported",
        "rows": rows,
    })


def main():
    os.environ["CUDA_VISIBLE_DEVICES"] = "0"
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    env = gpu_env()
    guard_gpu(env)
    print(f"GPU: {env['gpu']} (cap {env['compute_cap']}, "
          f"driver {env['driver_version']})")
    if mode in ("reference", "all"):
        mode_reference(env)
    if mode in ("probe", "all"):
        mode_probe(env)
    if mode in ("verify", "all"):
        mode_verify(env)
    if mode in ("bench", "all"):
        mode_bench(env)
    if mode in ("benchx", "all"):
        mode_benchx(env)


if __name__ == "__main__":
    main()
