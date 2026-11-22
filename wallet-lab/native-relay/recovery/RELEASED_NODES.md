# Backup restoration with the published Linux node releases

The local server used the downloadable `requantd-linux-x86_64` binaries
for ReQuant 0.18.2 and 0.18.3. Before starting the nodes, the harness
verified both release manifests with the public ReQuant signing key
embedded in the node source and matched both Linux binary hashes to
their signed manifest entries. A separately modified manifest was
refused for each release. The recorded signature hash covers the
decoded signature bytes. [Release 0.18.2](https://github.com/requant-network/requant/releases/tag/v0.18.2) ·
[Release 0.18.3](https://github.com/requant-network/requant/releases/tag/v0.18.3) ·
[Verification records](verified-releases.json).

Each rehearsal used a network-disabled Linux container, loopback
connections and a disposable regtest chain. The older release mined
the first 64 blocks and confirmed one ordinary Ed25519 wallet payment.
The stopped node supplied a sealed backup. Both working copies matched
the backup file inventory before either node opened them.

1. The older release opened a verified pre-upgrade working copy with
   zero peers and read the height-64 snapshot, UTXO audit and all 64 active
   block encodings.
2. The newer release opened its own working copy and matched that
   checkpoint. It produced four more ordinary blocks.
3. The restored old-release node synchronized to height 68 over native
   loopback P2P. It reopened offline from the new height-68 snapshot.
   All active block IDs, encoded-body SHA-256 values and the complete
   UTXO audit matched the newer node. The original payment appeared
   once and the sealed backup retained its byte inventory.

Three repetitions passed. The wallet helper was the separately built
native regtest CLI from ReQuant revision
`967ce1831a37b56832bda4d52ddd29c428a18f9b`, identified by its hash.
These results qualify the ordinary Ed25519 range for the published Linux
pair and the verified pre-upgrade
backup path. Published mixed-signature release qualification remains
a separate gate. [Trial 1](published-trial-1.json) ·
[Trial 2](published-trial-2.json) · [Trial 3](published-trial-3.json) ·
[Wallet helper digest](wallet-helper.sha256).

## Run the isolated backup rehearsal

Collect each signed manifest, signature and Linux binary under its
own version directory. Verify the assets with OpenSSL and make only
the approved Linux binaries executable. Use a dedicated lab workspace.

```sh
python3 -B released_nodes.py verify ASSETS > verified-releases.json
```

In a network-disabled container, mount the release assets, verification
record and native regtest wallet helper read-only. Use a fresh writable
scratch directory for each rehearsal. The tested Ubuntu container image
ID was `sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55`.

```sh
python3 -B released_nodes.py run ASSETS WALLET NEW_SCRATCH verified-releases.json
```
