"""Repair engine: turns headerless JPEG scan fragments back into viewable images.

Pipeline per file:
  1. intact JPEG?           -> keep as is
  2. RST-marked scan data?  -> detect sampling, restart interval, width, start column
  3. estimate quant tables from block-edge smoothness (pooled per camera setting)
  4. prepend a standard header, decode, crop to complete rows, save PNG
Anything else (e.g. encrypted / random data) is reported as not recoverable.
"""
import io
import re
import struct
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageFile

from .jpegscan import (AC_CHR_BITS, AC_CHR_VALS, AC_LUM_BITS, AC_LUM_VALS, DC_CHR_BITS,
                       DC_CHR_VALS, DC_LUM_BITS, DC_LUM_VALS, decode_coefs, decode_segment,
                       split_segments)

ImageFile.LOAD_TRUNCATED_IMAGES = True

ZIGZAG = [0, 1, 8, 16, 9, 2, 3, 10, 17, 24, 32, 25, 18, 11, 4, 5, 12, 19, 26, 33, 40, 48,
          41, 34, 27, 20, 13, 6, 7, 14, 21, 28, 35, 42, 49, 56, 57, 50, 43, 36, 29, 22, 15,
          23, 30, 37, 44, 51, 58, 59, 52, 45, 38, 31, 39, 46, 53, 60, 61, 54, 47, 55, 62, 63]

# Y blocks per MCU -> (sampling byte for Y, MCU width px, MCU height px)
SAMPLING = {1: (0x11, 8, 8), 2: (0x21, 16, 8), 4: (0x22, 16, 16)}

MIN_SEGMENTS = 50          # fewer RST markers than this: not enough to work with
MAX_EDGE_ROWS = 200_000    # cap on design-matrix rows for the quant-table fit


@dataclass
class Fragment:
    path: Path
    raw: bytes
    n_y: int = 0
    interval: int = 0
    width: int = 0          # in MCUs
    pad: int = 0            # MCU column of the first full segment
    coefs: np.ndarray = None  # (n_mcus, n_y + 2, 64)
    valid: np.ndarray = None
    note: str = ""


@dataclass
class Result:
    name: str
    ok: bool
    message: str
    output: Path = None
    extra: dict = field(default_factory=dict)


# --- detection -------------------------------------------------------------------------------

def _rst_positions(data):
    return [m.start() for m in re.finditer(rb"\xff[\xd0-\xd7]", data)]


def looks_like_scan(data):
    pos = _rst_positions(data)
    if len(pos) < MIN_SEGMENTS:
        return False
    nums = [data[p + 1] - 0xD0 for p in pos]
    in_order = sum((a + 1) % 8 == b for a, b in zip(nums, nums[1:]))
    return in_order / (len(nums) - 1) > 0.9


def detect_sampling(segs):
    """Return (n_y, interval) for which the segments decode cleanly, or (0, 0)."""
    sample = segs[:-1][:200]
    best = (0, 0, 0.0)
    for n_y in (2, 1, 4):
        res = [decode_segment(s, n_y) for s in sample]
        counts = Counter(r[0] for r in res if r[2])
        if not counts:
            continue
        interval, hits = counts.most_common(1)[0]
        score = hits / len(sample)
        if score > best[2]:
            best = (n_y, interval, score)
    return (best[0], best[1]) if best[2] > 0.9 else (0, 0)


def detect_width(dc, mcu_w):
    """Image width in MCUs from the autocorrelation of per-MCU luma DC values."""
    y = dc - dc.mean()
    max_lag = min(len(y) // 3, 8000 // mcu_w)
    lags = np.arange(max(8, 320 // mcu_w), max_lag)
    if len(lags) == 0:
        return 0
    denom = float(np.dot(y, y)) or 1.0
    ac = np.array([np.dot(y[:-L], y[L:]) / denom for L in lags])
    return int(lags[int(np.argmax(ac))])


def detect_start_column(dc, width):
    diffs = np.abs(np.diff(dc))
    seam = int(np.argmax([diffs[c::width].mean() for c in range(width)]))
    return (width - (seam + 1)) % width


def analyse(path):
    raw = Path(path).read_bytes()
    frag = Fragment(Path(path), raw)
    segs, _ = split_segments(raw)
    frag.n_y, frag.interval = detect_sampling(segs)
    if not frag.n_y:
        frag.note = "Bilddaten erkannt, aber Aufbau unbekannt"
        return frag
    mcus, valid = [], []
    for s in segs:
        arr, n = decode_coefs(s, frag.interval, frag.n_y)
        mcus.append(arr)
        valid += [True] * n + [False] * (frag.interval - n)
    frag.coefs = np.concatenate(mcus)
    frag.valid = np.array(valid)
    dc = frag.coefs[:, :frag.n_y, 0].mean(axis=1).astype(float)
    frag.width = detect_width(dc, SAMPLING[frag.n_y][1])
    if not frag.width:
        frag.note = "zu wenig Bilddaten"
        frag.n_y = 0
        return frag
    frag.pad = detect_start_column(dc, frag.width)
    return frag


# --- quantisation tables ---------------------------------------------------------------------

_c = np.array([np.sqrt(1 / 8)] + [np.sqrt(2 / 8)] * 7)
_cos = np.array([[_c[u] * np.cos((2 * x + 1) * u * np.pi / 16) for u in range(8)] for x in range(8)])
BASIS = np.zeros((64, 8, 8))
for _k, _nat in enumerate(ZIGZAG):
    _v, _u = divmod(_nat, 8)
    BASIS[_k] = np.outer(_cos[:, _v], _cos[:, _u])


def _grids(frag):
    """Block grids (blocks (R, C, 64), ok (R, C)) for Y, Cb and Cr of a fragment."""
    w, n_y = frag.width, frag.n_y
    coefs = np.concatenate([np.zeros((frag.pad, n_y + 2, 64), np.int32), frag.coefs])
    valid = np.concatenate([np.zeros(frag.pad, bool), frag.valid])
    rows = -(-len(coefs) // w)
    extra = rows * w - len(coefs)
    coefs = np.concatenate([coefs, np.zeros((extra, n_y + 2, 64), np.int32)]).reshape(rows, w, n_y + 2, 64)
    valid = np.concatenate([valid, np.zeros(extra, bool)]).reshape(rows, w)
    if n_y == 1:
        y, yok = coefs[:, :, 0], valid
    elif n_y == 2:
        y, yok = coefs[:, :, :2].reshape(rows, w * 2, 64), np.repeat(valid, 2, axis=1)
    else:  # 2x2 luma blocks per MCU
        y = coefs[:, :, :4].reshape(rows, w, 2, 2, 64).transpose(0, 2, 1, 3, 4).reshape(rows * 2, w * 2, 64)
        yok = np.repeat(np.repeat(valid, 2, axis=0), 2, axis=1)
    return [(y, yok)], [(coefs[:, :, n_y], valid), (coefs[:, :, n_y + 1], valid)]


def _edge_rows(blocks, ok, max_pairs, rng):
    """Design-matrix rows for block boundaries; at most max_pairs block pairs per direction
    are sampled first, so memory stays small (matters in the browser)."""
    out = []
    pairs = ((blocks[:, :-1], blocks[:, 1:], ok[:, :-1] & ok[:, 1:],
              1.5 * BASIS[:, :, 7] - 0.5 * BASIS[:, :, 6], 1.5 * BASIS[:, :, 0] - 0.5 * BASIS[:, :, 1]),
             (blocks[:-1], blocks[1:], ok[:-1] & ok[1:],
              1.5 * BASIS[:, 7, :] - 0.5 * BASIS[:, 6, :], 1.5 * BASIS[:, 0, :] - 0.5 * BASIS[:, 1, :]))
    for a, b, m, edge_a, edge_b in pairs:
        idx = np.flatnonzero(m)
        if len(idx) > max_pairs:
            idx = np.sort(rng.choice(idx, max_pairs, replace=False))
        a = a.reshape(-1, 64)[idx].astype(np.float32)
        b = b.reshape(-1, 64)[idx].astype(np.float32)
        f = b[:, :, None] * edge_b[None].astype(np.float32) - a[:, :, None] * edge_a[None].astype(np.float32)
        out.append(f.transpose(0, 2, 1).reshape(-1, 64))
    return np.concatenate(out)


def fit_quant_table(grids, q_dc):
    """Integer quant table (zigzag) that makes block edges smooth, DC fixed at q_dc."""
    rng = np.random.default_rng(0)
    max_pairs = MAX_EDGE_ROWS // (16 * len(grids))  # 8 rows per pair, 2 directions
    X = np.concatenate([_edge_rows(b, ok, max_pairs, rng) for b, ok in grids])
    usage = np.mean(np.concatenate([b[ok] != 0 for b, ok in grids]), axis=0)
    A, y = X[:, 1:], -X[:, 0] * q_dc
    w = np.ones(len(y), np.float32)
    for _ in range(4):  # IRLS so real edges in the photo don't dominate
        sw = np.sqrt(w)
        q, *_ = np.linalg.lstsq(A * sw[:, None], y * sw, rcond=None)
        r = np.abs(A @ q - y)
        w = 1 / np.maximum(r, np.median(r))
    q = np.clip(np.round(np.concatenate([[q_dc], q])), 1, 255)
    used = usage > 0.005
    for k in np.where(~used)[0]:  # unidentifiable: smooth, rising fill value
        q[k] = max(q[used & (np.arange(64) < k)].max(), 8)
    q[0] = q_dc
    cost = lambda t: float(np.mean(np.abs(X @ t)))
    best = cost(q)
    for _ in range(6):  # integer coordinate descent
        changed = False
        for k in np.where(used)[0][1:]:
            for cand in (q[k] - 2, q[k] - 1, q[k] + 1, q[k] + 2):
                if 1 <= cand <= 255:
                    old, q[k] = q[k], cand
                    c = cost(q)
                    if c < best - 1e-9:
                        best, changed = c, True
                    else:
                        q[k] = old
        if not changed:
            break
    return [int(v) for v in q]


def estimate_tables(frags):
    """Quant tables for a group of fragments sharing the same camera settings."""
    max_dc = max(np.percentile(np.abs(f.coefs[f.valid][:, :f.n_y, 0]), 99.9) for f in frags)
    q_dc = int(np.clip(1020 // max(max_dc, 1), 1, 64))  # block means must stay within 0..255
    y_grids, c_grids = [], []
    for f in frags:
        y, c = _grids(f)
        y_grids += y
        c_grids += c
    return {"Y": fit_quant_table(y_grids, q_dc), "C": fit_quant_table(c_grids, q_dc)}


# --- rebuilding ------------------------------------------------------------------------------

def _segment(marker, payload):
    return b"\xff" + bytes([marker]) + struct.pack(">H", len(payload) + 2) + payload


def build_header(width, height, n_y, interval, tables):
    dqt = bytes([0]) + bytes(tables["Y"]) + bytes([1]) + bytes(tables["C"])
    sof = struct.pack(">BHHB", 8, height, width, 3) + bytes([1, SAMPLING[n_y][0], 0, 2, 0x11, 1, 3, 0x11, 1])
    dht = b""
    for tc_th, bits, vals in ((0x00, DC_LUM_BITS, DC_LUM_VALS), (0x10, AC_LUM_BITS, AC_LUM_VALS),
                              (0x01, DC_CHR_BITS, DC_CHR_VALS), (0x11, AC_CHR_BITS, AC_CHR_VALS)):
        dht += bytes([tc_th]) + bytes(bits) + bytes(vals)
    sos = bytes([3, 1, 0x00, 2, 0x11, 3, 0x11, 0, 63, 0])
    return (b"\xff\xd8" + _segment(0xDB, dqt) + _segment(0xC0, sof) + _segment(0xC4, dht)
            + _segment(0xDD, struct.pack(">H", interval)) + _segment(0xDA, sos))


def _empty_interval(n_y, interval):
    """Entropy data for `interval` all-zero (mid-grey) MCUs: DC diff 0 + EOB per block."""
    bits = ("00" + "1010") * n_y + ("00" + "00") * 2
    bits *= interval
    bits += "1" * (-len(bits) % 8)
    data = int(bits, 2).to_bytes(len(bits) // 8, "big")
    return data.replace(b"\xff", b"\xff\x00")


def rebuild(frag, tables):
    """Return the repaired image (PIL), cropped to rows that are complete."""
    w, iv = frag.width, frag.interval
    pad = frag.pad
    while pad % iv:  # leading padding must be whole restart intervals
        pad += w
    raw = frag.raw
    pos = _rst_positions(raw)
    raw_segs = [raw[s + 2:e] for s, e in zip(pos, pos[1:] + [len(raw)])]
    intervals = [_empty_interval(frag.n_y, iv)] * (pad // iv) + raw_segs
    rows = -(-len(intervals) * iv // w)
    scan = bytearray()
    for i, seg in enumerate(intervals):
        if i:
            scan += bytes([0xFF, 0xD0 + (i - 1) % 8])
        scan += seg
    _, mcu_w, mcu_h = SAMPLING[frag.n_y]
    jpeg = build_header(w * mcu_w, rows * mcu_h, frag.n_y, iv, tables) + bytes(scan) + b"\xff\xd9"
    im = Image.open(io.BytesIO(jpeg))
    im.load()
    first_row = -(-pad // w)
    last_row = (pad + (len(raw_segs) - 1) * iv) // w
    if last_row <= first_row:  # strip shorter than one full row: keep everything
        first_row, last_row = 0, rows
    return im.crop((0, first_row * mcu_h, im.width, last_row * mcu_h))


# --- public API ------------------------------------------------------------------------------

def is_intact_jpeg(data):
    if not data.startswith(b"\xff\xd8"):
        return False
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.load()
        return True
    except Exception:
        return False


def process_files(paths, out_dir, progress=lambda text: None):
    """Repair all files into out_dir. Returns a list of Result."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    results, frags = [], []
    for i, p in enumerate(paths, 1):
        p = Path(p)
        progress(f"Untersuche {p.name} ({i}/{len(paths)})")
        try:
            data = p.read_bytes()
            if is_intact_jpeg(data):
                (out_dir / p.name).write_bytes(data)
                results.append(Result(p.name, True, "war schon in Ordnung", out_dir / p.name))
            elif looks_like_scan(data):
                frag = analyse(p)
                if frag.n_y:
                    frags.append(frag)
                else:
                    results.append(Result(p.name, False, frag.note))
            else:
                results.append(Result(p.name, False, "keine Bilddaten gefunden (verschlüsselt oder zerstört)"))
        except Exception as e:  # one broken file must not stop the rest
            results.append(Result(p.name, False, f"Fehler: {e}"))

    groups = {}
    for f in frags:
        groups.setdefault((f.n_y, f.interval, f.width), []).append(f)
    for group in groups.values():
        progress(f"Berechne Bildeinstellungen ({len(group)} Bild(er))")
        try:
            tables = estimate_tables(group)
        except Exception as e:
            results += [Result(f.path.name, False, f"Fehler: {e}") for f in group]
            continue
        pieces = []
        for f in group:
            progress(f"Repariere {f.path.name}")
            try:
                im = rebuild(f, tables)
                out = out_dir / (f.path.stem.lstrip("_") + "_gerettet.png")
                im.save(out)
                results.append(Result(f.path.name, True, f"gerettet ({im.width}×{im.height} Pixel)", out))
                pieces.append((f.path.stem, im, results[-1]))
            except Exception as e:
                results.append(Result(f.path.name, False, f"Fehler: {e}"))
        if len(pieces) >= 2:
            progress(f"Setze {len(pieces)} Teile zusammen")
            try:
                results.append(assemble([(stem, im) for stem, im, _ in pieces], out_dir))
            except Exception as e:  # keep the single strips as the result instead
                results.append(Result("Zusammengesetztes Bild", False, f"Fehler: {e}"))
                continue
            # only the combined image is handed out; the strips live on inside it
            for _, _, res in pieces:
                res.output.unlink(missing_ok=True)
                res.output = None
                res.message = "gerettet, im zusammengesetzten Bild enthalten"
    return results


# --- assembling strips of the same photo -----------------------------------------------------

GAP_PX = 48  # height of the hatched bar that marks missing data between two strips


def _edge_profile(im, top):
    """Smoothed brightness profile along the top or bottom 16 rows of a strip."""
    g = np.asarray(im.convert("L"), np.float32)
    rows = g[:16] if top else g[-16:]
    return np.convolve(rows.mean(axis=0), np.ones(32) / 32, mode="same")


def _fit(a, b):
    """How well strip b continues below strip a (correlation of the touching edges)."""
    pa, pb = _edge_profile(a, top=False), _edge_profile(b, top=True)
    if pa.std() < 1e-6 or pb.std() < 1e-6:
        return 0.0
    return float(np.corrcoef(pa, pb)[0, 1])


def order_pieces(pieces):
    """Top-to-bottom order of strips: best chain of matching edges, file order breaks ties."""
    from itertools import permutations
    n = len(pieces)
    score = [[_fit(pieces[i][1], pieces[j][1]) if i != j else 0.0 for j in range(n)] for i in range(n)]
    chain = lambda order: sum(score[a][b] for a, b in zip(order, order[1:]))
    by_name = sorted(range(n), key=lambda i: pieces[i][0])
    if n > 7:  # too many to try every order; trust the file numbering
        return by_name
    best = max(permutations(range(n)), key=chain)
    # camera file numbers follow the recording order, so keep them unless clearly worse
    return by_name if chain(by_name) >= chain(best) - 0.1 else list(best)


def assemble(pieces, out_dir):
    from PIL import ImageDraw
    order = order_pieces(pieces)
    ims = [pieces[i][1].convert("RGB") for i in order]
    width = max(im.width for im in ims)
    height = sum(im.height for im in ims) + GAP_PX * (len(ims) - 1)
    canvas = Image.new("RGB", (width, height), (40, 40, 40))
    draw = ImageDraw.Draw(canvas)
    y = 0
    for k, im in enumerate(ims):
        canvas.paste(im, (0, y))
        y += im.height
        if k < len(ims) - 1:
            for x in range(-GAP_PX, width, 64):
                draw.line([(x, y), (x + GAP_PX, y + GAP_PX)], fill=(70, 70, 70), width=6)
            y += GAP_PX
    names = [pieces[i][0].lstrip("_") for i in order]
    out = Path(out_dir) / f"zusammengesetzt_{names[0]}-{names[-1]}.png"
    canvas.save(out)
    return Result("Zusammengesetztes Bild", True,
                  f"aus {len(ims)} Teilen, Lücken grau schraffiert", out,
                  extra={"combined": True})
