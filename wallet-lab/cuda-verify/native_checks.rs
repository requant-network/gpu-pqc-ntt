use super::*;
use requant_consensus::pqc::check_cuda_batch;

fn check_batch(chain: &Chain, wires: &[Vec<u8>]) -> Result<(), Error> {
    let transactions = wires
        .iter()
        .map(|wire| Tx::decode_exact(wire))
        .collect::<Result<Vec<_>, _>>()?;
    check_cuda_batch(&chain.net.chain_id, &transactions)?;
    for tx in &transactions {
        chain.check_spend(tx)?;
    }
    Ok(())
}

pub(super) fn trial(chain: &Chain, key: &SigningKey<MlDsa44>, prev: OutPoint, payee: Hash, value: u64) {
    let wires = (0..256)
        .map(|i| {
            sign(
                key,
                prev,
                vec![Output {
                    value: value - i,
                    pkh: payee,
                }],
            )
            .encode()
        })
        .collect::<Vec<_>>();
    let start = Instant::now();
    check_batch(chain, &wires[..1]).unwrap();
    println!("cuda_first_call_ms={:.3}", start.elapsed().as_secs_f64() * 1000.0);
    let mut changed = Tx::decode_exact(&wires[1]).unwrap();
    let Tx::Transfer { outputs, .. } = &mut changed else {
        unreachable!()
    };
    outputs[0].value += 1;
    let wire = changed.encode();
    assert!(validate(chain, &wire).is_err());
    assert!(check_batch(chain, &[wires[0].clone(), wire]).is_err());
    assert!(check_cuda_batch(&Network::regtest().chain_id, &[Tx::decode_exact(&wires[0]).unwrap()]).is_err());
    println!("cuda_controls=native_valid,changed_output_in_batch,wrong_chain");

    bench("shared_key", chain, &wires);
    distinct_keys_trial();
}

fn distinct_keys_trial() {
    let legacy_key = EdKey::from_bytes(&[1; 32]);
    let payee = pkh(&legacy_key.verifying_key().to_bytes());
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

    let value = 10 * FEE;
    let keys = (0..256)
        .map(|id| {
            // These public fixture seeds are used exclusively on the disposable lab chain.
            let mut seed = Seed::default();
            seed[0] = id as u8;
            SigningKey::<MlDsa44>::from_seed(&seed)
        })
        .collect::<Vec<_>>();
    let mut outputs = keys
        .iter()
        .map(|key| Output {
            value,
            pkh: pqc::parse_address(&pqc::address(key)).unwrap(),
        })
        .collect::<Vec<_>>();
    let unique_owners = outputs
        .iter()
        .map(|output| output.pkh)
        .collect::<std::collections::BTreeSet<_>>();
    assert_eq!(unique_owners.len(), keys.len());
    outputs.push(Output {
        value: chain.coin(&coin).unwrap().output.value - (value * keys.len() as u64) - FEE,
        pkh: payee,
    });
    let funding = legacy(&chain.net.chain_id, &legacy_key, coin, outputs);
    let txid = funding.txid();
    extend(&mut chain, &payee, vec![funding]);
    let wires = keys
        .iter()
        .enumerate()
        .map(|(i, key)| {
            sign(
                key,
                OutPoint { txid, vout: i as u32 },
                vec![Output {
                    value: value - FEE,
                    pkh: payee,
                }],
            )
            .encode()
        })
        .collect::<Vec<_>>();
    bench("distinct_keys_and_utxos", &chain, &wires);
}

fn bench(cohort: &str, chain: &Chain, wires: &[Vec<u8>]) {
    for batch in [1, 8, 32, 128] {
        for pass in 0..6 {
            let cpu = || {
                let start = Instant::now();
                for wire in wires {
                    black_box(validate(chain, black_box(wire)).unwrap());
                }
                start.elapsed().as_secs_f64() * 1e6 / wires.len() as f64
            };
            let gpu = || {
                let start = Instant::now();
                for group in wires.chunks(batch) {
                    check_batch(chain, black_box(group)).unwrap();
                }
                start.elapsed().as_secs_f64() * 1e6 / wires.len() as f64
            };
            let (cpu_us, gpu_us) = if pass % 2 == 0 {
                (cpu(), gpu())
            } else {
                let gpu_us = gpu();
                (cpu(), gpu_us)
            };
            println!(
                "cohort={cohort} cuda_batch={batch} pass={pass} n={} cpu_us={cpu_us:.3} gpu_us={gpu_us:.3}",
                wires.len()
            );
        }
    }
}

#[test]
#[ignore = "run with CUDA_VISIBLE_DEVICES='' to test a failed backend"]
fn missing_device_is_error() {
    let mut output = vec![0u32; 4 * 256];
    let input = vec![0u32; 25 * 256];
    assert!(pqc::cuda_reconstruct(&input, &mut output, 1).is_err());
}
