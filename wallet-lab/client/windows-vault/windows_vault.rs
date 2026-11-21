use super::*;
use ml_dsa::Keypair;
use std::env;
use std::fs::{self, File};
use std::io::{self, Read};
use std::path::{Path, PathBuf};
use std::process::{Command, Output as ChildOutput};

mod dpapi {
    include!("pqc_dpapi.inc");
}

const BLOB: &str = "REQUANT_DPAPI_LAB_BLOB";
const WORK: &str = "REQUANT_DPAPI_LAB_WORK";
const ARTIFACT: &str = "REQUANT_DPAPI_LAB_ARTIFACT";
const PHASE: &str = "REQUANT_DPAPI_LAB_PHASE";

fn read_bounded(path: &Path, maximum: usize) -> io::Result<Vec<u8>> {
    let mut bytes = Vec::new();
    File::open(path)?
        .take((maximum + 1) as u64)
        .read_to_end(&mut bytes)?;
    if bytes.len() > maximum {
        return Err(dpapi::invalid("wallet lab input too large"));
    }
    Ok(bytes)
}

fn fixed<const N: usize>(path: &Path) -> io::Result<[u8; N]> {
    read_bounded(path, N)?
        .try_into()
        .map_err(|_| dpapi::invalid("invalid wallet lab input length"))
}

fn public(key: &SigningKey<MlDsa44>) -> [u8; 1312] {
    key.verifying_key().encode().into()
}

fn random_backup() -> io::Result<Zeroizing<[u8; 33]>> {
    let mut backup = Zeroizing::new([0; 33]);
    backup[0] = 1;
    getrandom::fill(&mut backup[1..]).map_err(io::Error::other)?;
    Ok(backup)
}

fn initialize(blob: &Path, artifact: &Path) -> io::Result<()> {
    let backup = random_backup()?;
    let key = restore(&backup[..]).map_err(dpapi::invalid)?;
    dpapi::seal(blob, &backup[..])?;
    dpapi::write_new(artifact, &public(&key))
}

fn sign_from_disk(work: &Path, blob: &Path, artifact: &Path) -> io::Result<()> {
    let expected: Hash = fixed(&work.join("expected-owner.bin"))?;
    let unsigned = read_bounded(&work.join("unsigned.bin"), 4_096)?;
    let mut tx = Tx::decode_exact(&unsigned).map_err(io::Error::other)?;
    tx.check_shape().map_err(io::Error::other)?;
    let Tx::Transfer { inputs, .. } = &tx else {
        return Err(dpapi::invalid("wallet lab expects a transfer"));
    };
    let [input] = inputs.as_slice() else {
        return Err(dpapi::invalid("wallet lab expects one input"));
    };
    let Unlock::MlDsa44(witness) = &input.unlock else {
        return Err(dpapi::invalid("wallet lab expects an ML-DSA input"));
    };
    if pqc::owner(&witness.public) != expected
        || witness.signature != [0; 2420]
        || input.pubkey != [0; 32]
        || input.sig != [0; 64]
        || input.anyone_can_pay
    {
        return Err(dpapi::invalid(
            "unsigned transaction is outside the wallet fixture",
        ));
    }

    let open_start = Instant::now();
    let backup = dpapi::unseal(blob)?;
    let key = restore(backup.bytes()?).map_err(dpapi::invalid)?;
    drop(backup);
    if pqc::owner(&public(&key)) != expected {
        return Err(dpapi::invalid("wallet owner mismatch"));
    }
    let open_us = open_start.elapsed().as_secs_f64() * 1e6;
    let sign_start = Instant::now();
    pqc::sign(&mut tx, 0, &key).map_err(io::Error::other)?;
    let wire = tx.encode();
    let sign_us = sign_start.elapsed().as_secs_f64() * 1e6;
    drop(key);
    tx.check_standalone(&Network::pqc_lab().chain_id)
        .map_err(io::Error::other)?;
    dpapi::write_new(artifact, &wire)?;
    println!("wallet_open_us={open_us:.3} sign_encode_us={sign_us:.3}");
    Ok(())
}

fn lab_path(name: &str) -> io::Result<PathBuf> {
    env::var_os(name)
        .map(PathBuf::from)
        .ok_or_else(|| dpapi::invalid("missing wallet lab path"))
}

#[test]
#[ignore = "called by the DPAPI wallet lab with disposable fixtures"]
fn dpapi_child() -> io::Result<()> {
    let blob = lab_path(BLOB)?;
    let artifact = lab_path(ARTIFACT)?;
    match env::var(PHASE).as_deref() {
        Ok("initialize") => initialize(&blob, &artifact),
        Ok("sign") => sign_from_disk(&lab_path(WORK)?, &blob, &artifact),
        _ => Err(dpapi::invalid("invalid wallet lab phase")),
    }
}

struct Scratch(PathBuf);

impl Scratch {
    fn new() -> Self {
        let mut nonce = [0u8; 16];
        getrandom::fill(&mut nonce).unwrap();
        let directory =
            env::temp_dir().join(format!("requant-dpapi-fixture-{}", hex::encode(nonce)));
        fs::create_dir(&directory).unwrap();
        Self(directory)
    }
}

impl Drop for Scratch {
    fn drop(&mut self) {
        if let Ok(files) = fs::read_dir(&self.0) {
            for file in files.flatten() {
                let _ = fs::remove_file(file.path());
            }
        }
        let _ = fs::remove_dir(&self.0);
    }
}

fn child(work: &Path, phase: &str, blob: &Path, artifact: &Path) -> ChildOutput {
    Command::new(env::current_exe().unwrap())
        .args([
            "--exact",
            "windows_vault::dpapi_child",
            "--ignored",
            "--nocapture",
        ])
        .env(PHASE, phase)
        .env(WORK, work)
        .env(BLOB, blob)
        .env(ARTIFACT, artifact)
        .output()
        .unwrap()
}

fn accepted(output: ChildOutput) -> String {
    assert!(
        output.status.success(),
        "wallet child failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8(output.stdout).unwrap()
}

#[test]
fn windows_vault_restarts_and_fails_closed() {
    let scratch = Scratch::new();
    let work = scratch.0.as_path();
    let blob = work.join("wallet.sealed");
    let pub_path = work.join("wallet-public.bin");
    accepted(child(work, "initialize", &blob, &pub_path));
    let protected = fs::read(&blob).unwrap();
    println!("sealed_bytes={}", protected.len());
    let public: [u8; 1312] = fixed(&pub_path).unwrap();
    assert!(!child(work, "initialize", &blob, &pub_path).status.success());
    assert_eq!(fs::read(&blob).unwrap(), protected);
    assert_eq!(fixed::<1312>(&pub_path).unwrap(), public);
    let copied = work.join("backup-copy.sealed");
    dpapi::write_new(&copied, &protected).unwrap();

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
    let owner = pqc::owner(&public);
    let value = chain.coin(&coin).unwrap().output.value - FEE;
    let migration = legacy(
        &chain.net.chain_id,
        &legacy_key,
        coin,
        vec![Output { value, pkh: owner }],
    );
    let funded = OutPoint {
        txid: migration.txid(),
        vout: 0,
    };
    extend(&mut chain, &payee, vec![migration]);
    dpapi::write_new(
        &work.join("expected-owner.bin"),
        &chain.coin(&funded).unwrap().output.pkh,
    )
    .unwrap();
    let unsigned = Tx::Transfer {
        inputs: vec![Input {
            unlock: Unlock::MlDsa44(Box::new(pqc::Witness {
                public,
                signature: [0; 2420],
            })),
            ..Input::new(funded)
        }],
        outputs: vec![Output {
            value: value - FEE,
            pkh: payee,
        }],
    };
    dpapi::write_new(&work.join("unsigned.bin"), &unsigned.encode()).unwrap();

    let mut reference = None;
    for trial in 1..=5 {
        let wire_path = work.join(format!("spend-{trial}.bin"));
        let started = Instant::now();
        let stdout = accepted(child(
            work,
            "sign",
            if trial == 5 { &copied } else { &blob },
            &wire_path,
        ));
        let process_ms = started.elapsed().as_secs_f64() * 1e3;
        let timing = stdout
            .lines()
            .find(|line| line.starts_with("wallet_open_us="))
            .unwrap();
        let wire = read_bounded(&wire_path, 4_096).unwrap();
        assert_eq!(validate(&chain, &wire), Ok(FEE));
        assert_eq!(Tx::decode_exact(&wire).unwrap().txid(), unsigned.txid());
        assert_eq!(reference.get_or_insert_with(|| wire.clone()), &wire);
        println!(
            "trial={trial} {timing} process_ms={process_ms:.3} wire_bytes={}",
            wire.len()
        );
    }

    let mut damaged = protected.clone();
    *damaged.last_mut().unwrap() ^= 1;
    dpapi::write_new(&work.join("bit-flip.sealed"), &damaged).unwrap();
    dpapi::write_new(
        &work.join("truncated.sealed"),
        &protected[..protected.len() - 12],
    )
    .unwrap();
    let mut wrong_format = protected.clone();
    wrong_format[0] ^= 1;
    dpapi::write_new(&work.join("wrong-format.sealed"), &wrong_format).unwrap();
    dpapi::write_new(&work.join("oversize.sealed"), &[0; 4_097]).unwrap();
    dpapi::seal_for_purpose(
        &work.join("wrong-purpose.sealed"),
        &random_backup().unwrap()[..],
        b"other lab purpose",
    )
    .unwrap();
    accepted(child(
        work,
        "initialize",
        &work.join("other-wallet.sealed"),
        &work.join("other-public.bin"),
    ));
    let unwanted = work.join("unwanted-spend.bin");
    for case in [
        "bit-flip",
        "truncated",
        "wrong-format",
        "oversize",
        "wrong-purpose",
        "other-wallet",
        "missing",
    ] {
        let output = child(
            work,
            "sign",
            &work.join(format!("{case}.sealed")),
            &unwanted,
        );
        assert!(
            !output.status.success(),
            "invalid {case} fixture produced a successful child"
        );
        assert!(!unwanted.exists());
        if case == "other-wallet" {
            assert!(String::from_utf8_lossy(&output.stderr).contains("wallet owner mismatch"));
        }
        println!("refused={case} signed_output=false");
    }

    let wire = reference.unwrap();
    let mut changed = Tx::decode_exact(&wire).unwrap();
    let Tx::Transfer { outputs, .. } = &mut changed else {
        unreachable!()
    };
    outputs[0].value -= 1;
    assert!(validate(&chain, &changed.encode()).is_err());
    extend(&mut chain, &payee, vec![Tx::decode_exact(&wire).unwrap()]);
    assert!(chain.coin(&funded).is_none());
    println!("wallet_restarts=5 same_signed_bytes=true protected_copy=true no_overwrite=true confirmed=true altered_payment_refused=true");
}
