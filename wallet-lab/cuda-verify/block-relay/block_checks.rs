use super::*;

fn candidate(chain: &mut Chain, payee: &Hash, txs: Vec<Tx>, fees: u64) -> Block {
    let tip = chain.tip();
    let height = chain.height() + 1;
    let mut b =
        chain.template_with_fees(&tip, payee, txs, fees, chain.net.genesis_time + 60 * height);
    let seed = chain.epoch_seed(&tip, height);
    let epoch = chain.epoch(&seed);
    b.claim = mine(&chain.net, &epoch, &b.header, 0, 1000).unwrap();
    b
}

#[test]
fn ml_dsa_batch_preserves_block_signature_and_utxo_rules() {
    // These public seeds belong exclusively to the disposable lab chain.
    let ed = EdKey::from_bytes(&[31; 32]);
    let pq = SigningKey::<MlDsa44>::from_seed(&Seed::from([32; 32]));
    let ed_owner = pkh(&ed.verifying_key().to_bytes());
    let pq_owner = pqc::parse_address(&pqc::address(&pq)).unwrap();
    let mut chain = Chain::new(Network::pqc_lab(), 1);
    let first = block(&mut chain, &ed_owner, vec![]);
    let coin = OutPoint {
        txid: first.txs[0].txid(),
        vout: 0,
    };
    chain.accept(first, NOW).unwrap();
    for _ in 0..chain.net.maturity {
        extend(&mut chain, &ed_owner, vec![]);
    }
    let amount = 5 * FEE;
    let change = chain.coin(&coin).unwrap().output.value - amount - FEE;
    let funding = legacy(
        &chain.net.chain_id,
        &ed,
        coin,
        vec![
            Output {
                value: amount,
                pkh: pq_owner,
            },
            Output {
                value: change,
                pkh: ed_owner,
            },
        ],
    );
    let prev = OutPoint {
        txid: funding.txid(),
        vout: 0,
    };
    let ed_prev = OutPoint {
        txid: funding.txid(),
        vout: 1,
    };
    extend(&mut chain, &ed_owner, vec![funding]);
    let before = chain.tip();
    let parent = sign(
        &pq,
        prev,
        vec![Output {
            value: amount - FEE,
            pkh: pq_owner,
        }],
    );
    let child_prev = OutPoint {
        txid: parent.txid(),
        vout: 0,
    };
    let child = sign(
        &pq,
        child_prev,
        vec![Output {
            value: amount - 2 * FEE,
            pkh: pq_owner,
        }],
    );
    let ed_payment = legacy(
        &chain.net.chain_id,
        &ed,
        ed_prev,
        vec![Output {
            value: change - FEE,
            pkh: ed_owner,
        }],
    );
    let mut changed = child.clone();
    let Tx::Transfer { outputs, .. } = &mut changed else {
        unreachable!()
    };
    outputs[0].value += 1;
    let invalid = candidate(
        &mut chain,
        &ed_owner,
        vec![parent.clone(), changed, ed_payment.clone()],
        3 * FEE - 1,
    );
    let invalid_id = invalid.id(&chain.net);
    assert_eq!(
        chain.accept(invalid, NOW),
        Err(Error::Invalid("bad ML-DSA signature"))
    );
    assert_eq!(chain.tip(), before);
    assert!(chain.block(&invalid_id).is_none());
    assert!(chain.coin(&prev).is_some());

    let duplicate = sign(
        &pq,
        prev,
        vec![Output {
            value: amount - 2 * FEE,
            pkh: pq_owner,
        }],
    );
    let bad_spend = candidate(
        &mut chain,
        &ed_owner,
        vec![parent.clone(), duplicate],
        3 * FEE,
    );
    assert!(chain.accept(bad_spend, NOW).is_err());
    assert_eq!(chain.tip(), before);
    assert!(chain.coin(&prev).is_some());

    let final_coin = OutPoint {
        txid: child.txid(),
        vout: 0,
    };
    let valid = candidate(
        &mut chain,
        &ed_owner,
        vec![parent, child, ed_payment],
        3 * FEE,
    );
    let valid_id = valid.id(&chain.net);
    chain.accept(valid, NOW).unwrap();
    assert_eq!(chain.tip(), valid_id);
    assert_eq!(
        chain.coin(&final_coin).unwrap().output.value,
        amount - 2 * FEE
    );
    assert!(chain.coin(&prev).is_none() && chain.coin(&child_prev).is_none());
    println!("block_controls=altered_signature_rejected,duplicate_spend_rejected,same_block_dependency_accepted");
}
