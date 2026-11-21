"""Convert the pinned NIST ACVP group 1 files into the Rust fixture's TSV."""
import hashlib
import json
from pathlib import Path
import sys
from urllib.request import urlopen


SOURCE = (
    "https://raw.githubusercontent.com/usnistgov/ACVP-Server/"
    "975de31eb83d87039ec88934fdc47d8c312b892d/gen-val/json-files"
)
FILES = {
    "keyGen": (
        "43e81ad820e495dbcad086fe27c1008393a8c32100bbbff77c558c3f06dcefef",
        "361f47ca19d592adcc66ff2cb591686ad785fea157b295648738bed6921a68df",
    ),
    "sigGen": (
        "447749d72817b211160d243311ce32302f3023e59c355b0f70be2bd3e9e7830d",
        "228d011bbe274aeb93e22eea1e0d57b78f43795cf6a64fb5ef1e626485a0bedb",
    ),
    "sigVer": (
        "e2cba4589389756fa0bea1a7e6837138bf0a81f9d14234c9ee8f6d33caa1654e",
        "e1d84ef1b2f35196278ab0b0ed6a46ec62cc03d2dfa92c564199e1999bfb8ea6",
    ),
}


def group(mode, kind, digest, cached):
    if cached is None:
        with urlopen(f"{SOURCE}/ML-DSA-{mode}-FIPS204/{kind}.json", timeout=30) as response:
            payload = response.read()
    else:
        payload = (cached / f"{mode}-{kind}.json").read_bytes()
    if hashlib.sha256(payload).hexdigest() != digest:
        raise ValueError(f"{mode}/{kind} digest mismatch")
    return next(group for group in json.loads(payload)["testGroups"] if group["tgId"] == 1)


def main():
    cached = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    for mode, (prompt_hash, results_hash) in FILES.items():
        prompt = group(mode, "prompt", prompt_hash, cached)
        results = group(mode, "expectedResults", results_hash, cached)
        if prompt["parameterSet"] != "ML-DSA-44":
            raise ValueError("unexpected ACVP parameter set")
        expected = {test["tcId"]: test for test in results["tests"]}
        for test in prompt["tests"]:
            case = test["tcId"]
            result = expected.pop(case)
            if mode == "keyGen":
                row = ("key", case, test["seed"], result["pk"], result["sk"])
            elif mode == "sigGen":
                row = ("sign", case, test["sk"], test["message"], test["context"], result["signature"])
            else:
                status = json.dumps(result["testPassed"])
                row = ("verify", case, test["pk"], test["message"], test["context"], test["signature"], status)
            print(*row, sep="\t")
        if expected:
            raise ValueError(f"{mode} has unused expected results")


if __name__ == "__main__":
    main()
