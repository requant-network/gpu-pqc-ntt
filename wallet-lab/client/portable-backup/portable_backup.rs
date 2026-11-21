use super::*;
use argon2::{Algorithm, Argon2, Block as ArgonBlock, Params, Version};
use chacha20poly1305::{aead::AeadInPlace, KeyInit, Tag, XChaCha20Poly1305, XNonce};
use ml_dsa::Keypair;
use std::{
    env,
    fs::File,
    io::{self, Read, Write},
    path::{Path, PathBuf},
};

const MAGIC: &[u8; 8] = b"RQPL-PB1";
const SALT: std::ops::Range<usize> = 8..24;
const NONCE: std::ops::Range<usize> = 24..48;
const OWNER: std::ops::Range<usize> = 48..80;
const HEADER: usize = 80;
const SEED: usize = 33;
const ENVELOPE: usize = HEADER + SEED + 16;
const MEMORY_KIB: u32 = 65_536;

fn invalid(reason: &'static str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, reason)
}

fn wallet_owner(key: &SigningKey<MlDsa44>) -> Hash {
    pqc::owner(&key.verifying_key().encode().into())
}

fn cipher(code: &[u8; 32], salt: &[u8]) -> io::Result<XChaCha20Poly1305> {
    let params = Params::new(MEMORY_KIB, 3, 4, Some(32)).unwrap();
    let block_count = params.block_count();
    let mut memory = Zeroizing::new(Vec::new());
    memory.try_reserve_exact(block_count).map_err(|_| {
        io::Error::new(io::ErrorKind::OutOfMemory, "Argon2id workspace unavailable")
    })?;
    memory.resize_with(block_count, ArgonBlock::default);
    let mut key = Zeroizing::new([0; 32]);
    Argon2::new(Algorithm::Argon2id, Version::V0x13, params)
        .hash_password_into_with_memory(code, salt, &mut key[..], &mut memory[..])
        .map_err(|_| invalid("backup key derivation failed"))?;
    Ok(XChaCha20Poly1305::new_from_slice(&key[..]).unwrap())
}

fn seal(code: &[u8; 32], owner: &Hash, backup: &[u8; SEED]) -> io::Result<[u8; ENVELOPE]> {
    let mut envelope = [0; ENVELOPE];
    envelope[..MAGIC.len()].copy_from_slice(MAGIC);
    getrandom::fill(&mut envelope[SALT.start..NONCE.end]).map_err(io::Error::other)?;
    envelope[OWNER].copy_from_slice(owner);
    let (header, payload) = envelope.split_at_mut(HEADER);
    let (ciphertext, tag_bytes) = payload.split_at_mut(SEED);
    let mut plaintext = Zeroizing::new(*backup);
    let tag = cipher(code, &header[SALT])?
        .encrypt_in_place_detached(
            XNonce::from_slice(&header[NONCE]),
            header,
            &mut plaintext[..],
        )
        .map_err(|_| invalid("backup encryption failed"))?;
    ciphertext.copy_from_slice(&plaintext[..]);
    tag_bytes.copy_from_slice(&tag);
    Ok(envelope)
}

fn open(code: &[u8; 32], owner: &Hash, envelope: &[u8]) -> io::Result<SigningKey<MlDsa44>> {
    if envelope.len() != ENVELOPE || !envelope.starts_with(MAGIC) {
        return Err(invalid("unsupported backup envelope"));
    }
    if envelope[OWNER] != *owner {
        return Err(invalid("backup owner header mismatch"));
    }
    let (header, payload) = envelope.split_at(HEADER);
    let (ciphertext, tag) = payload.split_at(SEED);
    let mut plaintext = Zeroizing::new(<[u8; SEED]>::try_from(ciphertext).unwrap());
    cipher(code, &header[SALT])?
        .decrypt_in_place_detached(
            XNonce::from_slice(&header[NONCE]),
            header,
            &mut plaintext[..],
            Tag::from_slice(tag),
        )
        .map_err(|_| invalid("backup authentication failed"))?;
    let key = restore(&plaintext[..]).map_err(invalid)?;
    if wallet_owner(&key) != *owner {
        return Err(invalid("restored key owner mismatch"));
    }
    Ok(key)
}

fn sign_and_confirm(key: &SigningKey<MlDsa44>, owner: Hash) -> Vec<u8> {
    let legacy_key = EdKey::from_bytes(&[1; 32]);
    let legacy_owner = pkh(&legacy_key.verifying_key().to_bytes());
    let mut chain = Chain::new(Network::pqc_lab(), 1);
    let first = block(&mut chain, &legacy_owner, vec![]);
    let first_coin = OutPoint {
        txid: first.txs[0].txid(),
        vout: 0,
    };
    chain.accept(first, NOW).unwrap();
    for _ in 0..chain.net.maturity {
        extend(&mut chain, &legacy_owner, vec![]);
    }

    let value = chain.coin(&first_coin).unwrap().output.value - FEE;
    let migration = legacy(
        &chain.net.chain_id,
        &legacy_key,
        first_coin,
        vec![Output { value, pkh: owner }],
    );
    let funded = OutPoint {
        txid: migration.txid(),
        vout: 0,
    };
    extend(&mut chain, &legacy_owner, vec![migration]);
    let tx = sign(
        key,
        funded,
        vec![Output {
            value: value - FEE,
            pkh: legacy_owner,
        }],
    );
    let wire = tx.encode();
    assert_eq!(validate(&chain, &wire), Ok(FEE));
    extend(&mut chain, &legacy_owner, vec![tx]);
    assert!(chain.coin(&funded).is_none());
    wire
}

fn write_new(directory: &Path, name: &str, bytes: &[u8]) -> io::Result<()> {
    let mut file = File::create_new(directory.join(name))?;
    file.write_all(bytes)?;
    file.sync_all()
}

#[test]
#[ignore = "cross-host fixture requires a private recovery code on stdin"]
fn portable_child() -> io::Result<()> {
    let directory = PathBuf::from(
        env::var_os("RQPL_PORTABLE_DIR").ok_or_else(|| invalid("missing fixture directory"))?,
    );
    let mut code = Zeroizing::new([0; 32]);
    let mut stdin = io::stdin().lock();
    stdin.read_exact(&mut code[..])?;
    if stdin.read(&mut Zeroizing::new([0; 1])[..])? != 0 {
        return Err(invalid("unexpected recovery data"));
    }

    match env::var("RQPL_PORTABLE_PHASE").as_deref() {
        Ok("export") => {
            let mut backup = Zeroizing::new([0; SEED]);
            backup[0] = 1;
            getrandom::fill(&mut backup[1..]).map_err(io::Error::other)?;
            let key = restore(&backup[..]).map_err(invalid)?;
            let owner = wallet_owner(&key);
            let start = Instant::now();
            let envelope = seal(&code, &owner, &backup)?;
            let elapsed = start.elapsed().as_secs_f64() * 1_000.0;
            drop(backup);
            drop(code);
            let wire = sign_and_confirm(&key, owner);
            write_new(&directory, "wallet.backup", &envelope)?;
            write_new(&directory, "donor.bin", &wire)?;
            write_new(&directory, "owner.bin", &owner)?;
            println!(
                "export_ms={elapsed:.3} envelope_bytes={} confirmed_wire_bytes={}",
                envelope.len(),
                wire.len()
            );
        }
        Ok("restore") => {
            let owner_text =
                env::var("RQPL_PORTABLE_OWNER").map_err(|_| invalid("missing owner pin"))?;
            let mut owner = [0; 32];
            hex::decode_to_slice(owner_text, &mut owner)
                .map_err(|_| invalid("invalid owner pin"))?;
            let mut envelope = Vec::new();
            File::open(directory.join("wallet.backup"))?
                .take(ENVELOPE as u64 + 1)
                .read_to_end(&mut envelope)?;
            let start = Instant::now();
            let key = open(&code, &owner, &envelope)?;
            let elapsed = start.elapsed().as_secs_f64() * 1_000.0;
            drop(code);
            let wire = sign_and_confirm(&key, owner);
            write_new(&directory, "restored.bin", &wire)?;
            println!(
                "restore_ms={elapsed:.3} confirmed_wire_bytes={}",
                wire.len()
            );
        }
        _ => return Err(invalid("unknown fixture phase")),
    }
    Ok(())
}

#[test]
fn portable_backup_rejects_bad_input() {
    let mut backup = Zeroizing::new([1; SEED]);
    let code = Zeroizing::new([2; 32]);
    let key = restore(&backup[..]).unwrap();
    let owner = wallet_owner(&key);
    let envelope = seal(&code, &owner, &backup).unwrap();
    assert_eq!(
        wallet_owner(&open(&code, &owner, &envelope).unwrap()),
        owner
    );
    assert!(open(&code, &owner, &envelope[..ENVELOPE - 1]).is_err());
    assert!(open(&code, &owner, &[envelope.as_slice(), &[0]].concat()).is_err());
    assert!(open(&code, &[0; 32], &envelope).is_err());
    assert!(open(&[3; 32], &owner, &envelope).is_err());
    for offset in [0, SALT.start, NONCE.start, HEADER, ENVELOPE - 1] {
        let mut damaged = envelope;
        damaged[offset] ^= 1;
        assert!(open(&code, &owner, &damaged).is_err());
    }
    backup[1] ^= 1;
    let mislabeled = seal(&code, &owner, &backup).unwrap();
    assert!(open(&code, &owner, &mislabeled).is_err());
    backup[0] = 2;
    let unknown_seed = seal(&code, &owner, &backup).unwrap();
    assert!(open(&code, &owner, &unknown_seed).is_err());
    println!("portable_backup_refusals=11");
}
