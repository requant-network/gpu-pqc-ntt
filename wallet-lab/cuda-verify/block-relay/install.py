"""Add the block-signature experiment to an already patched CUDA wallet lab."""

from pathlib import Path
import shutil
import sys


native = Path(sys.argv[1]).resolve()
here = Path(__file__).resolve().parent
consensus = native / "crates/consensus"
chain = consensus / "src/chain.rs"
pqc = consensus / "src/pqc.rs"
old = "        block.txs.iter().try_for_each(|t| t.check_signatures(&self.net.chain_id))?;"
source = chain.read_text()
if source.count(old) != 1 or "mod mixed_cuda_lab;" not in pqc.read_text():
    raise ValueError("expected the pinned native CUDA lab")
for path in (consensus / "src/block_signature_lab.rs", consensus / "tests/pqc_block_checks.inc"):
    if path.exists():
        raise ValueError(f"already installed: {path}")
chain.write_text(source.replace(old, """        crate::pqc::check_block_signatures(
            &self.net.chain_id, block.header.height, &block.txs,
        )?;"""))
with pqc.open("a") as handle:
    handle.write("""
#[path = "block_signature_lab.rs"]
mod block_signature_lab;
pub(crate) use block_signature_lab::check_block_signatures;
""")
shutil.copyfile(here / "block_signature_lab.rs", consensus / "src/block_signature_lab.rs")
shutil.copyfile(here / "block_checks.rs", consensus / "tests/pqc_block_checks.inc")
with (consensus / "tests/pqc_wallet.rs").open("a") as handle:
    handle.write('\nmod block_checks { include!("pqc_block_checks.inc"); }\n')
