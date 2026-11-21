//! Run the unpatched node's wire and consensus code on the isolated PQC fixture ID.
use requant_consensus::params::{tagged, Network};
use requant_node::node::{start, Config};
use serde_json::json;
use std::{
    io::{self, BufRead},
    net::SocketAddr,
    path::PathBuf,
    time::Duration,
};

fn main() {
    let args: Vec<_> = std::env::args().skip(1).collect();
    let (directory, connect) = match args.as_slice() {
        [directory] => (directory, vec![]),
        [directory, peer] => {
            let peer: SocketAddr = peer.parse().expect("valid peer socket");
            assert!(peer.ip().is_loopback());
            (directory, vec![peer.to_string()])
        }
        _ => panic!("expected DATADIR [PEER]"),
    };
    let datadir = PathBuf::from(directory);
    assert!(datadir.is_absolute());

    let mut net = Network::regtest();
    net.name = "pqc-lab";
    net.chain_id = tagged("requant/chain", &[b"pqc-lab"]);
    let loopback: SocketAddr = "127.0.0.1:0".parse().unwrap();
    let node = start(Config {
        net,
        datadir,
        listen: loopback,
        rpc: Some(loopback),
        connect,
        mine_to: None,
        mine_interval: Duration::from_millis(333),
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
    println!(
        "{}",
        json!({"ready": "legacy", "rpc": node.rpc.unwrap().port()})
    );

    let mut lines = io::stdin().lock().lines();
    assert_eq!(lines.next().unwrap().unwrap(), "stop");
    node.shutdown();
}
