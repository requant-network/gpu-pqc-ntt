# Windows user-protected ML-DSA wallet file

November 21, 2026. The isolated wallet lab used Windows DPAPI to
protect the existing 33-byte versioned ML-DSA-44 seed backup. A fresh
process generated the seed, wrote the encrypted file and exited. The
test parent used the published public key to fund the owner on the lab
chain. Separate processes reopened the protected file and signed the
same funded transfer. The chain accepted the recovered transaction and
rejected a change to the signed output.

## Storage and validation

The file has the `RQPL-WS1` header followed by the DPAPI blob. The
writer calls `CryptProtectData` with the current-user defaults, a
fixed wallet-purpose entropy string, and `CRYPTPROTECT_UI_FORBIDDEN`.
It flushes the ciphertext to a new temporary file and publishes it
with a same-volume NTFS hard link. An earlier wallet remains intact
when a second initialization tries the same target. The reader bounds
the envelope, checks the format and opens it with `CryptUnprotectData`.
It validates the backup version and verifies the regenerated public
owner against the owner independently pinned from the funded UTXO.
The decrypted DPAPI buffer is zeroized before `LocalFree`, and the
signing key uses the existing zeroization-enabled `ml-dsa` build.
[Windows protection API](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata) ·
[Windows recovery API](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptunprotectdata).

The tested protection boundary is the Windows user profile. The lab
keeps private seed bytes in process memory, and another process under
that same user can reopen the protected file. The chain and signing
files are confined to disposable lab state. Crash or power-loss
durability of a deployed wallet filesystem is outside the fixture.

## Measured result

Three fresh harness runs produced three 270-byte protected files. Each
run signed the same funded 3,924-byte wire from five fresh signer
processes, the fifth using a copy of the protected file. All five
signed byte strings within each run matched. Seven refusal controls
per run covered a changed byte, truncation, unknown envelope format,
oversize, different protection purpose, a valid file for another owner,
and a missing file. None of the 21 attempts created a signed output.
All three attempts to overwrite an existing wallet were refused.
The original migration test, 54 focused CPU regressions and the pinned
NIST ACVP vectors passed on the same Windows build.

| Boundary | Median | Range over 15 fresh processes |
| --- | ---: | ---: |
| Read file, open DPAPI, restore key and check owner | 1.647 ms | 1.287–2.577 ms |
| Sign and encode | 0.992 ms | 0.256–1.826 ms |
| Full signer process, including launch, checks and output write | 12.364 ms | 10.060–14.114 ms |

The measurement covers the Ryzen 7 8745HS Windows workstation, with
local NTFS and Rust 1.98.1. The wider process timing includes reading
the unsigned transaction and validating the signature before publishing
the output. The test removed its fixture directories after completion.
[Timings](results.json) · [First run](vault-1.log.txt) ·
[Second run](vault-2.log.txt) · [Third run](vault-3.log.txt) ·
[Regressions](regressions.log.txt) · [ACVP](acvp.log.txt) ·
[Environment and source hashes](environment.log.txt).

## Account boundary

On November 22, three fresh wallets produced by the Rust fixture opened
under the owner account. A process running as the built-in Windows
LocalService account with a limited token read each intact encrypted
file and attempted current-user DPAPI recovery using the same wallet
entropy. Windows refused all three with `0x8009000b`. The second
principal successfully protected and reopened its own control bytes
in each process. SHA-256 confirmed that both principals read the same
ciphertext and the original file stayed intact. The probe used the
Windows .NET DPAPI wrapper for recovery and wrote only diagnostics.

This tests the boundary between one interactive user and one existing
service account on a single Windows machine. Cross-machine DPAPI
recovery, shared or roaming user profiles, and privileged compromise
are outside the trial.
[Trial 1](account-boundary-1.json) · [Trial 2](account-boundary-2.json) ·
[Trial 3](account-boundary-3.json).

## Reproduce

Apply the [native wallet fixture patch](../../README.md#reproduce) to
the pinned disposable ReQuant checkout. Set `$Native` to that checkout
and `$Study` to this research repository. Add the Windows-only test
module from this directory and run it offline from PowerShell.

```powershell
$source = Join-Path $Study 'wallet-lab/client/windows-vault'
$tests = Join-Path $Native 'crates/consensus/tests'
Copy-Item -LiteralPath (Join-Path $source 'dpapi.rs') -Destination (Join-Path $tests 'pqc_dpapi.inc')
Copy-Item -LiteralPath (Join-Path $source 'windows_vault.rs') -Destination (Join-Path $tests 'pqc_windows_vault.inc')
Add-Content -LiteralPath (Join-Path $tests 'pqc_wallet.rs') -Value @(
  '', '#[cfg(windows)]', 'mod windows_vault { include!("pqc_windows_vault.inc"); }'
)
cargo test --manifest-path (Join-Path $Native 'Cargo.toml') --offline --locked --release `
  -p requant-consensus --test pqc_wallet `
  windows_vault::windows_vault_restarts_and_fails_closed -- --exact --nocapture
```

Repeat the test in three fresh processes, saving the outputs as
`vault-1.log.txt` through `vault-3.log.txt`. Run `python summarize.py`
from this directory to rebuild the summary.

For the account boundary, use the compiled `pqc_wallet` test binary
from that build. Run the following from this directory in an elevated
Windows PowerShell session. The one-shot task runs only the inspection
under LocalService and is removed with its disposable directory.

```powershell
.\check-account-boundary.ps1 -WalletTest 'C:\absolute\target\release\deps\pqc_wallet-HASH.exe'
```
