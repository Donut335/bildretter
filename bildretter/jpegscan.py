"""Tools for headerless JPEG scan data (entropy-coded segments split by RST markers).

Uses the standard Huffman tables from JPEG Annex K, which most cameras use.
"""
import re

# --- Annex K standard Huffman tables -------------------------------------------------
DC_LUM_BITS = [0, 1, 5, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0]
DC_LUM_VALS = list(range(12))
DC_CHR_BITS = [0, 3, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0]
DC_CHR_VALS = list(range(12))
AC_LUM_BITS = [0, 2, 1, 3, 3, 2, 4, 3, 5, 5, 4, 4, 0, 0, 1, 0x7D]
AC_LUM_VALS = bytes.fromhex(
    "01020300041105122131410613516107227114328191a1082342b1c11552d1f02433627282090a161718191a"
    "25262728292a3435363738393a434445464748494a535455565758595a636465666768696a737475767778797a"
    "838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2"
    "d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8f9fa")
AC_CHR_BITS = [0, 2, 1, 2, 4, 4, 3, 4, 7, 5, 4, 4, 0, 1, 2, 0x77]
AC_CHR_VALS = bytes.fromhex(
    "000102031104052131061241510761711322328108144291a1b1c109233352f0156272d10a162434e125f11718"
    "191a262728292a35363738393a434445464748494a535455565758595a636465666768696a737475767778797a"
    "82838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9ca"
    "d2d3d4d5d6d7d8d9dae2e3e4e5e6e7e8e9eaf2f3f4f5f6f7f8f9fa")


def build_lookup(bits, vals):
    """Return dict (length, code) -> value."""
    table, code, k = {}, 0, 0
    for length in range(1, 17):
        for _ in range(bits[length - 1]):
            table[(length, code)] = vals[k]
            code += 1
            k += 1
        code <<= 1
    return table


DC_LUM = build_lookup(DC_LUM_BITS, DC_LUM_VALS)
DC_CHR = build_lookup(DC_CHR_BITS, DC_CHR_VALS)
AC_LUM = build_lookup(AC_LUM_BITS, AC_LUM_VALS)
AC_CHR = build_lookup(AC_CHR_BITS, AC_CHR_VALS)


def split_segments(data):
    """Split raw scan data at RST markers. Returns (segments, rst_numbers).

    The first piece (before the first RST) is a partial segment and is dropped.
    Each segment is returned with FF00 byte stuffing removed.
    """
    pos = [m.start() for m in re.finditer(rb"\xff[\xd0-\xd7]", data)]
    segs, nums = [], []
    for a, b in zip(pos, pos[1:] + [len(data)]):
        segs.append(data[a + 2:b].replace(b"\xff\x00", b"\xff"))
        nums.append(data[a + 1] - 0xD0)
    return segs, nums


class BitReader:
    def __init__(self, data):
        self.val = int.from_bytes(data, "big") if data else 0
        self.n = len(data) * 8
        self.p = 0

    def bits(self, k):
        if self.p + k > self.n:
            raise EOFError
        v = (self.val >> (self.n - self.p - k)) & ((1 << k) - 1)
        self.p += k
        return v

    def huff(self, table):
        code = 0
        for length in range(1, 17):
            code = (code << 1) | self.bits(1)
            v = table.get((length, code))
            if v is not None:
                return v
        raise ValueError("bad huffman code")


def extend(v, s):
    return v - (1 << s) + 1 if s and v < (1 << (s - 1)) else v


def decode_block(br, dct, act, coefs=None):
    """Decode one 8x8 block. Returns DC diff. Optionally fills coefs[0..63] (zigzag order)."""
    s = br.huff(dct)
    diff = extend(br.bits(s), s) if s else 0
    k = 1
    while k < 64:
        rs = br.huff(act)
        r, s = rs >> 4, rs & 15
        if s == 0:
            if r == 15:
                k += 16
                continue
            break
        k += r
        if k > 63:
            raise ValueError("coef overflow")
        v = extend(br.bits(s), s)
        if coefs is not None:
            coefs[k] = v
        k += 1
    return diff


def decode_coefs(seg, n_mcus, n_y=2):
    """Decode exactly n_mcus MCUs into an int array (n_mcus, n_y + 2, 64), zigzag order,
    with absolute DC values. Returns (array, mcus_decoded); undecodable MCUs stay zero."""
    import numpy as np
    out = np.zeros((n_mcus, n_y + 2, 64), np.int32)
    br = BitReader(seg)
    preds = [0, 0, 0]
    tables = [(DC_LUM, AC_LUM, 0)] * n_y + [(DC_CHR, AC_CHR, 1), (DC_CHR, AC_CHR, 2)]
    m = 0
    try:
        for m in range(n_mcus):
            for b, (dct, act, comp) in enumerate(tables):
                c = [0] * 64
                preds[comp] += decode_block(br, dct, act, c)
                c[0] = preds[comp]
                out[m, b] = c
        m = n_mcus
    except (EOFError, ValueError):
        pass
    return out, m


def decode_segment(seg, n_y, max_mcus=10**6):
    """Decode MCUs of a segment with n_y luma blocks + 1 Cb + 1 Cr per MCU.

    Returns (mcus_decoded, leftover_bits, padding_ok, dc_list) where dc_list holds the
    absolute Y DC value of each MCU's first block (the DC predictor resets at every RST).
    """
    br = BitReader(seg)
    preds = [0, 0, 0]
    dcs, mcus = [], 0
    try:
        while mcus < max_mcus:
            start = br.p
            ys = []
            for _ in range(n_y):
                preds[0] += decode_block(br, DC_LUM, AC_LUM)
                ys.append(preds[0])
            preds[1] += decode_block(br, DC_CHR, AC_CHR)
            preds[2] += decode_block(br, DC_CHR, AC_CHR)
            dcs.append((ys, preds[1], preds[2]))
            mcus += 1
    except (EOFError, ValueError):
        br.p = start
    left = br.n - br.p
    pad_ok = left < 8 and (left == 0 or br.val & ((1 << left) - 1) == (1 << left) - 1)
    return mcus, left, pad_ok, dcs
