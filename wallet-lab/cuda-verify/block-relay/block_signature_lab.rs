use super::mixed_cuda_lab::{prepare, verify_batch, Prepared};
use super::*;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Instant;

const BATCH: usize = 32;
const CUDA_ERROR_NO_DEVICE: i32 = 100;
static DEVICE_UNAVAILABLE: AtomicBool = AtomicBool::new(false);

enum BatchFailure {
    Invalid,
    Cuda(i32),
}

fn verify_group(batch: &[Prepared]) -> Result<(), BatchFailure> {
    let verdicts = verify_batch(batch.iter()).map_err(BatchFailure::Cuda)?;
    if verdicts.len() != batch.len() || verdicts.iter().any(|ok| !ok) {
        return Err(BatchFailure::Invalid);
    }
    Ok(())
}

fn try_cuda(id: &Hash, txs: &[Tx]) -> Result<usize, BatchFailure> {
    let mut batch = Vec::with_capacity(BATCH);
    let mut total = 0;
    for tx in txs {
        if let Tx::Transfer { inputs, .. } = tx {
            if let [input] = inputs.as_slice() {
                if let Unlock::MlDsa44(witness) = &input.unlock {
                    batch.push(prepare(id, tx, input, witness).map_err(|_| BatchFailure::Invalid)?);
                    total += 1;
                    if batch.len() == BATCH {
                        verify_group(&batch)?;
                        batch.clear();
                    }
                    continue;
                }
            }
        }
        tx.check_signatures(id).map_err(|_| BatchFailure::Invalid)?;
    }
    if !batch.is_empty() {
        verify_group(&batch)?;
    }
    Ok(total)
}

pub(crate) fn check_block_signatures(id: &Hash, height: u64, txs: &[Tx]) -> Result<(), Error> {
    let cpu = || txs.iter().try_for_each(|tx| tx.check_signatures(id));
    if *id != chain_id() {
        return cpu();
    }
    let mode = std::env::var("REQUANT_PQC_BLOCK_LAB");
    if !matches!(mode.as_deref(), Ok("cpu" | "cuda")) {
        return cpu();
    }
    let start = Instant::now();
    let (backend, count, result) = match mode.as_deref() {
        Ok("cuda") if DEVICE_UNAVAILABLE.load(Ordering::Relaxed) => ("cpu-unavailable", 0, cpu()),
        Ok("cuda") => match try_cuda(id, txs) {
            Ok(0) => ("cpu-no-pq", 0, Ok(())),
            Ok(verified) => ("cuda", verified, Ok(())),
            // Repeat the ordinary checks in block order on a failed GPU attempt.
            Err(error) => {
                // An absent device is fixed for this CUDA process. Signature
                // refusals and other errors leave later attempts available.
                if matches!(error, BatchFailure::Cuda(CUDA_ERROR_NO_DEVICE)) {
                    DEVICE_UNAVAILABLE.store(true, Ordering::Relaxed);
                }
                ("cpu-fallback", 0, cpu())
            }
        },
        _ => ("cpu", 0, cpu()),
    };
    eprintln!(
        "PQC_BLOCK_SIGS height={height} backend={backend} gpu_verified={count} signatures_ok={} elapsed_us={}",
        result.is_ok(), start.elapsed().as_micros(),
    );
    result
}
