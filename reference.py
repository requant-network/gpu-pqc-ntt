"""Pure-stdlib exact reference for ML-KEM (Kyber) and ML-DSA (Dilithium) NTT.

Ground truth: schoolbook negacyclic convolution mod (X^256 + 1) over Z_q.
All arithmetic is exact Python integers; no floating point anywhere.

Kyber (ML-KEM): q = 3329, n = 256, primitive 256-th root zeta = 17.
  The standard Kyber NTT is INCOMPLETE: 7 Cooley-Tukey layers (len 128..2),
  zetas[k] = 17^bitrev7(k) mod q for k = 1..127. Output is 128 pairs, pair i
  being a(X) mod (X^2 - gamma_i) with gamma_i = 17^(2*bitrev7(i)+1).
  Pointwise multiplication is 128 degree-2 base products. INTT scales by
  128^-1 mod q. This matches the reference Kyber code structure exactly,
  except coefficients are kept in normal form instead of Montgomery form
  (Montgomery form is only a representation; the map is identical).

Dilithium (ML-DSA): q = 8380417, n = 256, zeta = 1753 is a primitive 512-th
  root of unity (1753^256 = -1 mod q). Full 8-layer CT NTT,
  zetas[k] = 1753^bitrev8(k) mod q, k = 1..255. Output coefficient i is
  a(1753^(2*bitrev8(i)+1)) (odd powers, bit-reversed order) -- the standard
  negacyclic evaluation set. Pointwise multiplication is coefficient-wise;
  INTT (Gentleman-Sande) scales by 256^-1 mod q.

Also provides:
  - the fixed NTT matrices M (and inverses) used by the tensor-core
    NTT-as-GEMM construction,
  - balanced signed-limb decomposition (Kyber: 2 limbs base 128, digits in
    [-64,63]; Dilithium: 3 limbs base 256 centered, digits in [-128,127]),
  - an independent exact-integer limb-GEMM oracle (different code path from
    the butterfly NTT) to validate the decomposition/recombination math.
"""

import random

N = 256
Q_KYBER = 3329
Q_DILITHIUM = 8380417
ROOT_KYBER = 17
ROOT_DILITHIUM = 1753


def bitrev(x, bits):
    r = 0
    for _ in range(bits):
        r = (r << 1) | (x & 1)
        x >>= 1
    return r


def _make_zetas(root, q, kmax, bits):
    return [0] + [pow(root, bitrev(k, bits), q) for k in range(1, kmax + 1)]


KYBER_ZETAS = _make_zetas(ROOT_KYBER, Q_KYBER, 127, 7)      # indices 1..127
DILITHIUM_ZETAS = _make_zetas(ROOT_DILITHIUM, Q_DILITHIUM, 255, 8)  # 1..255

KYBER_GAMMAS = [pow(ROOT_KYBER, 2 * bitrev(i, 7) + 1, Q_KYBER) for i in range(128)]
INV128_KYBER = pow(128, -1, Q_KYBER)
INV256_DILITHIUM = pow(256, -1, Q_DILITHIUM)


def check_roots():
    """Verify root orders: 17 has order 256 mod 3329; 1753 has order 512
    mod 8380417 (so 1753^256 = -1, the negacyclic evaluation set exists)."""
    assert pow(ROOT_KYBER, 128, Q_KYBER) == Q_KYBER - 1
    assert pow(ROOT_KYBER, 256, Q_KYBER) == 1
    assert pow(ROOT_DILITHIUM, 256, Q_DILITHIUM) == Q_DILITHIUM - 1
    assert pow(ROOT_DILITHIUM, 512, Q_DILITHIUM) == 1


# --------------------------------------------------------------------------
# Schoolbook ground truth: negacyclic convolution mod (X^256 + 1)
# --------------------------------------------------------------------------

def schoolbook_negacyclic(a, b, q):
    """O(n^2) schoolbook linear convolution folded with X^256 = -1."""
    n = len(a)
    assert n == len(b)
    r = [0] * (2 * n)  # index 2n-1 stays 0 (no X^(2n-1) term exists)
    for i in range(n):
        ai = a[i]
        if ai == 0:
            continue
        for j in range(n):
            r[i + j] += ai * b[j]
    return [(r[i] - r[i + n]) % q for i in range(n)]


# --------------------------------------------------------------------------
# Kyber NTT / INTT / base multiplication (normal form, faithful structure)
# --------------------------------------------------------------------------

def kyber_ntt(a):
    q = Q_KYBER
    r = list(a)
    k = 1
    length = 128
    while length >= 2:
        start = 0
        while start < 256:
            zeta = KYBER_ZETAS[k]
            k += 1
            for j in range(start, start + length):
                t = zeta * r[j + length] % q
                r[j + length] = (r[j] - t) % q
                r[j] = (r[j] + t) % q
            start += 2 * length
        length >>= 1
    return r


def kyber_intt(a):
    q = Q_KYBER
    r = list(a)
    k = 127
    length = 2
    while length <= 128:
        start = 0
        while start < 256:
            zeta = KYBER_ZETAS[k]
            k -= 1
            for j in range(start, start + length):
                t = r[j]
                r[j] = (t + r[j + length]) % q
                r[j + length] = zeta * ((r[j + length] - t) % q) % q
            start += 2 * length
        length <<= 1
    f = INV128_KYBER
    return [x * f % q for x in r]


def kyber_pointwise(a, b):
    """128 degree-2 base products: (a0+a1 X)(b0+b1 X) mod (X^2 - gamma_i)."""
    q = Q_KYBER
    r = [0] * 256
    for i in range(128):
        g = KYBER_GAMMAS[i]
        a0, a1 = a[2 * i], a[2 * i + 1]
        b0, b1 = b[2 * i], b[2 * i + 1]
        r[2 * i] = (a0 * b0 + g * a1 * b1) % q
        r[2 * i + 1] = (a0 * b1 + a1 * b0) % q
    return r


def kyber_conv(a, b):
    return kyber_intt(kyber_pointwise(kyber_ntt(a), kyber_ntt(b)))


# --------------------------------------------------------------------------
# Dilithium NTT / INTT / pointwise (normal form, faithful structure)
# --------------------------------------------------------------------------

def dilithium_ntt(a):
    q = Q_DILITHIUM
    r = list(a)
    k = 0
    length = 128
    while length > 0:
        start = 0
        while start < 256:
            k += 1
            zeta = DILITHIUM_ZETAS[k]
            for j in range(start, start + length):
                t = zeta * r[j + length] % q
                r[j + length] = (r[j] - t) % q
                r[j] = (r[j] + t) % q
            start += 2 * length
        length >>= 1
    return r


def dilithium_intt(a):
    q = Q_DILITHIUM
    r = list(a)
    k = 256
    length = 1
    while length < 256:
        start = 0
        while start < 256:
            k -= 1
            zeta = (-DILITHIUM_ZETAS[k]) % q
            for j in range(start, start + length):
                t = r[j]
                r[j] = (t + r[j + length]) % q
                r[j + length] = zeta * ((t - r[j + length]) % q) % q
            start += 2 * length
        length <<= 1
    f = INV256_DILITHIUM
    return [x * f % q for x in r]


def dilithium_pointwise(a, b):
    q = Q_DILITHIUM
    return [x * y % q for x, y in zip(a, b)]


def dilithium_conv(a, b):
    return dilithium_intt(dilithium_pointwise(dilithium_ntt(a), dilithium_ntt(b)))


# --------------------------------------------------------------------------
# Fixed NTT matrices for the tensor-core NTT-as-GEMM construction
# --------------------------------------------------------------------------

def kyber_matrix():
    """M such that M.a mod q == kyber_ntt(a).

    Pair i of the Kyber NTT is a(X) mod (X^2 - gamma_i): with X^2 = gamma_i,
    r0 = sum_k a[2k] gamma_i^k and r1 = sum_k a[2k+1] gamma_i^k.
    """
    q = Q_KYBER
    M = [[0] * N for _ in range(N)]
    for i in range(128):
        g = KYBER_GAMMAS[i]
        gk = 1
        for k in range(128):
            M[2 * i][2 * k] = gk
            M[2 * i + 1][2 * k + 1] = gk
            gk = gk * g % q
    return M


def kyber_matrix_inv():
    """Inverse map: a[2k] = 128^-1 sum_i hat[2i] gamma_i^-k (and odd part)."""
    q = Q_KYBER
    Mi = [[0] * N for _ in range(N)]
    f = INV128_KYBER
    for i in range(128):
        ginv = pow(KYBER_GAMMAS[i], -1, q)
        gk = f
        for k in range(128):
            Mi[2 * k][2 * i] = gk
            Mi[2 * k + 1][2 * i + 1] = gk
            gk = gk * ginv % q
    return Mi


def dilithium_matrix():
    """M[i][j] = 1753^((2*bitrev8(i)+1) * j) mod q (negacyclic eval set)."""
    q = Q_DILITHIUM
    w = ROOT_DILITHIUM
    return [[pow(w, (2 * bitrev(i, 8) + 1) * j, q) for j in range(N)]
            for i in range(N)]


def dilithium_matrix_inv():
    q = Q_DILITHIUM
    f = INV256_DILITHIUM
    w = ROOT_DILITHIUM
    return [[f * pow(w, (-(2 * bitrev(i, 8) + 1) * j) % (q - 1), q) % q
             for i in range(N)] for j in range(N)]


def matvec_mod(M, a, q):
    return [sum(x * y for x, y in row) % q for row in M] if False else \
           [sum(Mij * aj for Mij, aj in zip(row, a)) % q for row in M]


# --------------------------------------------------------------------------
# Balanced signed-limb decomposition (int8 tensor-core operands)
# --------------------------------------------------------------------------

def center(v, q):
    return v if v <= q // 2 else v - q


def limbs_kyber(v):
    """2 limbs base 128, digits in [-64,63] (d1 actually in [-13,13])."""
    c = center(v, Q_KYBER)
    d0 = c % 128
    if d0 > 63:
        d0 -= 128
    if d0 < -64:
        d0 += 128
    d1 = (c - d0) // 128
    assert -64 <= d0 <= 63 and -128 <= d1 <= 127
    assert d0 + 128 * d1 == c
    return d0, d1


def limbs_dilithium(v):
    """3 limbs base 256 centered, digits in [-128,127] (d2 in [-64,64])."""
    c = center(v, Q_DILITHIUM)
    d0 = c % 256
    if d0 > 127:
        d0 -= 256
    if d0 < -128:
        d0 += 256
    rem = (c - d0) // 256
    d1 = rem % 256
    if d1 > 127:
        d1 -= 256
    if d1 < -128:
        d1 += 256
    d2 = (rem - d1) // 256
    assert all(-128 <= d <= 127 for d in (d0, d1, d2))
    assert d0 + 256 * d1 + 65536 * d2 == c
    return d0, d1, d2


def decompose_matrix(M, q):
    """Return list of limb matrices (each n x n, signed int8 values)."""
    if q == Q_KYBER:
        L = [[], []]
        for row in M:
            l0, l1 = [], []
            for v in row:
                d0, d1 = limbs_kyber(v)
                l0.append(d0)
                l1.append(d1)
            L[0].append(l0)
            L[1].append(l1)
        return L
    L = [[], [], []]
    for row in M:
        ls = [[], [], []]
        for v in row:
            d0, d1, d2 = limbs_dilithium(v)
            ls[0].append(d0)
            ls[1].append(d1)
            ls[2].append(d2)
        for p in range(3):
            L[p].append(ls[p])
    return L


def limb_gemm_oracle(Mlimbs, polys, q):
    """Independent exact-integer oracle for the tensor-core construction.

    Computes C_pr = M_p . A_r with plain integer sums (no butterflies, no
    modular arithmetic until recombination), then recombines with the limb
    weights and reduces mod q. Returns list of NTT results, one per poly.
    Bound: K * max_digit^2 = 256*64^2 = 2^20 (Kyber) and 256*128^2 = 2^22
    (Dilithium), both < 2^31, so int32 GEMM accumulators are exact.
    """
    nl = len(Mlimbs)
    base = 128 if q == Q_KYBER else 256
    n = len(Mlimbs[0])
    out = []
    for a in polys:
        if q == Q_KYBER:
            Alimbs = [[], []]
            for v in a:
                d0, d1 = limbs_kyber(v)
                Alimbs[0].append(d0)
                Alimbs[1].append(d1)
        else:
            Alimbs = [[], [], []]
            for v in a:
                d0, d1, d2 = limbs_dilithium(v)
                Alimbs[0].append(d0)
                Alimbs[1].append(d1)
                Alimbs[2].append(d2)
        res = []
        for i in range(n):
            v = 0
            for p in range(nl):
                Mp = Mlimbs[p][i]
                for r in range(nl):
                    Ar = Alimbs[r]
                    s = sum(x * y for x, y in zip(Mp, Ar))
                    assert abs(s) <= 256 * (64 if q == Q_KYBER else 128) ** 2
                    v += s * base ** (p + r)
            res.append(v % q)
        out.append(res)
    return out


# --------------------------------------------------------------------------
# Input generation (deterministic, seeded)
# --------------------------------------------------------------------------

def random_polys(rng, count, q):
    return [[rng.randrange(q) for _ in range(N)] for _ in range(count)]


def edge_polys(q):
    return [
        [0] * N,                          # zero
        [q - 1] * N,                      # max coefficients
        [1] + [0] * (N - 1),              # delta at 0
        [0] * (N - 1) + [q - 1],          # max at top degree
        [1 if i % 2 == 0 else q - 1 for i in range(N)],  # alternating
    ]


def selftest(verbose=True):
    check_roots()
    for seed in (20261119, 987654321):
        rng = random.Random(seed)
        for q, ntt, intt, pw, conv in (
            (Q_KYBER, kyber_ntt, kyber_intt, kyber_pointwise, kyber_conv),
            (Q_DILITHIUM, dilithium_ntt, dilithium_intt, dilithium_pointwise, dilithium_conv),
        ):
            polys = random_polys(rng, 4, q) + edge_polys(q)
            for a in polys:
                assert intt(ntt(a)) == a, "NTT/INTT roundtrip failed"
            for a in polys[:4]:
                for b in polys[:4]:
                    assert conv(a, b) == schoolbook_negacyclic(a, b, q), \
                        "NTT conv != schoolbook"
            # matrix formulation must agree with butterfly NTT
            M = kyber_matrix() if q == Q_KYBER else dilithium_matrix()
            Mi = kyber_matrix_inv() if q == Q_KYBER else dilithium_matrix_inv()
            for a in polys[:4]:
                ha = ntt(a)
                assert matvec_mod(M, a, q) == ha, "M.a != NTT(a)"
                assert matvec_mod(Mi, ha, q) == a, "Minv.NTT(a) != a"
            # limb-GEMM oracle (independent code path) must agree too
            Ml = decompose_matrix(M, q)
            assert limb_gemm_oracle(Ml, polys[:2], q) == [ntt(a) for a in polys[:2]], \
                "limb GEMM oracle != NTT"
    if verbose:
        print("reference selftest: OK (2 seeds, both primes, edge cases, "
              "matrix + limb-GEMM cross-checks)")


if __name__ == "__main__":
    selftest()
