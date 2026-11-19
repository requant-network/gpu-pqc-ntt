"""Stdlib-only unittest suite for the exact PQC NTT reference.

Run: python3 -m unittest discover -s tests -v   (from the repo root)
or:  python3 tests/test_reference.py

Includes a faithfulness cross-check of the derived zeta tables against the
official pq-crystals reference tables (Montgomery form), fetched from
https://github.com/pq-crystals/kyber (main, ref/ntt.c) and
https://github.com/pq-crystals/dilithium (master, ref/ntt.c) on 2026-11-19.
"""

import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import reference as ref  # noqa: E402

# Official Kyber zetas[128], Montgomery form (centered), pq-crystals/kyber
# main ref/ntt.c. Index 0 is MONT = 2285 (centered: -1044), unused by NTT.
KYBER_OFFICIAL = [
    -1044, -758, -359, -1517, 1493, 1422, 287, 202,
    -171, 622, 1577, 182, 962, -1202, -1474, 1468,
    573, -1325, 264, 383, -829, 1458, -1602, -130,
    -681, 1017, 732, 608, -1542, 411, -205, -1571,
    1223, 652, -552, 1015, -1293, 1491, -282, -1544,
    516, -8, -320, -666, -1618, -1162, 126, 1469,
    -853, -90, -271, 830, 107, -1421, -247, -951,
    -398, 961, -1508, -725, 448, -1065, 677, -1275,
    -1103, 430, 555, 843, -1251, 871, 1550, 105,
    422, 587, 177, -235, -291, -460, 1574, 1653,
    -246, 778, 1159, -147, -777, 1483, -602, 1119,
    -1590, 644, -872, 349, 418, 329, -156, -75,
    817, 1097, 603, 610, 1322, -1285, -1465, 384,
    -1215, -136, 1218, -1335, -874, 220, -1187, -1659,
    -1185, -1530, -1278, 794, -1510, -854, -870, 478,
    -108, -308, 996, 991, 958, -1460, 1522, 1628,
]

# Official Dilithium zetas[256], Montgomery form (centered),
# pq-crystals/dilithium master ref/ntt.c. Index 0 unused.
DILITHIUM_OFFICIAL = [
    0, 25847, -2608894, -518909, 237124, -777960, -876248, 466468,
    1826347, 2353451, -359251, -2091905, 3119733, -2884855, 3111497, 2680103,
    2725464, 1024112, -1079900, 3585928, -549488, -1119584, 2619752, -2108549,
    -2118186, -3859737, -1399561, -3277672, 1757237, -19422, 4010497, 280005,
    2706023, 95776, 3077325, 3530437, -1661693, -3592148, -2537516, 3915439,
    -3861115, -3043716, 3574422, -2867647, 3539968, -300467, 2348700, -539299,
    -1699267, -1643818, 3505694, -3821735, 3507263, -2140649, -1600420, 3699596,
    811944, 531354, 954230, 3881043, 3900724, -2556880, 2071892, -2797779,
    -3930395, -1528703, -3677745, -3041255, -1452451, 3475950, 2176455, -1585221,
    -1257611, 1939314, -4083598, -1000202, -3190144, -3157330, -3632928, 126922,
    3412210, -983419, 2147896, 2715295, -2967645, -3693493, -411027, -2477047,
    -671102, -1228525, -22981, -1308169, -381987, 1349076, 1852771, -1430430,
    -3343383, 264944, 508951, 3097992, 44288, -1100098, 904516, 3958618,
    -3724342, -8578, 1653064, -3249728, 2389356, -210977, 759969, -1316856,
    189548, -3553272, 3159746, -1851402, -2409325, -177440, 1315589, 1341330,
    1285669, -1584928, -812732, -1439742, -3019102, -3881060, -3628969, 3839961,
    2091667, 3407706, 2316500, 3817976, -3342478, 2244091, -2446433, -3562462,
    266997, 2434439, -1235728, 3513181, -3520352, -3759364, -1197226, -3193378,
    900702, 1859098, 909542, 819034, 495491, -1613174, -43260, -522500,
    -655327, -3122442, 2031748, 3207046, -3556995, -525098, -768622, -3595838,
    342297, 286988, -2437823, 4108315, 3437287, -3342277, 1735879, 203044,
    2842341, 2691481, -2590150, 1265009, 4055324, 1247620, 2486353, 1595974,
    -3767016, 1250494, 2635921, -3548272, -2994039, 1869119, 1903435, -1050970,
    -1333058, 1237275, -3318210, -1430225, -451100, 1312455, 3306115, -1962642,
    -1279661, 1917081, -2546312, -1374803, 1500165, 777191, 2235880, 3406031,
    -542412, -2831860, -1671176, -1846953, -2584293, -3724270, 594136, -3776993,
    -2013608, 2432395, 2454455, -164721, 1957272, 3369112, 185531, -1207385,
    -3183426, 162844, 1616392, 3014001, 810149, 1652634, -3694233, -1799107,
    -3038916, 3523897, 3866901, 269760, 2213111, -975884, 1717735, 472078,
    -426683, 1723600, -1803090, 1910376, -1667432, -1104333, -260646, -3833893,
    -2939036, -2235985, -420899, -2286327, 183443, -976891, 1612842, -3545687,
    -554416, 3919660, -48306, -1362209, 3937738, 1400424, -846154, 1976782,
]

MONT_K = pow(2, 16, ref.Q_KYBER)          # 2285
MONT_D = pow(2, 32, ref.Q_DILITHIUM)      # 4193792


class TestRoots(unittest.TestCase):
    def test_root_orders(self):
        ref.check_roots()
        self.assertEqual(pow(17, 128, 3329), 3328)
        self.assertEqual(pow(1753, 256, 8380417), 8380416)

    def test_zetas_vs_official_kyber(self):
        self.assertEqual(len(KYBER_OFFICIAL), 128)
        for k in range(1, 128):
            self.assertEqual(ref.KYBER_ZETAS[k] * MONT_K % ref.Q_KYBER,
                             KYBER_OFFICIAL[k] % ref.Q_KYBER, f"kyber zeta {k}")

    def test_zetas_vs_official_dilithium(self):
        self.assertEqual(len(DILITHIUM_OFFICIAL), 256)
        for k in range(1, 256):
            self.assertEqual(ref.DILITHIUM_ZETAS[k] * MONT_D % ref.Q_DILITHIUM,
                             DILITHIUM_OFFICIAL[k] % ref.Q_DILITHIUM,
                             f"dilithium zeta {k}")

    def test_kyber_basemul_gammas_vs_official(self):
        # Kyber poly_basemul_montgomery uses zetas[64+i] for pair 2i and
        # -zetas[64+i] for pair 2i+1 (i = 0..63). Convert Montgomery -> normal.
        mont_inv = pow(MONT_K, -1, ref.Q_KYBER)
        for i in range(64):
            g = KYBER_OFFICIAL[64 + i] * mont_inv % ref.Q_KYBER
            self.assertEqual(ref.KYBER_GAMMAS[2 * i], g)
            self.assertEqual(ref.KYBER_GAMMAS[2 * i + 1],
                             (-g) % ref.Q_KYBER)


class TestNTT(unittest.TestCase):
    def test_roundtrip_2seeds(self):
        for seed in (20261119, 987654321):
            rng = random.Random(seed)
            for q, ntt, intt in ((ref.Q_KYBER, ref.kyber_ntt, ref.kyber_intt),
                                 (ref.Q_DILITHIUM, ref.dilithium_ntt, ref.dilithium_intt)):
                for a in ref.random_polys(rng, 8, q) + ref.edge_polys(q):
                    self.assertEqual(intt(ntt(a)), a)

    def test_conv_vs_schoolbook_2seeds(self):
        for seed in (20261119, 987654321):
            rng = random.Random(seed)
            for q, conv in ((ref.Q_KYBER, ref.kyber_conv),
                            (ref.Q_DILITHIUM, ref.dilithium_conv)):
                polys = ref.random_polys(rng, 3, q) + ref.edge_polys(q)
                for a in polys:
                    for b in polys:
                        self.assertEqual(conv(a, b),
                                         ref.schoolbook_negacyclic(a, b, q))

    def test_matrix_formulation(self):
        rng = random.Random(7)
        for q, ntt, M, Mi in (
            (ref.Q_KYBER, ref.kyber_ntt, ref.kyber_matrix(), ref.kyber_matrix_inv()),
            (ref.Q_DILITHIUM, ref.dilithium_ntt, ref.dilithium_matrix(), ref.dilithium_matrix_inv()),
        ):
            for a in ref.random_polys(rng, 3, q):
                ha = ntt(a)
                self.assertEqual(ref.matvec_mod(M, a, q), ha)
                self.assertEqual(ref.matvec_mod(Mi, ha, q), a)
            # M . Minv = I on sampled unit vectors
            for j in (0, 1, 127, 128, 255):
                e = [0] * ref.N
                e[j] = 1
                self.assertEqual(ref.matvec_mod(M, ref.matvec_mod(Mi, e, q), q), e)

    def test_limb_gemm_oracle(self):
        rng = random.Random(42)
        for q, ntt, M in ((ref.Q_KYBER, ref.kyber_ntt, ref.kyber_matrix()),
                          (ref.Q_DILITHIUM, ref.dilithium_ntt, ref.dilithium_matrix())):
            polys = ref.random_polys(rng, 2, q) + [ref.edge_polys(q)[1]]
            Ml = ref.decompose_matrix(M, q)
            self.assertEqual(ref.limb_gemm_oracle(Ml, polys, q),
                             [ntt(a) for a in polys])


class TestLimbs(unittest.TestCase):
    def test_roundtrip_kyber_exhaustive(self):
        for v in range(ref.Q_KYBER):
            d0, d1 = ref.limbs_kyber(v)
            self.assertEqual((d0 + 128 * d1) % ref.Q_KYBER, v)

    def test_roundtrip_dilithium_sampled(self):
        rng = random.Random(3)
        vals = [0, 1, ref.Q_DILITHIUM - 1, ref.Q_DILITHIUM // 2,
                ref.Q_DILITHIUM // 2 + 1, 128, 255, 256, 65535, 65536]
        vals += [rng.randrange(ref.Q_DILITHIUM) for _ in range(50000)]
        for v in vals:
            d0, d1, d2 = ref.limbs_dilithium(v)
            self.assertEqual((d0 + 256 * d1 + 65536 * d2) % ref.Q_DILITHIUM, v)

    def test_int32_accumulator_bound(self):
        # K * max_digit^2 < 2^31 for both decompositions (K = n = 256)
        self.assertLess(256 * 64 * 64, 2**31)
        self.assertLess(256 * 128 * 128, 2**31)


class TestBarrett(unittest.TestCase):
    """Mirror the exact GPU Barrett reducers (same constants, same ops)."""

    def test_kyber_exhaustive(self):
        q, mu = ref.Q_KYBER, 2**32 // ref.Q_KYBER
        self.assertEqual(mu, 1290167)
        for a in range(q * q):
            t = (a * mu) >> 32
            r = a - t * q
            if r >= q:
                r -= q
            if r != a % q:
                self.fail(f"barrett kyber mismatch at a={a}")

    def test_dilithium_sampled(self):
        q, mu = ref.Q_DILITHIUM, 2**64 // ref.Q_DILITHIUM
        self.assertEqual(mu, 2201172575745)
        rng = random.Random(5)
        vals = [0, 1, q - 1, q, q + 1, q * q - 1, q * q - 2, 2 * q * q - 1]
        vals += [rng.randrange(q * q) for _ in range(200000)]
        vals += [rng.randrange(2 * q * q) for _ in range(50000)]
        for a in vals:
            t = (a * mu) >> 64
            r = a - t * q
            if r >= q:
                r -= q
            if r != a % q:
                self.fail(f"barrett dilithium mismatch at a={a}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
