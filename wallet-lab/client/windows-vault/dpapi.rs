use std::ffi::c_void;
use std::fs::{self, File};
use std::io::{self, Read, Write};
use std::path::Path;
use std::{ptr, slice};
use zeroize::Zeroize;

const FORMAT: &[u8] = b"RQPL-WS1";
const PURPOSE: &[u8] = b"requant/pqc-lab/windows-seed/v1";
const BACKUP_LEN: usize = 33;
const LIMIT: usize = 4_096;
const UI_FORBIDDEN: u32 = 1;

#[repr(C)]
struct DataBlob {
    len: u32,
    data: *mut u8,
}

#[link(name = "crypt32")]
unsafe extern "system" {
    fn CryptProtectData(
        input: *const DataBlob,
        description: *const u16,
        entropy: *const DataBlob,
        reserved: *mut c_void,
        prompt: *mut c_void,
        flags: u32,
        output: *mut DataBlob,
    ) -> i32;
    fn CryptUnprotectData(
        input: *const DataBlob,
        description: *mut *mut u16,
        entropy: *const DataBlob,
        reserved: *mut c_void,
        prompt: *mut c_void,
        flags: u32,
        output: *mut DataBlob,
    ) -> i32;
}

#[link(name = "kernel32")]
unsafe extern "system" {
    fn LocalFree(memory: *mut c_void) -> *mut c_void;
}

pub(super) fn invalid(message: &'static str) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, message)
}

fn input_blob(bytes: &[u8]) -> io::Result<DataBlob> {
    Ok(DataBlob {
        len: u32::try_from(bytes.len()).map_err(|_| invalid("DPAPI input too long"))?,
        data: bytes.as_ptr().cast_mut(),
    })
}

pub(super) struct ProtectedBuffer {
    blob: DataBlob,
    secret: bool,
}

impl ProtectedBuffer {
    fn new(secret: bool) -> Self {
        Self {
            blob: DataBlob {
                len: 0,
                data: ptr::null_mut(),
            },
            secret,
        }
    }

    pub(super) fn bytes(&self) -> io::Result<&[u8]> {
        if self.blob.data.is_null() || self.blob.len == 0 || self.blob.len as usize > LIMIT {
            return Err(invalid("invalid DPAPI output length"));
        }
        // DPAPI returned a live allocation owned by this object.
        Ok(unsafe { slice::from_raw_parts(self.blob.data, self.blob.len as usize) })
    }
}

impl Drop for ProtectedBuffer {
    fn drop(&mut self) {
        if !self.blob.data.is_null() {
            // DPAPI owns this allocation until the caller calls LocalFree.
            unsafe {
                if self.secret && self.blob.len != 0 {
                    slice::from_raw_parts_mut(self.blob.data, self.blob.len as usize).zeroize();
                }
                LocalFree(self.blob.data.cast());
            }
        }
    }
}

pub(super) fn write_new(path: &Path, bytes: &[u8]) -> io::Result<()> {
    let mut nonce = [0u8; 16];
    getrandom::fill(&mut nonce).map_err(io::Error::other)?;
    let pending = path.with_extension(format!("{}.pending", hex::encode(nonce)));
    let mut file = File::create_new(&pending)?;
    let written = file.write_all(bytes).and_then(|()| file.sync_all());
    drop(file);
    // The lab publishes on the same NTFS volume without replacing a wallet.
    let published = written.and_then(|()| fs::hard_link(&pending, path));
    let _ = fs::remove_file(pending);
    published
}

pub(super) fn seal(path: &Path, backup: &[u8]) -> io::Result<()> {
    seal_for_purpose(path, backup, PURPOSE)
}

pub(super) fn seal_for_purpose(path: &Path, backup: &[u8], purpose: &[u8]) -> io::Result<()> {
    if backup.len() != BACKUP_LEN || backup.first() != Some(&1) {
        return Err(invalid("invalid wallet backup format"));
    }
    let input = input_blob(backup)?;
    let entropy = input_blob(purpose)?;
    let mut output = ProtectedBuffer::new(false);
    // The input slices remain live during the synchronous calls and DPAPI only reads them.
    let ok = unsafe {
        CryptProtectData(
            &input,
            ptr::null(),
            &entropy,
            ptr::null_mut(),
            ptr::null_mut(),
            UI_FORBIDDEN,
            &mut output.blob,
        )
    };
    if ok == 0 {
        return Err(io::Error::last_os_error());
    }
    let mut envelope = FORMAT.to_vec();
    envelope.extend_from_slice(output.bytes()?);
    if envelope.len() > LIMIT {
        return Err(invalid("wallet envelope too large"));
    }
    write_new(path, &envelope)
}

pub(super) fn unseal(path: &Path) -> io::Result<ProtectedBuffer> {
    let mut bytes = Vec::new();
    File::open(path)?
        .take((LIMIT + 1) as u64)
        .read_to_end(&mut bytes)?;
    let payload = bytes
        .strip_prefix(FORMAT)
        .filter(|data| !data.is_empty() && bytes.len() <= LIMIT)
        .ok_or_else(|| invalid("invalid wallet envelope"))?;
    let input = input_blob(payload)?;
    let entropy = input_blob(PURPOSE)?;
    let mut output = ProtectedBuffer::new(true);
    // DPAPI checks its integrity tag before returning a plaintext allocation.
    let ok = unsafe {
        CryptUnprotectData(
            &input,
            ptr::null_mut(),
            &entropy,
            ptr::null_mut(),
            ptr::null_mut(),
            UI_FORBIDDEN,
            &mut output.blob,
        )
    };
    if ok == 0 {
        return Err(io::Error::last_os_error());
    }
    if output.bytes()?.len() != BACKUP_LEN || output.bytes()?.first() != Some(&1) {
        return Err(invalid("invalid recovered wallet backup"));
    }
    Ok(output)
}
