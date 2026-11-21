use super::*;
use crate::chain::Chain;

pub(super) type Prepared = (VerifyingKey<MlDsa44>, Hash, Signature<MlDsa44>);

pub(super) fn prepare(
    id: &Hash,
    tx: &Tx,
    input: &Input,
    witness: &Witness,
) -> Result<Prepared, Error> {
    check_encoding(id, input)?;
    let key = VerifyingKey::<MlDsa44>::decode(&witness.public.into());
    let sig = Signature::<MlDsa44>::decode(&witness.signature.into())
        .ok_or(Error::Invalid("bad ML-DSA signature encoding"))?;
    Ok((key, Tx::sighash(id, &tx.txid(), 0), sig))
}

pub(super) fn verify_batch<'a>(
    prepared: impl Iterator<Item = &'a Prepared>,
) -> Result<Vec<bool>, i32> {
    let batch = prepared
        .map(|(key, message, sig)| (key, message.as_slice(), CONTEXT, sig))
        .collect::<Vec<_>>();
    VerifyingKey::<MlDsa44>::verify_cuda_lab(&batch, cuda_reconstruct)
}

#[derive(Debug, PartialEq, Eq)]
pub enum BatchBackend {
    CpuOnly,
    Cuda,
    CpuFallback,
}

/// Check independent transactions against one immutable chain tip. Keep a result
/// per wire, including its fee or the first validation error. This lab path
/// never admits transactions to a mempool or applies a block transition.
pub fn check_mixed_batch(
    chain: &Chain,
    wires: &[Vec<u8>],
) -> Result<(Vec<Result<u64, Error>>, BatchBackend), Error> {
    if !(1..=128).contains(&wires.len()) {
        return Err(Error::Invalid("bad validation batch size"));
    }
    let id = &chain.net.chain_id;
    let decoded = wires
        .iter()
        .map(|wire| {
            let tx = Tx::decode_exact(wire)?;
            tx.check_shape()?;
            let prepared = match &tx {
                Tx::Transfer { inputs, .. } => match inputs.as_slice() {
                    [
                        input @ Input {
                            unlock: Unlock::MlDsa44(witness),
                            ..
                        },
                    ] => Some(prepare(id, &tx, input, witness)?),
                    _ => None,
                },
                _ => None,
            };
            if prepared.is_none() {
                tx.check_signatures(id)?;
            }
            Ok((tx, prepared))
        })
        .collect::<Vec<Result<_, Error>>>();
    let prepared = decoded
        .iter()
        .filter_map(|entry| entry.as_ref().ok()?.1.as_ref());
    let count = prepared.clone().count();
    let (backend, gpu_verdicts) = if count == 0 {
        (BatchBackend::CpuOnly, Vec::new())
    } else {
        match verify_batch(prepared) {
            Ok(verdicts) if verdicts.len() == count => (BatchBackend::Cuda, verdicts),
            _ => (BatchBackend::CpuFallback, Vec::new()),
        }
    };
    let mut gpu_verdicts = gpu_verdicts.into_iter();
    let results = decoded
        .into_iter()
        .map(|entry| {
            let (tx, prepared) = entry?;
            if prepared.is_some() {
                match gpu_verdicts.next() {
                    Some(true) => (),
                    Some(false) => return Err(Error::Invalid("bad ML-DSA signature")),
                    None => tx.check_signatures(id)?,
                }
            }
            chain.check_spend(&tx)
        })
        .collect();
    Ok((results, backend))
}
