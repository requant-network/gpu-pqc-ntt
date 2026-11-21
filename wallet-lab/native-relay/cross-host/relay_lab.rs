//! A pinned, loopback-only native PQC relay fixture. The node selector remains unchanged.
use ed25519_dalek::SigningKey as EdKey;
use ml_dsa::{MlDsa44, Seed, SigningKey as PqKey};
use requant_consensus::{
    chain::{mine, Accepted},
    params::Network,
    pqc,
    tx::{pkh, Hash, Input, OutPoint, Output, Tx},
};
use requant_node::{
    node::{start, supply_audit, Config, Handle},
    rpc::hex,
};
use serde_json::json;
use std::{
    io::{self, BufRead},
    net::SocketAddr,
    path::PathBuf,
    sync::atomic::Ordering,
    time::{Duration, Instant},
};

const PQ_COUNT: usize = 64;
const ED_COUNT: usize = 16;
const FEE: u64 = 10_000;
const BLOCK_PERIOD: Duration = Duration::from_millis(333);

enum Signer {
    Pq(Box<PqKey<MlDsa44>>),
    Ed(EdKey),
}

impl Signer {
    fn owner(&self) -> Hash {
        match self {
            Self::Pq(key) => pqc::parse_address(&pqc::address(key)).unwrap(),
            Self::Ed(key) => pkh(&key.verifying_key().to_bytes()),
        }
    }

    fn spend(&self, prev: OutPoint, output: Output, net: &Network) -> Tx {
        let input = match self {
            Self::Pq(key) => pqc::input(prev, key),
            Self::Ed(_) => Input::new(prev),
        };
        let mut tx = Tx::Transfer {
            inputs: vec![input],
            outputs: vec![output],
        };
        match self {
            Self::Pq(key) => pqc::sign(&mut tx, 0, key).unwrap(),
            Self::Ed(key) => tx.sign(&net.chain_id, &[key]),
        }
        tx
    }
}

fn mine_next(node: &Handle, payee: &Hash, transfers: usize) -> OutPoint {
    let mut state = node.shared.lock().unwrap();
    let (block, seed) = state.new_work(payee);
    assert_eq!(block.txs.len(), transfers + 1);
    let net = state.chain.net.clone();
    let epoch = state.chain.epoch(&seed);
    let claim = mine(&net, &epoch, &block.header, 0, 20_000).unwrap();
    let digest = block.header.digest(&net.chain_id);
    assert_eq!(
        state.submit_work(&digest, claim).unwrap(),
        Some(Accepted::NewTip)
    );

    let height = state.chain.height();
    let id = state.chain.tip();
    let accepted = state.chain.block(&id).unwrap();
    let wire = accepted.encode();
    println!(
        "{}",
        json!({
            "height": height,
            "tip": hex(&id),
            "block_sha256": hex(&tnet::sha256::sha256(&wire)),
            "block_bytes": wire.len(),
            "transfers": transfers,
        "peers": state.peer_count(),
        })
    );
    OutPoint {
        txid: accepted.txs[0].txid(),
        vout: 0,
    }
}

fn drive(node: &Handle, commands: &mut impl Iterator<Item = io::Result<String>>) {
    let net = Network::pqc_lab();
    let miner = Signer::Ed(EdKey::from_bytes(&[0xf0; 32]));
    let payee = miner.owner();
    assert_eq!(commands.next().unwrap().unwrap(), "fund");

    let first = mine_next(node, &payee, 0);
    for _ in 0..net.maturity {
        mine_next(node, &payee, 0);
    }
    let signers: Vec<_> = (0..PQ_COUNT)
        .map(|i| Signer::Pq(Box::new(PqKey::from_seed(&Seed::from([i as u8; 32])))))
        .chain((0..ED_COUNT).map(|i| Signer::Ed(EdKey::from_bytes(&[(i + 64) as u8; 32]))))
        .collect();
    let owners: Vec<_> = signers.iter().map(Signer::owner).collect();
    let value = node
        .shared
        .lock()
        .unwrap()
        .chain
        .coin(&first)
        .unwrap()
        .output
        .value;
    let mut amount = (value - FEE) / owners.len() as u64;
    let mut funding = Tx::Transfer {
        inputs: vec![Input::new(first)],
        outputs: owners
            .iter()
            .map(|&pkh| Output { value: amount, pkh })
            .collect(),
    };
    let Signer::Ed(miner) = miner else {
        unreachable!()
    };
    funding.sign(&net.chain_id, &[&miner]);
    let funding_id = node
        .shared
        .lock()
        .unwrap()
        .process_tx(funding, None)
        .unwrap();
    mine_next(node, &payee, 1);

    let mut unspent: Vec<_> = (0..owners.len())
        .map(|i| OutPoint {
            txid: funding_id,
            vout: i as u32,
        })
        .collect();
    for (phase, blocks) in [("online", 30), ("offline", 12), ("resumed", 18)] {
        assert_eq!(commands.next().unwrap().unwrap(), phase);
        for _ in 0..blocks {
            let start = Instant::now();
            amount -= FEE;
            for ((signer, &pkh), prev) in signers.iter().zip(&owners).zip(&mut unspent) {
                let tx = signer.spend(*prev, Output { value: amount, pkh }, &net);
                let id = node.shared.lock().unwrap().process_tx(tx, None).unwrap();
                *prev = OutPoint { txid: id, vout: 0 };
            }
            mine_next(node, &payee, owners.len());
            std::thread::sleep(BLOCK_PERIOD.saturating_sub(start.elapsed()));
        }
    }
    let state = node.shared.lock().unwrap();
    assert_eq!(
        unspent
            .iter()
            .filter(|op| state.chain.coin(op).is_some())
            .count(),
        owners.len()
    );
    println!("{}", json!({"audit": supply_audit(&state)}));
}

fn main() {
    let args: Vec<_> = std::env::args().skip(1).collect();
    let (role, directory, connect) = match args.as_slice() {
        [role, directory] if role == "produce" => (role, directory, vec![]),
        [role, directory] if role == "receive" => (role, directory, vec![]),
        [role, directory, peer] if role == "receive" => {
            let peer: SocketAddr = peer.parse().expect("valid peer socket");
            assert!(peer.ip().is_loopback());
            (role, directory, vec![peer.to_string()])
        }
        _ => panic!("expected produce|receive DATADIR [RECEIVER_PEER]"),
    };
    let datadir = PathBuf::from(directory);
    assert!(datadir.is_absolute());
    let loopback: SocketAddr = "127.0.0.1:0".parse().unwrap();
    let node = start(Config {
        net: Network::pqc_lab(),
        datadir,
        listen: loopback,
        rpc: Some(loopback),
        connect,
        mine_to: None,
        mine_interval: BLOCK_PERIOD,
        threads: 1,
        max_reorg: 100,
        rpc_token: None,
        rpc_cookie: false,
        reject_dust_outputs: false,
        peer_interval: Duration::from_millis(100),
        discover: false,
        explorer: None,
        pool: None,
        auto_update: false,
        release_key: [0; 32],
        notify: Default::default(),
        max_upload: None,
        faucet: None,
    })
    .unwrap();
    let mut commands = io::stdin().lock().lines();
    println!(
        "{}",
        json!({"ready": role, "p2p": node.p2p.port(), "rpc": node.rpc.unwrap().port()})
    );
    if role == "produce" {
        drive(&node, &mut commands);
    }
    assert_eq!(commands.next().unwrap().unwrap(), "stop");
    node.stop.store(true, Ordering::Relaxed);
}
