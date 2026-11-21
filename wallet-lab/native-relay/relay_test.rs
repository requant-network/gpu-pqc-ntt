use super::*;
use ml_dsa::{MlDsa44, Seed, SigningKey as PqKey};
use requant_consensus::pqc;

const FEE: u64 = 10_000;

fn mine_and_sync(a: &Handle, b: &Handle, payee: &Hash, payments: &[Hash]) {
    let (height, tip) = {
        let mut state = a.shared.lock().unwrap();
        let (template, seed) = state.new_work(payee);
        let net = state.chain.net.clone();
        let epoch = state.chain.epoch(&seed);
        let included: Vec<_> = template.txs[1..].iter().map(Tx::txid).collect();
        assert_eq!(included.len(), payments.len());
        assert!(payments.iter().all(|id| included.contains(id)));
        let claim = mine(&net, &epoch, &template.header, 0, 20_000).unwrap();
        let digest = template.header.digest(&net.chain_id);
        assert_eq!(
            state.submit_work(&digest, claim).unwrap(),
            Some(Accepted::NewTip)
        );
        (state.chain.height(), state.chain.tip())
    };
    wait("peer to accept the mined block", 15, || {
        let state = b.shared.lock().unwrap();
        state.chain.height() == height && state.chain.tip() == tip
    });
}

fn relay(a: &Handle, b: &Handle, tx: &Tx) {
    let info = request(a.rpc.unwrap(), "sendtx", json!([hex(&tx.encode())])).unwrap();
    let id = tx.txid();
    let wire = tx.encode();
    assert_eq!(info, json!(hex(&id)));
    wait("payment to enter the peer mempool", 15, || {
        let state = b.shared.lock().unwrap();
        state
            .mempool
            .get(&id)
            .is_some_and(|received| received.encode() == wire)
    });
}

#[test]
fn pqc_mixed_block_relay() {
    let dir_a = datadir("pqc-relay-a");
    let dir_b = datadir("pqc-relay-b");
    let a = node_on_network(&dir_a, vec![], None, [0; 32], Network::pqc_lab());
    let b = node_on_network(
        &dir_b,
        vec![a.p2p.to_string()],
        None,
        [0; 32],
        Network::pqc_lab(),
    );
    wait("lab peer handshake", 15, || {
        a.shared.lock().unwrap().peer_count() == 1 && b.shared.lock().unwrap().peer_count() == 1
    });

    let legacy = SigningKey::from_bytes(&[13; 32]);
    let miner = addr(&legacy);
    let key = PqKey::<MlDsa44>::from_seed(&Seed::from([42; 32]));
    let owner = pqc::parse_address(&pqc::address(&key)).unwrap();
    let net = Network::pqc_lab();
    for _ in 0..=net.maturity {
        mine_and_sync(&a, &b, &miner, &[]);
    }

    let rpc_b = b.rpc.unwrap();
    let list = request(rpc_b, "utxos", json!([hex(&miner)])).unwrap();
    let coin = list
        .as_array()
        .unwrap()
        .iter()
        .find(|coin| coin["spendable"] == true)
        .unwrap();
    let op = OutPoint {
        txid: unhex(coin["txid"].as_str().unwrap())
            .unwrap()
            .try_into()
            .unwrap(),
        vout: coin["vout"].as_u64().unwrap().try_into().unwrap(),
    };
    let value = coin["value"].as_u64().unwrap();
    let tranche = (value - FEE) / 2;
    let mut migrate = Tx::Transfer {
        inputs: vec![Input::new(op)],
        outputs: vec![
            Output {
                value: tranche,
                pkh: owner,
            },
            Output {
                value: value - FEE - tranche,
                pkh: miner,
            },
        ],
    };
    migrate.sign(&net.chain_id, &[&legacy]);
    relay(&b, &a, &migrate);
    mine_and_sync(&a, &b, &miner, &[migrate.txid()]);

    let mut pq = Tx::Transfer {
        inputs: vec![pqc::input(
            OutPoint {
                txid: migrate.txid(),
                vout: 0,
            },
            &key,
        )],
        outputs: vec![Output {
            value: tranche - FEE,
            pkh: [14; 32],
        }],
    };
    pqc::sign(&mut pq, 0, &key).unwrap();
    let mut altered = pq.clone();
    if let Tx::Transfer { outputs, .. } = &mut altered {
        outputs[0].value -= 1;
    }
    let refusal = request(rpc_b, "sendtx", json!([hex(&altered.encode())])).unwrap_err();
    assert!(
        refusal.to_string().contains("bad ML-DSA signature"),
        "{refusal}"
    );
    assert_eq!(b.shared.lock().unwrap().mempool.len(), 0);
    assert_eq!(a.shared.lock().unwrap().mempool.len(), 0);

    let mut ed = Tx::Transfer {
        inputs: vec![Input::new(OutPoint {
            txid: migrate.txid(),
            vout: 1,
        })],
        outputs: vec![Output {
            value: value - FEE - tranche - FEE,
            pkh: [15; 32],
        }],
    };
    ed.sign(&net.chain_id, &[&legacy]);
    relay(&b, &a, &pq);
    relay(&b, &a, &ed);
    mine_and_sync(&a, &b, &miner, &[pq.txid(), ed.txid()]);
    assert_eq!(b.shared.lock().unwrap().mempool.len(), 0);
    assert!(request(rpc_b, "sendtx", json!([hex(&pq.encode())])).is_err());

    let final_tip = b.shared.lock().unwrap().chain.tip();
    a.stop.store(true, Ordering::Relaxed);
    b.stop.store(true, Ordering::Relaxed);
    drop(a);
    drop(b);
    let reopened = node_on_network(&dir_b, vec![], None, [0; 32], Network::pqc_lab());
    let state = reopened.shared.lock().unwrap();
    assert_eq!(state.chain.tip(), final_tip);
    for (tx, owner, value) in [
        (&pq, [14; 32], tranche - FEE),
        (&ed, [15; 32], value - FEE - tranche - FEE),
    ] {
        let coin = state
            .chain
            .coin(&OutPoint {
                txid: tx.txid(),
                vout: 0,
            })
            .unwrap();
        assert_eq!(coin.output, Output { value, pkh: owner });
    }
    assert!(Network::by_name(net.name).is_none());
    reopened.stop.store(true, Ordering::Relaxed);
    println!("relay_transport=loopback_tcp nodes=2 ml_dsa_wire_bytes={} ed25519_wire_bytes={} confirmed_height={} refused=2 reopened_tip=matched",
        pq.encode().len(), ed.encode().len(), state.chain.height());
}
