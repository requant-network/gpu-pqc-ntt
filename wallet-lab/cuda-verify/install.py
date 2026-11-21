"""Apply the CUDA experiment to a disposable native wallet-lab checkout."""
from pathlib import Path
import shutil
import sys


def replace(path, old, new):
    source = path.read_text()
    if source.count(old) != 1:
        raise ValueError(f"unexpected source in {path}: {old!r}")
    path.write_text(source.replace(old, new))


def append(path, source):
    with path.open("a") as handle:
        handle.write(source)


def install(native, registry, here):
    registry_manifest = (registry / "Cargo.toml").read_text()
    if 'name = "ml-dsa"' not in registry_manifest or 'version = "0.1.2"' not in registry_manifest:
        raise ValueError("expected the locked ml-dsa 0.1.2 crate")
    vendor = native / "vendor/ml-dsa"
    shutil.copytree(registry, vendor)
    append(native / "Cargo.toml", '\n[patch.crates-io]\nml-dsa = { path = "vendor/ml-dsa" }\n')
    shutil.copyfile(here / "cuda_verify_lab.rs", vendor / "src/cuda_verify_lab.rs")
    replace(
        vendor / "src/algebra.rs",
        "struct NttMatrix<K: ArraySize, L: ArraySize>(Array<MaybeBox<NttVector<L>>, K>);",
        "struct NttMatrix<K: ArraySize, L: ArraySize>(pub(crate) Array<MaybeBox<NttVector<L>>, K>);",
    )
    replace(
        vendor / "src/verifying.rs",
        "        let w1p = sigma.h.use_hint(&wp_approx);",
        """        self.verify_approx(mu, sigma, &wp_approx)
    }

    fn verify_approx(&self, mu: &B64, sigma: &Signature<P>, wp_approx: &Vector<P::K>) -> bool {
        let w1p = sigma.h.use_hint(wp_approx);""",
    )
    append(vendor / "src/verifying.rs", '\n#[path = "cuda_verify_lab.rs"]\nmod cuda_verify_lab;\n')

    consensus = native / "crates/consensus"
    replace(
        consensus / "src/tx.rs",
        """                    if *chain_id != pqc::chain_id() {
                        return Err(Error::Invalid("ML-DSA is disabled on this chain"));
                    }
                    // One encoding for this condition; the inherited Ed25519 slots are reserved.
                    if inp.pubkey != [0; 32] || inp.sig != [0; 64] || inp.anyone_can_pay {
                        return Err(Error::Invalid("noncanonical ML-DSA input"));
                    }""",
        """                    pqc::check_encoding(chain_id, inp)?;""",
    )
    append(
        consensus / "src/pqc.rs",
        """
pub(crate) fn check_encoding(id: &Hash, input: &Input) -> Result<(), Error> {
    if *id != chain_id() {
        return Err(Error::Invalid("ML-DSA is disabled on this chain"));
    }
    if input.pubkey != [0; 32] || input.sig != [0; 64] || input.anyone_can_pay {
        return Err(Error::Invalid("noncanonical ML-DSA input"));
    }
    Ok(())
}

#[link(name = "pqc_verify_lab")]
unsafe extern "C" {
    fn pqc_cuda_reconstruct(input: *const u32, output: *mut u32, count: i32) -> i32;
}

/// Lab-only bridge for the pinned polynomial layout.
pub fn cuda_reconstruct(input: &[u32], output: &mut [u32], count: usize) -> Result<(), i32> {
    if !(1..=128).contains(&count) || input.len() != count * 25 * 256 || output.len() != count * 4 * 256
        || input.iter().any(|&coefficient| coefficient >= 8_380_417)
    {
        return Err(-1);
    }
    // The checked slices remain live and distinct until the synchronous CUDA return.
    let status = unsafe { pqc_cuda_reconstruct(input.as_ptr(), output.as_mut_ptr(), count as i32) };
    if status == 0 { Ok(()) } else { Err(status) }
}

/// Candidate signature batch for single-input ML-DSA transactions on the lab chain.
pub fn check_cuda_batch(id: &Hash, txs: &[Tx]) -> Result<(), Error> {
    if txs.is_empty() || txs.len() > 128 {
        return Err(Error::Invalid("bad ML-DSA batch size"));
    }
    let mut prepared = Vec::with_capacity(txs.len());
    for tx in txs {
        tx.check_shape()?;
        let Tx::Transfer { inputs, .. } = tx else { return Err(Error::Invalid("not a transfer")) };
        let [input] = inputs.as_slice() else { return Err(Error::Invalid("expected one ML-DSA input")) };
        let Unlock::MlDsa44(witness) = &input.unlock else { return Err(Error::Invalid("expected ML-DSA input")) };
        check_encoding(id, input)?;
        let key = VerifyingKey::<MlDsa44>::decode(&witness.public.into());
        let sig = Signature::<MlDsa44>::decode(&witness.signature.into())
            .ok_or(Error::Invalid("bad ML-DSA signature encoding"))?;
        prepared.push((key, Tx::sighash(id, &tx.txid(), 0), sig));
    }
    let batch = prepared.iter().map(|(key, message, sig)| (key, message.as_slice(), CONTEXT, sig)).collect::<Vec<_>>();
    let checked = VerifyingKey::<MlDsa44>::verify_cuda_lab(&batch, cuda_reconstruct)
        .map_err(|_| Error::Invalid("CUDA backend error"))?;
    if checked.into_iter().all(|valid| valid) {
        Ok(())
    } else {
        Err(Error::Invalid("bad ML-DSA signature"))
    }
}
""",
    )
    shutil.copyfile(here / "native_checks.rs", consensus / "tests/pqc_cuda_checks.inc")
    replace(
        consensus / "tests/pqc_wallet.rs",
        "    let recovered = Tx::decode_exact(&signed).unwrap();",
        """    cuda::trial(&chain, &key, pq_coin, legacy_owner, half - FEE);
    let recovered = Tx::decode_exact(&signed).unwrap();""",
    )
    append(consensus / "tests/pqc_wallet.rs", '\nmod cuda { include!("pqc_cuda_checks.inc"); }\n')

    vectors = consensus / "tests/pqc_vectors.rs"
    replace(vectors, "    let mut counts = [0; 4];", "    let mut counts = [0; 4];\n    let mut dispatched = 0;")
    replace(
        vectors,
        """                let passed = signature
                    .is_some_and(|signature| pk.verify_with_context(&bytes(message), &bytes(context), &signature));""",
        """                let message = bytes(message);
                let context = bytes(context);
                let passed = signature.as_ref()
                    .is_some_and(|signature| pk.verify_with_context(&message, &context, signature));
                if let Some(signature) = signature.as_ref() {
                    let gpu = VerifyingKey::verify_cuda_lab(
                        &[(&pk, &message, &context, signature)],
                        requant_consensus::pqc::cuda_reconstruct,
                    ).unwrap();
                    assert_eq!(gpu, [passed], "GPU sigVer {id}");
                    dispatched += 1;
                }""",
    )
    replace(
        vectors,
        "    assert!(counts.iter().all(|&count| count > 0));",
        """    assert!(counts.iter().all(|&count| count > 0));
    assert!(dispatched > 0);
    println!("ACVP dispatched to GPU={dispatched}");""",
    )


if __name__ == "__main__":
    install(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve(), Path(__file__).resolve().parent)
