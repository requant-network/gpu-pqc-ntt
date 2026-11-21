use super::*;
use crate::{MlDsa44, algebra::Polynomial};
use alloc::vec::Vec;
use hybrid_array::Array;

const N: usize = 256;
const DIM: usize = 4;
const INPUT: usize = (DIM * DIM + 2 * DIM + 1) * N;
const OUTPUT: usize = DIM * N;

type Item<'a> = (
    &'a VerifyingKey<MlDsa44>,
    &'a [u8],
    &'a [u8],
    &'a Signature<MlDsa44>,
);

impl VerifyingKey<MlDsa44> {
    /// Verify the supplied public inputs with the isolated CUDA research backend.
    pub fn verify_cuda_lab(
        items: &[Item<'_>],
        reconstruct: impl FnOnce(&[u32], &mut [u32], usize) -> Result<(), i32>,
    ) -> Result<Vec<bool>, i32> {
        if items.is_empty() || items.len() > 128 || items.iter().any(|item| item.2.len() > 255) {
            return Err(-1);
        }
        let mut input = Vec::with_capacity(INPUT * items.len());
        let mut output = alloc::vec![0u32; OUTPUT * items.len()];
        for &(key, _, _, sig) in items {
            let values = &key.precomputed_values;
            for row in &values.A_hat.0 {
                for polynomial in &row.0 {
                    input.extend(polynomial.0.iter().map(|coefficient| coefficient.0));
                }
            }
            for polynomial in &values.t1_2d_hat.0 {
                input.extend(polynomial.0.iter().map(|coefficient| coefficient.0));
            }
            for polynomial in &sig.z.0 {
                input.extend(polynomial.0.iter().map(|coefficient| coefficient.0));
            }
            let c = sample_in_ball(&sig.c_tilde, MlDsa44::TAU);
            input.extend(c.0.iter().map(|coefficient| coefficient.0));
        }
        assert_eq!(input.len(), INPUT * items.len());
        reconstruct(&input, &mut output, items.len())?;
        if output.iter().any(|&coefficient| coefficient >= 8_380_417) {
            return Err(-2);
        }
        Ok(items
            .iter()
            .zip(output.chunks_exact(OUTPUT))
            .map(|(&(key, message, ctx, sig), coefficients)| {
                let approx = Vector::new(Array::from_fn(|row| {
                    Polynomial::new(Array::from_fn(|col| Elem::new(coefficients[row * N + col])))
                }));
                let mu = MuBuilder::new(&key.precomputed_values.tr, ctx).message(&[message]);
                key.verify_approx(&mu, sig, &approx)
            })
            .collect())
    }
}
