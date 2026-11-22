# Recover a ReQuant lab node from a pre-upgrade copy or a clean sync

The local Linux server used isolated loopback processes and disposable
data directories. The legacy observer created a clean height-4 backup
before the first mixed-signature block. The approved lab build later
stopped at height 34. Its full directory was copied and inventoried as
incident evidence. Recovery used the pre-upgrade backup and a separate
empty directory.

1. Shut down the source of each preserved copy before inventorying
   it. Record the size and SHA-256 of every file. Preserve the height-4
   backup separately from the height-34 source and its incident copy.
2. Change a separate copy of the height-4 block log. The expected
   inventory must refuse that copy before a receiver opens it.
3. Open a verified working copy of the height-4 backup with the approved
   build and no peers. Check the snapshot height, accepted tip, UTXO
   audit and all four encoded blocks before entering synchronization.
4. Synchronize both the backup-based node and a newly initialized node
   against the same producer at height 46. Verify each block's ID,
   SHA-256, size and transaction count. Compare the full UTXO audit.
   Restart the clean-sync node with no peers and check it again.
5. Receive 18 further mixed blocks on the backup-based node. Shut it
   down and reopen it without peers. Compare all 64 block bodies and
   the final UTXO audit, and confirm that each preserved inventory
   still matches.

The fixture gives the recovery check the exact expected chain through
a separate producer and retains its public block digests. The test
covers the recovery and return-to-service checks on the isolated lab
chain. Mixed production releases, reorganization and public network
operations remain open.

## Results

Both routes recovered the same height-46 tip and UTXO audit in all
three trials. The backup-based node accepted the remaining 18 blocks
and opened all 64 bodies again after an offline restart. Each
64-block manifest includes 3,840 ML-DSA and 960 Ed25519 spends.
The original backup, stopped node directory and its evidence copy
matched their initial inventories after recovery. All altered copies
were refused before opening a receiver.

| Trial | Backup path to height 46 | Empty-directory path to height 46 |
| --- | ---: | ---: |
| [1](trial-1.json) | 1,344.342 ms | 1,355.338 ms |
| [2](trial-2.json) | 1,351.800 ms | 1,359.390 ms |
| [3](trial-3.json) | 1,343.765 ms | 1,349.439 ms |

The clock starts at receiver launch after copy intake and stops
after verifying the checkpoint, full UTXO audit and all 46 block
encodings. These times describe one short loopback chain. The two
paths took about the same time at this checkpoint.

## Reproduce

Use the [native relay fixture](../cross-host/README.md#reproduce) and the
[legacy observer](../mixed-version/README.md#reproduce) from ReQuant base
`967ce1831a37b56832bda4d52ddd29c428a18f9b`. Run both examples on one
server. The legacy example participates only through the Ed25519
funding prefix.

```sh
python3 -B run_recovery.py /absolute/path/to/relay_lab /absolute/path/to/legacy_observer
```
