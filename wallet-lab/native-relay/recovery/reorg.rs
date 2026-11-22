use super::*;
use ml_dsa::{MlDsa44, Seed, SigningKey as PqKey};
use requant_consensus::pqc;
use requant_node::node::{connect, supply_audit};
use std::fs;

const FEE: u64 = 10_000;

fn mine_local(node: &Handle, owner: &Hash, payments: &[Hash]) -> Hash {
    let mut state = node.shared.lock().unwrap();
    let (template, seed) = state.new_work(owner);
    assert_eq!(
        template.txs[1..].iter().map(Tx::txid).collect::<Vec<_>>(),
        payments,
    );
    let network = state.chain.net.clone();
    let epoch = state.chain.epoch(&seed);
    let claim = mine(&network, &epoch, &template.header, 0, 20_000).unwrap();
    let digest = template.header.digest(&network.chain_id);
    assert_eq!(
        state.submit_work(&digest, claim).unwrap(),
        Some(Accepted::NewTip),
    );
    state.chain.tip()
}

fn copy_checkpoint(source: &Path, target: &Path) {
    let source = source.join("pqc-lab");
    let target = target.join("pqc-lab");
    fs::create_dir_all(&target).unwrap();
    for file in fs::read_dir(source).unwrap() {
        let file = file.unwrap();
        assert!(file.file_type().unwrap().is_file());
        fs::copy(file.path(), target.join(file.file_name())).unwrap();
    }
}

fn utxo(value: u64, owner: Hash) -> Output {
    Output { value, pkh: owner }
}

fn payment(network: &Network, key: &PqKey<MlDsa44>, input: OutPoint, output: Output) -> Tx {
    let mut tx = Tx::Transfer {
        inputs: vec![pqc::input(input, key)],
        outputs: vec![output],
    };
    pqc::sign(&mut tx, 0, key).unwrap();
    tx.check_standalone(&network.chain_id).unwrap();
    tx
}

fn audit(node: &Handle) -> serde_json::Value {
    let state = node.shared.lock().unwrap();
    let result = supply_audit(&state);
    assert_eq!(result["ok"], true);
    result
}

fn body(node: &Handle, block: impl Into<serde_json::Value>) -> serde_json::Value {
    request(node.rpc.unwrap(), "getblock", json!([block.into()])).unwrap()
}

#[test]
fn pqc_recovered_node_reorg() {
    let reference_dir = datadir("pqc-reorg-reference");
    let backup = datadir("pqc-reorg-backup");
    let recovered_dir = datadir("pqc-reorg-restored");
    let network = Network::pqc_lab();
    let legacy = SigningKey::from_bytes(&[13; 32]);
    let miner = addr(&legacy);
    let pq = PqKey::<MlDsa44>::from_seed(&Seed::from([42; 32]));
    let owner = pqc::parse_address(&pqc::address(&pq)).unwrap();
    let reference = node_on_network(&reference_dir, vec![], None, [0; 32], network.clone());

    let first = mine_local(&reference, &miner, &[]);
    for _ in 0..network.maturity {
        mine_local(&reference, &miner, &[]);
    }
    let funding = {
        let state = reference.shared.lock().unwrap();
        let tx = &state.chain.block(&first).unwrap().txs[0];
        OutPoint {
            txid: tx.txid(),
            vout: 0,
        }
    };
    let initial_value = reference
        .shared
        .lock()
        .unwrap()
        .chain
        .coin(&funding)
        .unwrap()
        .output
        .value;
    let funded_value = initial_value - FEE;
    let mut fund = Tx::Transfer {
        inputs: vec![Input::new(funding)],
        outputs: vec![utxo(funded_value, owner)],
    };
    fund.sign(&network.chain_id, &[&legacy]);
    reference
        .shared
        .lock()
        .unwrap()
        .process_tx(fund.clone(), None)
        .unwrap();
    let fork = mine_local(&reference, &miner, &[fund.txid()]);
    assert_eq!(height(&reference), 4);
    let checkpoint = audit(&reference);
    reference.shutdown();
    drop(reference);
    copy_checkpoint(&reference_dir, &backup);
    let backup_store = fs::read(backup.join("pqc-lab/blocks.dat")).unwrap();
    let backup_snapshot = fs::read(backup.join("pqc-lab/chainstate.bin")).unwrap();
    copy_checkpoint(&backup, &recovered_dir);

    let a = node_on_network(&recovered_dir, vec![], None, [0; 32], network.clone());
    let b = node_on_network(&reference_dir, vec![], None, [0; 32], network.clone());
    assert_eq!(audit(&a), checkpoint);
    assert_eq!(audit(&b), checkpoint);
    for node in [&a, &b] {
        let info = request(node.rpc.unwrap(), "getinfo", json!([])).unwrap();
        assert_eq!(info["snapshot_height"], 4);
        assert_eq!(info["tip"], hex(&fork));
        assert_eq!(info["peers"], 0);
    }

    let prev = OutPoint {
        txid: fund.txid(),
        vout: 0,
    };
    let detached = payment(&network, &pq, prev, utxo(funded_value - FEE, [14; 32]));
    let detached_coin = OutPoint {
        txid: detached.txid(),
        vout: 0,
    };
    a.shared
        .lock()
        .unwrap()
        .process_tx(detached.clone(), None)
        .unwrap();
    let detached_blocks = [
        mine_local(&a, &miner, &[detached.txid()]),
        mine_local(&a, &miner, &[]),
    ];
    {
        let state = a.shared.lock().unwrap();
        assert!(state.chain.coin(&prev).is_none());
        assert_eq!(
            state.chain.coin(&detached_coin).unwrap().output,
            utxo(funded_value - FEE, [14; 32])
        );
    }
    assert_eq!(height(&a), 6);
    let mut winner = fork;
    for _ in 0..3 {
        winner = mine_local(&b, &[15; 32], &[]);
    }
    assert_eq!(height(&b), 7);
    let started = Instant::now();
    connect(&a.shared, &b.p2p.to_string()).unwrap();
    wait("restored node to switch to the longer branch", 15, || {
        a.shared.lock().unwrap().chain.tip() == winner
    });
    let reorg_ms = started.elapsed().as_secs_f64() * 1000.0;
    assert_eq!(audit(&a), audit(&b));
    {
        let state = a.shared.lock().unwrap();
        assert_eq!(
            state.chain.coin(&prev).unwrap().output,
            utxo(funded_value, owner)
        );
        assert!(state.chain.coin(&detached_coin).is_none());
        assert!(state.mempool.contains(&detached.txid()));
    }
    for block in detached_blocks {
        assert_eq!(body(&a, hex(&block))["best_chain"], false);
    }

    let replacement = payment(&network, &pq, prev, utxo(funded_value - FEE, [16; 32]));
    let replacement_coin = OutPoint {
        txid: replacement.txid(),
        vout: 0,
    };
    b.shared
        .lock()
        .unwrap()
        .process_tx(replacement.clone(), None)
        .unwrap();
    let final_tip = mine_local(&b, &[15; 32], &[replacement.txid()]);
    wait("restored node to accept the replacement spend", 15, || {
        a.shared.lock().unwrap().chain.tip() == final_tip
    });
    let final_audit = audit(&b);
    assert_eq!(audit(&a), final_audit);
    a.shutdown();
    drop(a);

    let reopened = node_on_network(&recovered_dir, vec![], None, [0; 32], network);
    let info = request(reopened.rpc.unwrap(), "getinfo", json!([])).unwrap();
    assert_eq!(info["snapshot_height"], 8);
    assert_eq!(info["peers"], 0);
    assert_eq!(audit(&reopened), final_audit);
    for height in 1..=8 {
        assert_eq!(body(&reopened, height), body(&b, height));
    }
    for block in detached_blocks {
        assert_eq!(body(&reopened, hex(&block))["best_chain"], false);
    }
    {
        let state = reopened.shared.lock().unwrap();
        assert!(state.chain.coin(&prev).is_none() && state.chain.coin(&detached_coin).is_none());
        assert_eq!(
            state.chain.coin(&replacement_coin).unwrap().output,
            utxo(funded_value - FEE, [16; 32])
        );
        assert!(!state.mempool.contains(&detached.txid()));
    }
    assert_eq!(
        fs::read(backup.join("pqc-lab/blocks.dat")).unwrap(),
        backup_store
    );
    assert_eq!(
        fs::read(backup.join("pqc-lab/chainstate.bin")).unwrap(),
        backup_snapshot
    );
    reopened.shutdown();
    b.shutdown();
    println!(
        "{}",
        json!({
            "checkpoint_height":4, "detached_height":6, "winning_height":7, "final_height":8,
            "reorg_ms":reorg_ms, "detached_blocks":detached_blocks.map(|id| hex(&id)),
            "detached_spend":hex(&detached.txid()), "replacement_spend":hex(&replacement.txid()),
            "final_audit":final_audit, "matched_bodies_after_reopen":8, "backup_matched":true,
        })
    );
}
