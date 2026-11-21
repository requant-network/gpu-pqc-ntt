use super::*;
use pqc::{BatchBackend, check_mixed_batch};

const COUNT: usize = 256;
const VALUE: u64 = 10 * FEE;

fn outputs(payee: Hash, value: u64) -> Vec<Output> {
    vec![Output { value, pkh: payee }]
}

fn change_output(wire: &[u8]) -> Vec<u8> {
    let mut tx = Tx::decode_exact(wire).unwrap();
    let Tx::Transfer { outputs, .. } = &mut tx else {
        unreachable!()
    };
    outputs[0].value += 1;
    tx.encode()
}

fn fixture() -> (Chain, Vec<Vec<u8>>, Vec<Vec<u8>>, Vec<u8>, Vec<u8>) {
    let miner = EdKey::from_bytes(&[1; 32]);
    let payee = pkh(&miner.verifying_key().to_bytes());
    let mut chain = Chain::new(Network::pqc_lab(), 1);
    let first = block(&mut chain, &payee, vec![]);
    let coin = OutPoint {
        txid: first.txs[0].txid(),
        vout: 0,
    };
    chain.accept(first, NOW).unwrap();
    for _ in 0..chain.net.maturity {
        extend(&mut chain, &payee, vec![]);
    }

    let keys = (0..COUNT)
        .map(|slot| {
            // Public seeds for the disposable chain only.
            let mut seed = Seed::default();
            seed[0] = slot as u8;
            let mut ed_seed = [2; 32];
            ed_seed[0] = slot as u8;
            (
                EdKey::from_bytes(&ed_seed),
                SigningKey::<MlDsa44>::from_seed(&seed),
            )
        })
        .collect::<Vec<_>>();
    let mut funded = Vec::with_capacity(2 * COUNT + 1);
    for (ed, pq) in &keys {
        funded.extend([
            Output {
                value: VALUE,
                pkh: pkh(&ed.verifying_key().to_bytes()),
            },
            Output {
                value: VALUE,
                pkh: pqc::parse_address(&pqc::address(pq)).unwrap(),
            },
        ]);
    }
    funded.push(Output {
        value: chain.coin(&coin).unwrap().output.value - (2 * COUNT) as u64 * VALUE - FEE,
        pkh: payee,
    });
    let funding = legacy(&chain.net.chain_id, &miner, coin, funded);
    let txid = funding.txid();
    extend(&mut chain, &payee, vec![funding]);

    let mut ed_wires = Vec::new();
    let mut pq_wires = Vec::new();
    let outpoint = |vout| OutPoint { txid, vout };
    for (slot, (ed, pq)) in keys.iter().enumerate() {
        let value = VALUE - FEE - slot as u64;
        ed_wires.push(
            legacy(
                &chain.net.chain_id,
                ed,
                outpoint(2 * slot as u32),
                outputs(payee, value),
            )
            .encode(),
        );
        pq_wires.push(sign(pq, outpoint(2 * slot as u32 + 1), outputs(payee, value)).encode());
    }
    let (ed, pq) = &keys[0];
    let mut joint = Tx::Transfer {
        inputs: vec![Input::new(outpoint(0)), pqc::input(outpoint(1), pq)],
        outputs: outputs(payee, 2 * VALUE - FEE),
    };
    joint.sign_input(&chain.net.chain_id, 0, ed);
    pqc::sign(&mut joint, 1, pq).unwrap();
    let missing = sign(pq, outpoint(10_000), outputs(payee, VALUE - FEE));
    (chain, ed_wires, pq_wires, joint.encode(), missing.encode())
}

fn controls(expect: BatchBackend) {
    let (chain, ed, pq, joint, missing) = fixture();
    let mut unsupported_encoding = Tx::decode_exact(&pq[5]).unwrap();
    inputs(&mut unsupported_encoding)[0].pubkey[0] = 1;
    let mut malformed = pq[7].clone();
    malformed.pop();
    let cases = vec![
        ed[0].clone(),
        change_output(&pq[1]),
        pq[2].clone(),
        malformed,
        change_output(&ed[4]),
        unsupported_encoding.encode(),
        joint,
        missing,
        pq[8].clone(),
    ];
    let cpu = cases
        .iter()
        .map(|wire| validate(&chain, wire))
        .collect::<Vec<_>>();
    assert_eq!(cpu[0], Ok(FEE));
    assert_eq!(cpu[1], Err(Error::Invalid("bad ML-DSA signature")));
    assert_eq!(cpu[2], Ok(FEE + 2));
    assert!(matches!(cpu[3], Err(Error::Decode(_))));
    assert_eq!(cpu[4], Err(Error::Invalid("bad signature")));
    assert_eq!(cpu[5], Err(Error::Invalid("noncanonical ML-DSA input")));
    assert_eq!(cpu[6], Ok(FEE));
    assert_eq!(cpu[7], Err(Error::Invalid("missing or spent input")));
    assert_eq!(cpu[8], Ok(FEE + 8));
    for shift in 0..cases.len() {
        let mut rotated = cases.clone();
        rotated.rotate_left(shift);
        let mut expected = cpu.clone();
        expected.rotate_left(shift);
        let (result, backend) = check_mixed_batch(&chain, &rotated).unwrap();
        assert_eq!(backend, expect);
        assert_eq!(result, expected);
    }

    let ordinary = Chain::new(Network::regtest(), 1);
    let (result, backend) = check_mixed_batch(&ordinary, &pq[0..1]).unwrap();
    assert_eq!(
        result,
        [Err(Error::Invalid("ML-DSA is disabled on this chain"))]
    );
    assert_eq!(backend, BatchBackend::CpuOnly);
    let (result, backend) = check_mixed_batch(&chain, &ed[128..]).unwrap();
    assert_eq!(result.len(), 128);
    assert!(result.iter().all(Result::is_ok));
    assert_eq!(backend, BatchBackend::CpuOnly);
    assert!(check_mixed_batch(&chain, &[]).is_err());
    assert!(check_mixed_batch(&chain, &ed[..129]).is_err());
    println!(
        "mixed_controls backend={expect:?} cases={} rotations={} wrong_chain=1 cpu_only=128 batch_limits=2",
        cases.len(),
        cases.len()
    );
}

#[test]
fn mixed_witness_batches() {
    controls(BatchBackend::Cuda);
    let (chain, ed, pq, _, _) = fixture();
    for (cohort, share, dirty) in [
        ("ed_only", 0, false),
        ("pq25", 1, false),
        ("pq50", 2, false),
        ("pq50_dirty", 2, true),
        ("pq100", 4, false),
    ] {
        let mut wires = (0..COUNT)
            .map(|slot| {
                if slot % 4 < share {
                    pq[slot].clone()
                } else {
                    ed[slot].clone()
                }
            })
            .collect::<Vec<_>>();
        if dirty {
            for slot in (0..COUNT).step_by(32) {
                for index in [slot, slot + 2] {
                    wires[index] = change_output(&wires[index]);
                }
            }
        }
        for batch in [8, 32, 128] {
            for pass in 0..6 {
                let cpu = || {
                    let start = Instant::now();
                    let records = wires
                        .iter()
                        .map(|wire| validate(&chain, black_box(wire)))
                        .collect::<Vec<_>>();
                    (start.elapsed().as_secs_f64() * 1e6 / COUNT as f64, records)
                };
                let hybrid = || {
                    let start = Instant::now();
                    let mut records = Vec::with_capacity(COUNT);
                    let mut cuda_calls = 0;
                    let mut fallbacks = 0;
                    for chunk in wires.chunks(batch) {
                        let (results, backend) =
                            check_mixed_batch(&chain, black_box(chunk)).unwrap();
                        cuda_calls += usize::from(backend == BatchBackend::Cuda);
                        fallbacks += usize::from(backend == BatchBackend::CpuFallback);
                        records.extend(results);
                    }
                    (
                        start.elapsed().as_secs_f64() * 1e6 / COUNT as f64,
                        records,
                        cuda_calls,
                        fallbacks,
                    )
                };
                let ((cpu_us, expected), (hybrid_us, results, cuda_calls, fallbacks)) =
                    if pass % 2 == 0 {
                        (cpu(), hybrid())
                    } else {
                        let result = hybrid();
                        (cpu(), result)
                    };
                assert_eq!(results, expected);
                assert_eq!(cuda_calls, if share == 0 { 0 } else { COUNT / batch });
                assert_eq!(fallbacks, 0);
                let rejected = results.iter().filter(|record| record.is_err()).count();
                assert_eq!(rejected, if dirty { 16 } else { 0 });
                println!(
                    "mixed={cohort} batch={batch} pass={pass} n={COUNT} rejected={rejected} cuda_calls={cuda_calls} cpu_us={cpu_us:.3} hybrid_us={hybrid_us:.3}"
                );
            }
        }
    }
}

#[test]
#[ignore = "run with CUDA_VISIBLE_DEVICES='' to exercise CPU fallback"]
fn mixed_witness_missing_device() {
    controls(BatchBackend::CpuFallback);
}
