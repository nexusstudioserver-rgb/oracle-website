#!/usr/bin/env python3
"""
Oracle decompiler - serverless endpoint (Vercel Python function)
================================================================

Accepts, as JSON on POST:
  * {script|bytecode: <base64>, filename}  - one compiled Luau chunk, or
    a whole Roblox place/model (.rbxl .rbxlx .rbxm .rbxmx) to unpack, or
  * {oracle_upload: {id, index|finish, total, filename}, chunk}  - one piece
    of a big file (hosts cap a single body at 4.5 MB), or
  * {oracle_result: {id, part}}  - one piece of a big answer.

GET returns a small health JSON. No API key is required anywhere on this
path, so the key typed in the page is never validated or rejected.
"""

import base64
import concurrent.futures
import json
import os
import re
import struct
import tempfile
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler

DEFAULT_UPSTREAM = "https://lunaux-decompiler.dev/api/decompile"
UPSTREAM = DEFAULT_UPSTREAM
ALLOWED_ORIGIN = "*"
TIMEOUT = 120
MAX_SCRIPTS = 20000
MAX_OUTPUT = 64 * 1024 * 1024          # bytes of text returned to the editor
WORKERS = 8                            # parallel engine calls for big places
WRITE_FILES = False
EXTRACT_DIR = "/tmp/extracted"

# ---- chunked transfer -------------------------------------------------------
# Hosts such as Vercel cap a single request *and* response body at 4.5 MB. Files
# and results bigger than that travel in numbered pieces; each piece stays well
# under the cap. Everything below is plain HTTP, so the local engine speaks the
# same protocol and nothing special is needed on the client beyond splitting.
TX_DIR = os.path.join(tempfile.gettempdir(), "oracle-transfer")
TX_TTL = 15 * 60                       # pieces older than this are dropped
TX_PART = 3 * 1024 * 1024              # utf-8 bytes per returned result piece
PARK_LIMIT = 3 * 1024 * 1024           # a bigger answer is parked (see _park_result)
MAX_UPLOAD = 64 * 1024 * 1024          # assembled size cap for the chunked path
_UPLOADS = {}                          # id -> {"chunks": {i: b64}, "total", "filename", "ts"}
_RESULTS = {}                          # id -> {"parts": [str], "filename", "ts"}
_PLACES = {}                           # id -> the scripts of one game, for the .rbxl download

SCRIPT_CLASSES = ("Script", "LocalScript", "ModuleScript")


class RbxError(Exception):
    """Raised when a game/model file can't be read."""


# ===========================================================================
#  LZ4 block decoder (pure python - Roblox compresses its chunks with LZ4)
# ===========================================================================
def lz4_decompress(src, out_size):
    out = bytearray()
    i, n = 0, len(src)
    while i < n:
        token = src[i]; i += 1
        lit = token >> 4
        if lit == 15:
            while i < n:
                b = src[i]; i += 1
                lit += b
                if b != 255:
                    break
        if lit:
            out += src[i:i + lit]
            i += lit
        if i + 2 > n:
            break
        offset = src[i] | (src[i + 1] << 8); i += 2
        if offset == 0:
            raise RbxError("corrupt LZ4 data in this file")
        mlen = token & 0x0F
        if mlen == 15:
            while i < n:
                b = src[i]; i += 1
                mlen += b
                if b != 255:
                    break
        mlen += 4
        start = len(out) - offset
        if start < 0:
            raise RbxError("corrupt LZ4 data in this file")
        if offset >= mlen:
            out += out[start:start + mlen]
        else:                                   # overlapping copy = a repeating pattern
            seed = bytes(out[start:start + offset])
            out += (seed * ((mlen + offset - 1) // offset))[:mlen]
        if out_size and len(out) >= out_size:
            break
    if out_size and len(out) < out_size:
        raise RbxError("truncated file (LZ4 block ended early)")
    return bytes(out[:out_size] if out_size else out)


def _untransform(x):
    return (x >> 1) ^ -(x & 1)


def _deinterleave(buf, count, width):
    out = bytearray(count * width)
    for w in range(width):
        out[w::width] = buf[w * count:(w + 1) * count]
    return bytes(out)


def _read_string(buf, off):
    ln = struct.unpack_from('<I', buf, off)[0]
    off += 4
    if off + ln > len(buf):
        raise RbxError("truncated string inside the file")
    return buf[off:off + ln], off + ln


def _read_referents(buf, off, count):
    """Referents are big-endian transformed ints, byte-interleaved, deltas."""
    raw = _deinterleave(buf[off:off + count * 4], count, 4)
    out, acc = [], 0
    for (v,) in struct.iter_unpack('>i', raw):
        acc += _untransform(v)
        out.append(acc)
    return out, off + count * 4


def _decompress_chunk(name, body, uncomp_len):
    """Chunk payload -> raw bytes. Roblox writes either LZ4 or ZSTD; both are
    decoded here in pure python, so nothing has to be installed anywhere."""
    if body[:4] == b'\x28\xb5\x2f\xfd':                   # ZSTD frame magic
        try:
            return zstd_decompress(body, max_output=uncomp_len)
        except Exception as exc:
            # if the local machine happens to have the zstandard package, let it
            # have a go before giving up - it is the same decoder in C
            try:
                import zstandard
            except ImportError:
                raise RbxError(
                    "This place stores its %s chunk with ZSTD compression and the "
                    "frame could not be read (%s)." % (name.strip(), exc))
            return zstandard.ZstdDecompressor().decompress(body, max_output_size=uncomp_len)
    return lz4_decompress(body, uncomp_len)

# ===========================================================================
#  ZSTD frame decoder (pure python - RFC 8878)
# ---------------------------------------------------------------------------
#  Newer Roblox saves compress their chunks with ZSTD. No browser can inflate
#  that (DecompressionStream has no zstd), and a serverless host has no
#  zstandard package, so the decoder is here in plain python. It was written
#  against RFC 8878 and the zstd 1.5.6 sources and checked on 224 generated
#  frames (every level 1..22, with and without a content size, with a
#  checksum, and written by a streaming compressor) plus the real fixtures.
# ===========================================================================
class ZstdError(Exception):
    """Raised when a frame cannot be read."""


class ZstdFrameError(ZstdError):
    pass


# --------------------------------------------------------------------- tables
_LL_BASE = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15,
            16, 18, 20, 22, 24, 28, 32, 40, 48, 64, 128, 256, 512, 1024, 2048,
            4096, 8192, 16384, 32768, 65536]
_LL_BITS = [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
            1, 1, 1, 1, 2, 2, 3, 3, 4, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16]
_ML_BASE = [3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21,
            22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34,
            35, 37, 39, 41, 43, 47, 51, 59, 67, 83, 99, 131, 259, 515, 1027, 2051,
            4099, 8195, 16387, 32771, 65539]
_ML_BITS = [0] * 32 + [1, 1, 1, 1, 2, 2, 3, 3, 4, 4, 5, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16]

# the distributions the format predefines for sequences
_LL_DEFAULT = [4, 3, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 1, 1, 1,
               2, 2, 2, 2, 2, 2, 2, 2, 2, 3, 2, 1, 1, 1, 1, 1,
               -1, -1, -1, -1]
_ML_DEFAULT = [1, 4, 3, 2, 2, 2, 2, 2, 2, 1, 1, 1, 1, 1, 1, 1,
               1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1,
               1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, -1, -1,
               -1, -1, -1, -1, -1]
_OF_DEFAULT = [1, 1, 1, 1, 1, 1, 2, 2, 2, 1, 1, 1, 1, 1, 1, 1,
               1, 1, 1, 1, 1, 1, 1, 1, -1, -1, -1, -1, -1]

_REV8 = bytes(int(bin(i)[2:].zfill(8)[::-1], 2) for i in range(256))
_REV16 = [int(bin(i)[2:].zfill(16)[::-1], 2) for i in range(1 << 16)]


def _revbits(value, length):
    return _REV16[value & 0xFFFF] >> (16 - length) if length <= 16 else 0


# ---------------------------------------------------------------------- bits
class _Bits(object):
    """A direct port of zstd's BIT_DStream (lib/common/bitstream.h): the stream
    is read backwards through the bytes, last byte first and high bits first
    inside a byte, through a 64-bit window that is refilled as it is used. The
    window is kept in `container` with `bits_consumed` counting how many of its
    top bits are already gone, exactly like the reference, so `reload()` can
    report the same unfinished/end-of-buffer/completed/overflow states the C
    decoder branches on."""

    UNFINISHED, END_OF_BUFFER, COMPLETED, OVERFLOW = 0, 1, 2, 3

    __slots__ = ('raw', 'size', 'start', 'limit', 'ptr', 'container', 'bits_consumed', 'auto')

    def __init__(self, raw, skip_padding=True, auto=True):
        n = len(raw)
        self.raw = raw
        self.size = n * 8
        self.start = 0
        self.limit = 8                       # start + sizeof(bitContainer)
        if n >= 8:
            ptr = n - 8
            container = int.from_bytes(raw[ptr:ptr + 8], 'little')
            last = raw[n - 1]
            consumed = (9 - last.bit_length()) if last else 0   # skip padding + stop bit
        else:
            ptr = 0
            container = int.from_bytes(raw.ljust(8, b'\x00'), 'little')
            last = raw[n - 1] if n else 0
            consumed = ((9 - last.bit_length()) if last else 0) + (8 - n) * 8
        self.ptr = ptr
        self.container = container
        self.bits_consumed = consumed if skip_padding else 0
        self.auto = auto

    @property
    def consumed(self):
        return self.bits_consumed

    def reload(self):
        consumed = self.bits_consumed
        if consumed > 64:
            return 3                                     # overflow: past the end
        if self.ptr >= self.limit:                       # plain refill
            self.ptr -= consumed >> 3
            self.bits_consumed = consumed & 7
            self.container = int.from_bytes(self.raw[self.ptr:self.ptr + 8], 'little')
            return 0
        if self.ptr == self.start:
            return 1 if consumed < 64 else 2
        nb = consumed >> 3                               # cautious refill
        result = 0
        if self.ptr - nb < self.start:
            nb = self.ptr - self.start
            result = 1
        self.ptr -= nb
        self.bits_consumed = consumed - nb * 8
        self.container = int.from_bytes(self.raw[self.ptr:self.ptr + 8], 'little')
        return result

    def peek(self, k):
        # the reference refills after every sequence/symbol group; refilling a
        # little early never changes a value, but it does keep the window from
        # running past its end in the middle of a read
        if self.auto and self.bits_consumed >= 32:
            self.reload()
        window = (self.container << (self.bits_consumed & 63)) & 0xFFFFFFFFFFFFFFFF
        return (window >> 1) >> ((63 - k) & 63)

    def skip(self, k):
        self.bits_consumed += k

    def read(self, k):
        v = self.peek(k)
        self.skip(k)
        return v

    def bits_left(self):
        return self.size - self.bits_consumed


# ------------------------------------------------------------------ FSE table
class _FseTable(object):
    __slots__ = ('symbols', 'nbits', 'base', 'log')

    def __init__(self, symbols, nbits, base, log):
        self.symbols, self.nbits, self.base, self.log = symbols, nbits, base, log


def _fse_rle(symbol):
    return _FseTable([symbol], [0], [0], 0)


def _fse_read_description(src, pos, max_symbol, max_log):
    """Parse an FSE table description (RFC 8878 4.1.1). Returns (counts, log, pos)."""
    if pos >= len(src):
        raise ZstdFrameError("FSE table description is missing")

    val = int.from_bytes(src[pos:pos + 64], 'little')
    limit = 8 * len(src[pos:pos + 64])
    bit = 4
    log = (val & 0xF) + 5
    if log > max_log:
        raise ZstdFrameError("FSE accuracy log %d is too large" % log)

    remaining = (1 << log) + 1
    threshold = 1 << log
    nbits = log + 1
    counts = []
    prev_zero = False
    while remaining > 1:
        if bit > limit:
            raise ZstdFrameError("FSE table description runs past the block")
        if len(counts) > max_symbol:
            raise ZstdFrameError("FSE table has too many symbols")
        if prev_zero:
            # zero probabilities come in runs: 2-bit repeat flags
            while True:
                flag = (val >> bit) & 3
                bit += 2
                counts.extend([0] * flag)
                if flag != 3:
                    break
        most = (2 * threshold - 1) - remaining
        low = (val >> bit) & (threshold - 1)
        if low < most:
            value = low
            bit += nbits - 1
        else:
            value = (val >> bit) & (2 * threshold - 1)
            if value >= threshold:
                value -= most
            bit += nbits
        value -= 1
        remaining -= -value if value < 0 else value   # -1 counts as one point
        counts.append(value)
        prev_zero = (value == 0)
        while remaining < threshold:
            nbits -= 1
            threshold >>= 1
    used = (bit + 7) // 8
    if remaining != 1:
        raise ZstdFrameError("FSE table description is corrupt")
    return counts, log, pos + used


def _fse_build(counts, log):
    """Spread the distribution into decode tables (RFC 8878 4.1.1)."""
    size = 1 << log
    symbols = [0] * size
    nbits = [0] * size
    base = [0] * size
    mask = size - 1
    high = size - 1

    for sym, count in enumerate(counts):
        if count == -1:                      # "less than one" probability
            symbols[high] = sym
            high -= 1
    step = (size >> 1) + (size >> 3) + 3
    pos = 0
    for sym, count in enumerate(counts):
        if count <= 0:
            continue
        for _ in range(count):
            symbols[pos] = sym
            pos = (pos + step) & mask
            while pos > high:
                pos = (pos + step) & mask
    if pos != 0:
        raise ZstdFrameError("FSE table spread did not fill up")

    next_state = [1 if c < 0 else c for c in counts]
    for state in range(size):
        sym = symbols[state]
        nxt = next_state[sym]
        next_state[sym] = nxt + 1
        nb = log - (nxt.bit_length() - 1)
        nbits[state] = nb
        base[state] = (nxt << nb) - size
    return _FseTable(symbols, nbits, base, log)


_MAX_SYMBOL = {'ll': 35, 'of': 31, 'ml': 52}

_PREDEFINED = {}


def _predefined(kind):
    table = _PREDEFINED.get(kind)
    if table is None:
        if kind == 'll':
            table = _fse_build(_LL_DEFAULT, 6)
        elif kind == 'ml':
            table = _fse_build(_ML_DEFAULT, 6)
        else:
            table = _fse_build(_OF_DEFAULT, 5)
        _PREDEFINED[kind] = table
    return table


# ------------------------------------------------------------------- Huffman
def _huffman_table(weights):
    """weights -> (lookup table, bits per code). Codes go to the symbols sorted
    by weight, shortest codes handed out last, exactly as the format describes."""
    total = 0
    for w in weights:
        if w:
            if w > 11:
                raise ZstdFrameError("Huffman weight is too large")
            total += 1 << (w - 1)
    if total == 0:
        raise ZstdFrameError("empty Huffman tree")
    max_bits = total.bit_length()                   # highbit(total) + 1
    rest = (1 << max_bits) - total
    if rest != (1 << (rest.bit_length() - 1)):      # must be a clean power of two
        raise ZstdFrameError("Huffman weights do not add up")
    weights = list(weights) + [rest.bit_length()]   # the last symbol is implied
    if max_bits > 11:
        raise ZstdFrameError("Huffman tree is too deep")

    order = sorted(range(len(weights)), key=lambda s: (weights[s], s))
    lookup = [0] * (1 << max_bits)
    code = 0
    prev_len = None
    for sym in order:
        weight = weights[sym]
        if not weight:
            continue
        length = max_bits + 1 - weight
        if prev_len is None:
            code = 0
        else:
            code += 1
            if length < prev_len:
                code >>= (prev_len - length)
        marker = (sym << 4) | length
        start = code << (max_bits - length)
        for idx in range(start, start + (1 << (max_bits - length))):
            lookup[idx] = marker
        prev_len = length
    return lookup, max_bits


def _fse_decode_stream(data, cap, table):
    """Port of FSE_decompress_usingDTable_generic: two interleaved states, four
    symbols per pass, and the reference's exact tail (which emits one last
    symbol from the other state when the reader runs past the end)."""
    bits = _Bits(data, auto=False)
    log = table.log
    symbols, nbits, base = table.symbols, table.nbits, table.base
    state1 = bits.read(log)
    bits.reload()                              # FSE_initDState refills after each read
    state2 = bits.read(log)
    bits.reload()
    out = []
    limit = cap - 3
    while bits.reload() == 0 and len(out) < limit:
        out.append(symbols[state1]); state1 = base[state1] + bits.read(nbits[state1])
        out.append(symbols[state2]); state2 = base[state2] + bits.read(nbits[state2])
        out.append(symbols[state1]); state1 = base[state1] + bits.read(nbits[state1])
        out.append(symbols[state2]); state2 = base[state2] + bits.read(nbits[state2])
    # tail: the reference emits a symbol first and only then looks at the reader,
    # and when the reader has run past the end it still emits one last symbol
    # from the other state before stopping
    while True:
        if len(out) > cap - 2:
            raise ZstdFrameError("FSE stream produced too many symbols")
        out.append(symbols[state1]); state1 = base[state1] + bits.read(nbits[state1])
        if bits.reload() == 3:
            out.append(symbols[state2])
            break
        if len(out) > cap - 2:
            raise ZstdFrameError("FSE stream produced too many symbols")
        out.append(symbols[state2]); state2 = base[state2] + bits.read(nbits[state2])
        if bits.reload() == 3:
            out.append(symbols[state1])
            break
    return out


def _read_huffman_weights(src, pos):
    """Huffman tree description -> (weights of the first symbols, new position).
    Weights cover symbols 0..n-1; the last symbol's weight is implied by the
    total and is worked out by the caller."""
    header = src[pos]
    pos += 1
    if header >= 128:                                # direct 4-bit weights
        count = header - 127
        weights = []
        for i in range(count):
            byte = src[pos + (i >> 1)]
            weights.append((byte >> 4) if not (i & 1) else (byte & 0xF))
        return weights, pos + ((count + 1) // 2)

    # FSE-compressed series of weights: one table, then one symbol per state
    size = header
    chunk = src[pos:pos + size]
    counts, log, used = _fse_read_description(chunk, 0, 255, 6)
    table = _fse_build(counts, log)
    return _fse_decode_stream(chunk[used:], 255, table), pos + size


def _huffman_decode(stream, lookup, max_bits, out_size):
    """Decode one Huffman-coded stream."""
    bits = _Bits(stream, skip_padding=True)
    out = bytearray()
    peek, skip = bits.peek, bits.skip
    while len(out) < out_size:
        marker = lookup[peek(max_bits)]
        if not marker:
            raise ZstdFrameError("bad Huffman code")
        out.append(marker >> 4)
        skip(marker & 0xF)
    return out


# ---------------------------------------------------------------- frame parts
def _literals(src, pos, state):
    """Literals section -> (bytes, new pos). The tree it used is kept in the
    frame state for the next blocks."""
    start = pos
    header = src[pos]
    pos += 1
    kind = header & 3
    fmt = (header >> 2) & 3

    if kind in (0, 1):                                # raw / RLE
        if fmt in (0, 2):
            regen = header >> 3
        elif fmt == 1:
            regen = (header >> 4) | (src[pos] << 4)
            pos += 1
        else:
            regen = (header >> 4) | (src[pos] << 4) | (src[pos + 1] << 12)
            pos += 2
        if kind == 0:
            return src[pos:pos + regen], pos + regen
        return bytes([src[pos]]) * regen, pos + 1

    if fmt == 0:
        regen = (header >> 4) | ((src[pos] & 0x3F) << 4)
        comp = (src[pos] >> 6) | (src[pos + 1] << 2)
        pos += 2
        streams = 1
    elif fmt == 1:
        regen = (header >> 4) | ((src[pos] & 0x3F) << 4)
        comp = (src[pos] >> 6) | (src[pos + 1] << 2)
        pos += 2
        streams = 4
    elif fmt == 2:
        regen = (header >> 4) | (src[pos] << 4) | ((src[pos + 1] & 3) << 12)
        comp = (src[pos + 1] >> 2) | (src[pos + 2] << 6)
        pos += 3
        streams = 4
    else:
        regen = (header >> 4) | (src[pos] << 4) | ((src[pos + 1] & 0x3F) << 12)
        comp = (src[pos + 1] >> 6) | (src[pos + 2] << 2) | (src[pos + 3] << 10)
        pos += 4
        streams = 4
    end = pos + comp

    if kind == 2:
        weights, pos = _read_huffman_weights(src, pos)
        lookup, max_bits = _huffman_table(weights)
        state.huffman = (lookup, max_bits)
    else:
        if state.huffman is None:
            raise ZstdFrameError("treeless literals with no previous tree")
        lookup, max_bits = state.huffman

    if streams == 1:
        out = _huffman_decode(src[pos:end], lookup, max_bits, regen)
    else:
        sizes = []
        for i in range(3):
            sizes.append(src[pos + 2 * i] | (src[pos + 2 * i + 1] << 8))
        head = pos + 6
        chunk = (regen + 3) // 4
        out = bytearray()
        for i in range(4):
            size = sizes[i] if i < 3 else end - head
            piece = chunk if i < 3 else regen - 3 * chunk
            out += _huffman_decode(src[head:head + size], lookup, max_bits, piece)
            head += size
    if len(out) != regen:
        raise ZstdFrameError("literals came out the wrong size")
    return bytes(out), end


def _sequences(src, pos, end, state, literals, out):
    """Sequences section -> new position. Appends the block's bytes to out."""
    first = src[pos]
    if first == 0:
        out += literals
        return pos + 1
    if first < 128:
        count = first
        pos += 1
    elif first < 255:
        count = ((first - 128) << 8) + src[pos + 1]
        pos += 2
    else:
        count = src[pos + 1] + (src[pos + 2] << 8) + 0x7F00
        pos += 3

    modes = src[pos]
    pos += 1
    tables = list(state.tables)
    for i, (kind, shift, max_log) in enumerate((('ll', 6, 9), ('of', 4, 8), ('ml', 2, 9))):
        mode = (modes >> shift) & 3
        if mode == 0:
            tables[i] = _predefined(kind)
        elif mode == 1:
            tables[i] = _fse_rle(src[pos])
            pos += 1
        elif mode == 2:
            # symbol ceilings from the format: 35 literals length codes,
            # 31 offset codes, 52 match length codes
            counts, log, pos = _fse_read_description(
                src, pos, _MAX_SYMBOL[kind], max_log)
            tables[i] = _fse_build(counts, log)
        else:
            if tables[i] is None:
                raise ZstdFrameError("%s table in repeat mode with nothing to repeat" % kind)
    state.tables = tables
    return _decode_sequences(src, pos, end, count, tables, state, literals, out)


def _decode_sequences(src, pos, end, count, tables, state, literals, out):
    """The bitstream half of a sequences section: reads backwards from `end`."""
    ll_t, of_t, ml_t = tables
    bits = _Bits(src[pos:end])
    ll_state = bits.read(ll_t.log)
    of_state = bits.read(of_t.log)
    ml_state = bits.read(ml_t.log)

    rep = state.reps
    lit_pos = 0
    for i in range(count):
        ll_code = ll_t.symbols[ll_state]
        ml_code = ml_t.symbols[ml_state]
        of_code = of_t.symbols[of_state]
        offset_value = (1 << of_code) + bits.read(of_code)
        match_len = _ML_BASE[ml_code] + bits.read(_ML_BITS[ml_code])
        lit_len = _LL_BASE[ll_code] + bits.read(_LL_BITS[ll_code])

        if i + 1 < count:
            ll_state = ll_t.base[ll_state] + bits.read(ll_t.nbits[ll_state])
            ml_state = ml_t.base[ml_state] + bits.read(ml_t.nbits[ml_state])
            of_state = of_t.base[of_state] + bits.read(of_t.nbits[of_state])

        if lit_len:
            if lit_pos + lit_len > len(literals):
                raise ZstdFrameError("not enough literals")
            out += literals[lit_pos:lit_pos + lit_len]
            lit_pos += lit_len

        if offset_value > 3:
            offset = offset_value - 3
            rep = [offset, rep[0], rep[1]]
        else:
            if lit_len == 0:
                offset_value += 1
            if offset_value == 1:
                offset = rep[0]
            elif offset_value == 2:
                offset = rep[1]
                rep = [offset, rep[0], rep[2]]
            elif offset_value == 3:
                offset = rep[2]
                rep = [offset, rep[0], rep[1]]
            else:                                    # rep[0] - 1
                offset = rep[0] - 1
                rep = [offset, rep[0], rep[1]]
        state.reps = rep
        if offset <= 0 or offset > len(out):
            raise ZstdFrameError("match offset is out of range")
        if match_len:
            start = len(out) - offset
            if offset >= match_len:
                out += out[start:start + match_len]
            else:
                piece = bytes(out[start:])
                out += (piece * (match_len // len(piece) + 1))[:match_len]
    if lit_pos < len(literals):
        out += literals[lit_pos:]
    return end


class _FrameState(object):
    """Everything a frame must carry from one block to the next."""

    __slots__ = ('reps', 'tables', 'huffman')

    def __init__(self):
        self.reps = [1, 4, 8]            # the format starts with 1, 4, 8
        self.tables = [None, None, None]  # literals length, offset, match length
        self.huffman = None


def zstd_decompress(src, max_output=None):
    """Decompress one ZSTD frame. Extra frames after it are ignored."""
    if len(src) < 4 or src[:4] != b'\x28\xb5\x2f\xfd':
        raise ZstdFrameError("not a ZSTD frame")
    pos = 4
    fhd = src[pos]
    pos += 1
    if fhd & 0x08:
        raise ZstdFrameError("reserved frame header bit is set")
    single = (fhd >> 5) & 1
    if not single:
        pos += 1                                     # window descriptor
    dict_id = fhd & 3
    if dict_id:
        raise ZstdFrameError("dictionary frames are not supported")
    fcs_size = [0, 2, 4, 8][(fhd >> 6) & 3]
    if fcs_size == 0 and single:                     # single segment always states a size
        fcs_size = 1
    content_size = None
    if fcs_size:
        if fcs_size == 2:                            # only the 2-byte form is offset
            content_size = int.from_bytes(src[pos:pos + 2], 'little') + 256
        else:
            content_size = int.from_bytes(src[pos:pos + fcs_size], 'little')
        pos += fcs_size

    out = bytearray()
    state = _FrameState()
    while True:
        if pos + 3 > len(src):
            raise ZstdFrameError("frame ended inside a block header")
        header = src[pos] | (src[pos + 1] << 8) | (src[pos + 2] << 16)
        pos += 3
        last = header & 1
        kind = (header >> 1) & 3
        size = header >> 3
        if kind == 0:                                # raw
            out += src[pos:pos + size]
            pos += size
        elif kind == 1:                              # RLE
            out += bytes([src[pos]]) * size
            pos += 1
        elif kind == 2:                              # compressed
            end = pos + size
            literals, pos = _literals(src, pos, state)
            pos = _sequences(src, pos, end, state, literals, out)
            if pos != end:
                raise ZstdFrameError("block did not end where it said it would")
        else:
            raise ZstdFrameError("reserved block type")
        if max_output is not None and len(out) > max_output:
            raise ZstdFrameError("frame is larger than expected")
        if last:
            break
    if fhd & 4:                                      # content checksum
        pos += 4
    if content_size is not None and len(out) != content_size:
        raise ZstdFrameError("frame says %d bytes but %d came out" % (content_size, len(out)))
    return bytes(out)

# ===========================================================================
#  scripts  ->  a Roblox binary place (.rbxl)
# ---------------------------------------------------------------------------
#  What the download button gives back: the same game shape, one instance per
#  script in its original folder tree, with the decompiled source in each
#  Script / LocalScript / ModuleScript.  Roblox Studio opens it directly.
#  Chunks are LZ4-compressed, exactly as Studio writes them, so the file
#  needs no package here - the compressor below is plain python.
#  The shape is checked against rbx-dom (Rojo's reader, the one Studio
#  matches): root parents are the null referent, services carry markers.
# ===========================================================================
import os
import struct



# ---------------------------------------------------------------- LZ4 block ---
def lz4_compress_block(src):
    """LZ4 block format (the one Roblox writes and lz4.block.compress makes).

    Greedy match finder over a 64K hash table; the last 5 bytes stay literals
    and no match starts in the final 12 bytes, exactly as the format requires.
    """
    n = len(src)
    out = bytearray()
    if n == 0:
        return b"\x00"

    tbl = [-1] * (1 << 16)
    mask = (1 << 16) - 1
    anchor = 0
    i = 0
    last = n - 5
    mfl = n - 12
    unpack = struct.unpack_from

    while i < mfl:
        h = (unpack("<I", src, i)[0] * 2654435761) >> 16 & mask
        cand = tbl[h]
        tbl[h] = i
        if cand >= 0 and i - cand <= 65535 and src[cand:cand + 4] == src[i:i + 4]:
            m = i + 4
            j = cand + 4
            while m < last and src[m] == src[j]:
                m += 1
                j += 1
            lit = i - anchor
            ml = m - 4 - i
            out.append(((15 if lit >= 15 else lit) << 4) | (15 if ml >= 15 else ml))
            if lit >= 15:
                r = lit - 15
                while r >= 255:
                    out.append(255)
                    r -= 255
                out.append(r)
            out += src[anchor:i]
            off = i - cand
            out.append(off & 0xFF)
            out.append((off >> 8) & 0xFF)
            if ml >= 15:
                r = ml - 15
                while r >= 255:
                    out.append(255)
                    r -= 255
                out.append(r)
            i = m
            anchor = m
        else:
            i += 1

    lit = n - anchor
    out.append((15 if lit >= 15 else lit) << 4)
    if lit >= 15:
        r = lit - 15
        while r >= 255:
            out.append(255)
            r -= 255
        out.append(r)
    out += src[anchor:]
    return bytes(out)


def _chunk(name, payload):
    """One file chunk: 4-char name, compressed size, raw size, reserved, body."""
    comp = lz4_compress_block(payload)
    return name + struct.pack("<III", len(comp), len(payload), 0) + comp


def _name(text):
    """Instance names: Roblox forbids \\ / : * ? " < > | and control characters."""
    import re as _re
    return _re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', str(text)).strip()[:80] or 'Unnamed'


def _ustr(text):
    if isinstance(text, str):
        text = text.encode("utf-8", "replace")
    return struct.pack("<I", len(text)) + text


def _zz(v):
    """zig-zag, the way Roblox stores signed deltas"""
    return ((v << 1) ^ (v >> 31)) & 0xFFFFFFFF


def _referents(vals):
    """Referent list: zig-zagged deltas, byte-interleaved."""
    deltas, prev = [], 0
    for v in vals:
        deltas.append(_zz(v - prev))
        prev = v
    raw = b"".join(struct.pack(">I", v) for v in deltas)
    count = len(deltas)
    out = bytearray(count * 4)
    for w in range(4):
        out[w * count:(w + 1) * count] = raw[w::4]
    return bytes(out)


# ------------------------------------------------------------------- builder --
#  The file has to match what Roblox Studio writes, exactly:
#    * referents are transformed (zig-zag) 32-bit ints, delta-accumulated,
#      byte-interleaved, big endian          (docs/binary.md "Referent")
#    * a root instance's parent is the null referent -1, not 0
#    * Instance names live in a "Name" string property (PROP), never in INST
#    * a service class is written with object format 1 plus one marker byte
#      per instance, or Roblox makes a duplicate copy of the service and the
#      game looks empty
#    * the END chunk carries the literal </roblox>
#  Verified against rojo/rbx-dom: the files below deserialize with rbx-dom's
#  own reader, which is what Studio uses.
SERVICES = {
    "Workspace": "Workspace",
    "Players": "Players",
    "Lighting": "Lighting",
    "ReplicatedFirst": "ReplicatedFirst",
    "ReplicatedStorage": "ReplicatedStorage",
    "ServerScriptService": "ServerScriptService",
    "ServerStorage": "ServerStorage",
    "StarterGui": "StarterGui",
    "StarterPack": "StarterPack",
    "StarterPlayer": "StarterPlayer",
    "SoundService": "SoundService",
    "Chat": "Chat",
    "Teams": "Teams",
    "TextChatService": "TextChatService",
    "MaterialService": "MaterialService",
    "LocalizationService": "LocalizationService",
    "ProximityPromptService": "ProximityPromptService",
    "TestService": "TestService",
    "VoiceChatService": "VoiceChatService",
}

# real classes that are not services, so they can sit anywhere in the tree
CONTAINERS = {
    "StarterPlayerScripts": "StarterPlayerScripts",
    "StarterCharacterScripts": "StarterCharacterScripts",
    "Camera": "Camera",
    "Terrain": "Terrain",
}


def build_binary_place(scripts):
    """scripts -> bytes of a .rbxl, ready for Roblox Studio.

    scripts: [{"path": "ServerScriptService/Main", "class": "Script", "body": "<lua>"}]
    """
    # ---- 1. lay the scripts out as a tree ----------------------------------
    root = {"class": None, "name": "", "kids": [], "index": {}, "source": None}

    def child(parent, name, cls, top, unique=False):
        """Find or create a child.  Folders are shared; two scripts may share a
        name in Roblox, so a leaf that is already taken gets " (2)", " (3)", ..."""
        node = parent["index"].get(name)
        if node is not None and not unique:
            return node
        if node is not None:
            k = 2
            while "%s (%d)" % (name, k) in parent["index"]:
                k += 1
            name = "%s (%d)" % (name, k)
        if top and name in SERVICES:
            cls = SERVICES[name]
        elif name in CONTAINERS:
            cls = CONTAINERS[name]
        node = {"class": cls, "name": name, "kids": [], "index": {}, "source": None,
                "service": bool(top and name in SERVICES)}
        parent["kids"].append(node)
        parent["index"][name] = node
        return node

    for s in scripts:
        parts = [p for p in str(s.get("path") or "").split("/") if p]
        cls = s.get("class") if s.get("class") in SCRIPT_CLASSES else "Script"
        body = s.get("body") or ""
        if not parts:                                   # can't happen, but be safe
            parts = ["Unnamed"]
        cur = root
        for i, part in enumerate(parts[:-1]):
            cur = child(cur, _name(part), "Folder", i == 0)
        leaf = child(cur, _name(parts[-1]), cls, False, unique=True)
        leaf["class"] = cls
        leaf["source"] = body

    # ---- 2. number every instance (referents are ours to choose) -----------
    flat = []          # (referent, class, name, source, parent ref, is_service)

    def walk(node, parent_ref):
        ref = len(flat) + 1
        flat.append((ref, node["class"], node["name"], node["source"], parent_ref,
                     node["service"]))
        for kid in node["kids"]:
            walk(kid, ref)

    for kid in root["kids"]:
        walk(kid, -1)                                   # a root's parent is null

    by_class = {}
    for item in flat:
        by_class.setdefault(item[1], []).append(item)
    classes = sorted(by_class)                          # unique, monotonic class IDs

    # PRNT: every instance once, children before parents (depth-first post-order),
    # which is the order Studio writes and applies relationships in.
    prnt_kids, prnt_parents = [], []
    children_of = {}
    for ref, cls, name, source, parent, service in flat:
        children_of.setdefault(parent, []).append(ref)
        children_of.setdefault(ref, [])

    seen = set()

    def emit(ref):
        if ref in seen:
            return
        seen.add(ref)
        for kid in children_of.get(ref, []):
            emit(kid)
        prnt_kids.append(ref)

    for ref in children_of.get(-1, []):
        emit(ref)

    for ref in prnt_kids:
        prnt_parents.append(next(item[4] for item in flat if item[0] == ref))

    # ---- 3. chunks ---------------------------------------------------------
    out = bytearray(b"<roblox!" + bytes([0x89, 0xFF, 0x0D, 0x0A, 0x1A, 0x0A])
                    + struct.pack("<H", 0)                    # version 0
                    + struct.pack("<i", len(classes))
                    + struct.pack("<i", len(flat))
                    + b"\x00" * 8)                            # reserved

    info = {item[0]: item for item in flat}

    for cid, cls in enumerate(classes):
        items = by_class[cls]
        any_service = any(item[5] for item in items)
        body = (struct.pack("<I", cid) + _ustr(cls)
                + bytes([1 if any_service else 0])
                + struct.pack("<I", len(items))
                + _referents([item[0] for item in items]))
        if any_service:
            body += bytes(1 if item[5] else 0 for item in items)
        out += _chunk(b"INST", body)

    for cid, cls in enumerate(classes):
        items = by_class[cls]
        body = struct.pack("<I", cid) + _ustr("Name") + bytes([0x01])
        for item in items:
            body += _ustr(item[2])
        out += _chunk(b"PROP", body)

    for cid, cls in enumerate(classes):
        if cls not in SCRIPT_CLASSES:
            continue
        items = by_class[cls]
        body = struct.pack("<I", cid) + _ustr("Source") + bytes([0x01])
        for item in items:
            body += _ustr(item[3] or "")
        out += _chunk(b"PROP", body)

    out += _chunk(b"PRNT", b"\x00" + struct.pack("<I", len(prnt_kids))
                  + _referents(prnt_kids) + _referents(prnt_parents))

    # the END chunk holds </roblox> and is never compressed
    out += b"END\x00" + struct.pack("<III", 0, 9, 0) + b"</roblox>"
    return bytes(out)

# ===========================================================================
#  Roblox binary place/model  ->  list of scripts
# ===========================================================================
def parse_binary_place(data):
    if data[:8] != b'<roblox!':
        raise RbxError("not a Roblox binary file")
    ver, _classes, _instances = struct.unpack_from('<Hii', data, 14)
    if ver != 0:
        raise RbxError("unsupported Roblox binary version %d" % ver)

    classes = {}          # cid -> (class name, [referents])
    pending = []          # (cid, prop name, [values])
    parents = {}
    off, end = 32, len(data)

    while off + 16 <= end:
        name = data[off:off + 4]
        comp_len, uncomp_len, _r = struct.unpack_from('<III', data, off + 4)
        off += 16
        body = data[off:off + (comp_len or uncomp_len)]
        off += (comp_len or uncomp_len)

        if name == b'END\x00':
            break
        chunk = _decompress_chunk(name, body, uncomp_len) if comp_len else body

        if name == b'INST':
            cid = struct.unpack_from('<I', chunk, 0)[0]
            cname, p = _read_string(chunk, 4)
            p += 1                                            # object format byte
            icount = struct.unpack_from('<I', chunk, p)[0]; p += 4
            refs, p = _read_referents(chunk, p, icount)
            classes[cid] = (cname.decode('utf-8', 'replace'), refs)
        elif name == b'PROP':
            cid = struct.unpack_from('<I', chunk, 0)[0]
            pname, p = _read_string(chunk, 4)
            tid = chunk[p]; p += 1
            pname = pname.decode('utf-8', 'replace')
            if tid in (0x01, 0x1d) and cid in classes:        # String / Bytecode
                vals = []
                for _ in range(len(classes[cid][1])):          # one value per instance
                    s, p = _read_string(chunk, p)
                    vals.append(s)
                pending.append((cid, pname, vals))
        elif name == b'PRNT':
            p = 1
            pcount = struct.unpack_from('<I', chunk, p)[0]; p += 4
            children, p = _read_referents(chunk, p, pcount)
            pars, p = _read_referents(chunk, p, pcount)
            parents.update(zip(children, pars))

    instances = {}
    for cid, (cname, refs) in classes.items():
        for r in refs:
            instances[r] = {'class': cname, 'props': {}}
    for cid, pname, vals in pending:
        for r, v in zip(classes[cid][1], vals):
            if r in instances:
                instances[r]['props'][pname] = v

    return _collect(instances, parents)


# ===========================================================================
#  Roblox XML place/model  ->  list of scripts
# ===========================================================================
def _xml_tag(el):
    return el.tag.rsplit('}', 1)[-1]


def _xml_props(item):
    """Return {prop name: text} for <Properties> children of an <Item>."""
    out = {}
    for child in item:
        if _xml_tag(child) != 'Properties':
            continue
        for prop in child:
            nm = prop.get('name')
            if nm:
                out[nm] = prop.text or ''
    return out


def parse_xml_place(data):
    text = data.decode('utf-8', errors='replace')
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise RbxError("could not read this XML file (%s)" % exc)

    scripts, count = [], 0

    def walk(node, parents):
        nonlocal count
        for child in node:
            if _xml_tag(child) != 'Item':
                continue
            cls = child.get('class') or '?'
            props = _xml_props(child)
            name = props.get('Name') or cls
            here = parents + [_safe_name(name)]
            count += 1
            if cls in SCRIPT_CLASSES:
                src = props.get('Source') or ''
                bc = props.get('Bytecode') or ''
                scripts.append({
                    'path': '/'.join(here),
                    'class': cls,
                    'source': src.encode('utf-8', 'replace'),
                    'bytecode': base64.b64decode(bc) if bc else b'',
                })
            walk(child, here)

    walk(root, [])
    return scripts, count


# ===========================================================================
#  shared: build paths, write files, render one document
# ===========================================================================
def _safe_name(name, fallback='Unnamed'):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', str(name)).strip() or fallback
    return name[:80]


def _collect(instances, parents):
    """Turn the instance table into a list of scripts with full paths."""
    scripts = []
    for ref, inst in instances.items():
        if inst['class'] not in SCRIPT_CLASSES:
            continue
        parts, cur, guard = [], ref, 0
        while cur in instances and guard < 512:
            nm = instances[cur]['props'].get('Name', '?')
            nm = nm.decode('utf-8', 'replace') if isinstance(nm, bytes) else str(nm)
            parts.append(_safe_name(nm))
            nxt = parents.get(cur)
            if nxt is None or nxt == cur or nxt not in instances:
                break
            cur = nxt
            guard += 1
        parts.reverse()
        src = inst['props'].get('Source') or b''
        bc = inst['props'].get('Bytecode') or b''
        scripts.append({'path': '/'.join(parts), 'class': inst['class'],
                        'source': src, 'bytecode': bc})
    scripts.sort(key=lambda s: (s['path'], s['class']))
    return scripts, len(instances)


def render_document(name, scripts, instance_count, fmt, extract_dir=None, notes=(), place_id=None):
    """One document: a header, then every script with a separator comment."""
    have_src = sum(1 for s in scripts if s['source'].strip())
    have_bc = sum(1 for s in scripts if not s['source'].strip() and s['bytecode'])
    empty = len(scripts) - have_src - have_bc

    head = [
        "-- " + "=" * 72,
        "-- Extracted from: %s   (%s format%s)" % (
            name, fmt, ", %d instances" % instance_count if instance_count else ""),
        "-- Scripts found: %d   |   with source: %d   |   bytecode only: %d%s"
        % (len(scripts), have_src, have_bc,
           "   |   no data: %d" % empty if empty else ""),
    ]
    if place_id:
        # the page reads this line: Download asks the engine for this id and gets
        # the whole game back as a .rbxl
        head.append('-- @place %s' % json.dumps({"id": place_id, "name": name,
                                                 "scripts": len(scripts)}))
    if extract_dir:
        head.append("-- Also written to: %s" % extract_dir)
    head.append("-- Note: source that was still stored in the file is shown as-is;")
    head.append("--       bytecode-only scripts were sent to the decompiler.")
    for n in notes:
        head.append("-- " + n)
    head.append("-- " + "=" * 72)
    head.append("")

    out = ["\n".join(head)]
    used = len(out[0])
    shown = 0
    truncated = False
    for i, s in enumerate(scripts, 1):
        body = s['source'].decode('utf-8', 'replace').rstrip('\r\n')
        if not body.strip() and s['bytecode']:
            body = s.get('decompiled') or ("-- (bytecode only - the decompiler engine did not "
                                           "answer for this script)")
        if not body.strip():
            body = "-- (this script is empty in the file)"
        # machine-readable label: the download button cuts the document into one
        # .lua file per script on this line, so nothing has to parse the artwork
        label = '-- @script {"path": %s, "class": %s, "index": %d}' % (
            json.dumps(s['path']), json.dumps(s['class']), i)
        block = "\n%s\n-- %s\n-- %s   [%s]\n-- %s\n\n%s\n" % (
            label, "-" * 70, s['path'], s['class'], "-" * 70, body)
        if used + len(block) > MAX_OUTPUT:
            truncated = True
            break
        out.append(block)
        used += len(block)
        shown += 1
    if truncated:
        out.append("\n-- @partial %d/%d\n" % (shown, len(scripts)))
        out.append("-- ===== PARTIAL ANSWER: only the first %d of %d scripts fit in one answer.\n"
                   "-- ===== Drop the file in again, or open Oracle.html on your own computer.\n"
                   % (shown, len(scripts)))
    return "".join(out)


def write_scripts(name, scripts):
    """Write every script next to the bridge, under extracted/<file name>/."""
    base = _safe_name(os.path.splitext(os.path.basename(name))[0], 'extracted')
    root = os.path.join(EXTRACT_DIR, base)
    written, seen = 0, {}
    for s in scripts:
        rel = s['path'] or s['class']
        key = rel + '|' + s['class']
        seen[key] = seen.get(key, 0) + 1
        if seen[key] > 1:
            rel = "%s (%d)" % (rel, seen[key])
        path = os.path.join(root, *(rel.split('/')[:-1] + [rel.split('/')[-1]]))
        path += {'Script': '.lua', 'LocalScript': '.local.lua',
                 'ModuleScript': '.module.lua'}.get(s['class'], '.lua')
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if s['source'].strip():
                body = s['source'].decode('utf-8', 'replace')
            elif s.get('decompiled'):
                body = s['decompiled']
            elif s['bytecode']:
                body = ("-- (bytecode only - decompilation did not produce output;\n"
                        "--  drop this .luauc directly into Oracle.html and try again)\n")
            else:
                body = "-- (empty script)\n"
            with open(path, 'w', encoding='utf-8', errors='replace') as fh:
                fh.write(body)
            written += 1
        except OSError:
            pass
    return root, written


# ===========================================================================
#  sniffing / orchestration
# ===========================================================================
def looks_like_place(raw):
    if raw[:8] == b'<roblox!':
        return 'binary'
    head = raw[:64].lstrip()
    if head.startswith(b'<?xml') or head.startswith(b'<roblox'):
        return 'xml'
    return None


def handle_place(raw, filename, decompile_one):
    fmt = looks_like_place(raw)
    t0 = time.time()
    if fmt == 'binary':
        scripts, count = parse_binary_place(raw)
    else:
        scripts, count = parse_xml_place(raw)

    notes = []
    if not scripts:
        cls = set()
        notes.append("No Script / LocalScript / ModuleScript found in this file.")
        notes.append("If this is a place saved with protected scripts, their source "
                     "may not be stored.")
    # decompile the bytecode-only ones (in parallel - big places have hundreds)
    bc_scripts = [s for s in scripts if not s['source'].strip() and s['bytecode']]
    if bc_scripts:
        todo = bc_scripts[:MAX_SCRIPTS]
        notes.append("Decompiling %d bytecode-only script(s)..." % len(todo))

        def _one(s):
            """One compiled chunk -> (decompiled text, why it failed).

            The engine is a free service, so a busy moment shows up as a bad
            answer rather than an obvious error; three tries turn most of those
            into a real decompilation instead of a gap in the answer.
            """
            why = None
            for attempt in range(3):
                try:
                    payload = {"bytecode": base64.b64encode(s['bytecode']).decode(),
                               "filename": s['path']}
                    text = decompile_one(payload)
                    if text and text.strip():
                        return text, None
                    why = "the engine sent an empty answer"
                except Exception as exc:
                    why = str(exc)
                if attempt < 2:
                    time.sleep(0.6 * (attempt + 1))
            return None, why or "the engine did not answer"

        texts, whys = [None] * len(todo), [None] * len(todo)
        if len(todo) > 1 and WORKERS > 1:
            with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
                for i, (text, why) in enumerate(pool.map(_one, todo)):
                    texts[i], whys[i] = text, why
        else:
            for i, s in enumerate(todo):
                texts[i], whys[i] = _one(s)

        done = 0
        for s, text, why in zip(todo, texts, whys):
            if text:
                s['decompiled'] = text
                done += 1
            else:
                s['decompiled'] = (
                    "-- (bytecode only - this script could not be decompiled: %s.\n"
                    "--  Drop the file in again to retry just the missing ones.)" % why)
        notes.append("Bytecode-only scripts decompiled: %d of %d." % (done, len(todo)))
        if done < len(todo):
            notes.append("%d script(s) still missing - the engine was busy or refused them."
                         % (len(todo) - done))

    extract_dir = None
    if WRITE_FILES and scripts:
        try:
            root, written = write_scripts(filename, scripts)
            extract_dir = "%s  (%d files)" % (root, written)
        except Exception as exc:
            notes.append("could not write the extracted folder: %s" % exc)

    if len(scripts) > MAX_SCRIPTS:
        notes.append("@partial %d/%d" % (MAX_SCRIPTS, len(scripts)))
        notes.append("Only the first %d of %d scripts are shown - the file has more"
                     % (MAX_SCRIPTS, len(scripts)))
        scripts = scripts[:MAX_SCRIPTS]

    # remember the scripts, so Download can rebuild them as a .rbxl place
    place_id = None
    if scripts:
        place_id = "p" + os.urandom(5).hex()
        payload = []
        for s in scripts:
            if s['source'].strip():
                # exactly the text that was in the game - not a byte more
                body = s['source'].decode('utf-8', 'replace')
            elif s.get('decompiled'):
                body = s['decompiled']
            elif s['bytecode']:
                body = "-- (bytecode only - this script could not be decompiled)\n"
            else:
                body = "-- (empty script)\n"
            payload.append({"path": s['path'], "class": s['class'], "body": body})
        _PLACES[place_id] = {"name": filename, "scripts": payload, "ts": time.time()}
        try:
            tx_save("place", place_id, json.dumps(payload).encode("utf-8"))
        except Exception:
            pass

    doc = render_document(filename, scripts, count, fmt, extract_dir, notes, place_id)
    doc += "\n-- unpacked in %.2fs\n" % (time.time() - t0)
    return doc


# ===========================================================================
#  engine call
# ===========================================================================
def forward(payload):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        UPSTREAM, data=body, method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "text/plain",
            # Cloudflare in front of the engine blocks Python's default UA
            "User-Agent": "OracleLocalBridge/2.0 (local desktop tool)",
        })
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="replace")


def diagnose(raw):
    """Friendly explanation when the input clearly isn't Luau bytecode."""
    if len(raw) < 4:
        return ("This file is empty or far too small to be compiled bytecode.")
    if raw[:8] == b'<roblox!':
        return None                      # handled as a game file elsewhere
    head = raw[:64].lstrip()
    if head.startswith(b'<?xml') or head.startswith(b'<roblox'):
        return None                      # handled as a game file elsewhere
    if raw[:4] == b'\x1bLua':
        return ("This is standard Lua 5.1 bytecode, not Luau bytecode.")
    if raw[0] >= 0x20:
        snippet = raw[:40].decode('utf-8', errors='replace').replace('\n', ' ')
        return ("This does not look like compiled bytecode - it looks like text:\n"
                "  \"%s...\"\n"
                "Send the COMPILED chunk (.luac/.luauc), or drop a whole game file\n"
                "(.rbxl/.rbxlx/.rbxm) and every script inside it will be extracted." % snippet)
    return None


# ===========================================================================
#  HTTP
# ===========================================================================
# ===========================================================================
#  chunked transfer helpers
# ===========================================================================
def split_text(text, limit=TX_PART):
    """Cut a long document into pieces that each stay under the body cap."""
    parts, i, n = [], 0, len(text)
    while i < n:
        j = min(n, i + limit)          # ascii: 1 char == 1 byte, so this fits
        while j > i and len(text[i:j].encode('utf-8')) > limit:
            j -= max(1, (j - i) // 16)
        if j <= i:
            j = i + 1
        parts.append(text[i:j])
        i = j
    return parts


def tx_purge():
    now = time.time()
    for store in (_UPLOADS, _RESULTS, _PLACES):
        for key in [k for k, v in store.items() if now - v.get('ts', 0) > TX_TTL]:
            store.pop(key, None)
    try:
        for name in os.listdir(TX_DIR):
            path = os.path.join(TX_DIR, name)
            if now - os.path.getmtime(path) > TX_TTL:
                os.remove(path)
    except OSError:
        pass


def tx_save(kind, tid, data):
    """Keep a copy on disk too, so a stalled transfer can still be finished."""
    try:
        os.makedirs(TX_DIR, exist_ok=True)
        with open(os.path.join(TX_DIR, '%s-%s' % (kind, tid)), 'wb') as fh:
            fh.write(data)
    except OSError:
        pass


def tx_load(kind, tid):
    try:
        with open(os.path.join(TX_DIR, '%s-%s' % (kind, tid)), 'rb') as fh:
            return fh.read()
    except OSError:
        return None


def tx_id(value):
    return re.sub(r'[^A-Za-z0-9_-]', '', str(value or ''))[:60]


def assemble(b64):
    return base64.b64decode(b64 + '=' * (-len(b64) % 4))


class handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "OracleLocalEngine/2.0"

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", ALLOWED_ORIGIN)
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Max-Age", "86400")

    def _reply(self, status, text, ctype="text/plain; charset=utf-8", newline=True):
        if newline and ctype.startswith("text/plain") and not text.endswith("\n"):
            text += "\n"
        data = text.encode("utf-8")
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        pass    # Vercel already logs every request

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _route(self):
        """The same handler runs at / on the local engine and at /api/decompile
        when a host mounts it, so strip a leading /api and normalise the rest."""
        path = self.path.split("?")[0]
        if path.startswith("/api/"):
            path = path[len("/api"):]
        return path.rstrip("/") or "/"

    def do_GET(self):
        if self._route() in ("/", "/health", "/status", "/decompile"):
            self._reply(200, json.dumps({
                "ok": True,
                "bridge": "Oracle local engine",
                "engine": UPSTREAM,
                "needs_api_key": False,
                "game_files_supported": [".rbxl", ".rbxlx", ".rbxm", ".rbxmx"],
                "chunked_transfer": True,
                "max_transfer_bytes": MAX_UPLOAD,
                "extract_dir": EXTRACT_DIR if WRITE_FILES else None,
            }), "application/json")
        else:
            self._reply(404, "POST your bytecode or game file here.")

    # ---- the decompiling itself, shared by the one-shot and chunked paths --
    def _decompile(self, raw, filename, options=None):
        """Return (status, text) for one uploaded file."""
        if looks_like_place(raw):
            try:
                return 200, handle_place(raw, filename, forward)
            except RbxError as exc:
                return 400, str(exc)
            except Exception as exc:
                return 500, "Could not unpack this game file: %s" % exc

        # ---- single script
        note = diagnose(raw)
        if note:
            return 400, note

        payload = {"bytecode": base64.b64encode(raw).decode(), "filename": filename}
        if isinstance(options, dict):
            payload["options"] = options

        # The Authorization header is deliberately ignored: the engine needs no
        # key, so nothing on this path can reject or revoke anything.
        try:
            return 200, forward(payload)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            if exc.code in (500, 502):
                return 502, (
                    "The engine could not read this file as Luau bytecode.\n"
                    "It must be a COMPILED Luau chunk (.luac / .luauc).\n"
                    "Check the file is not empty, truncated, or a place file.\n"
                    "(engine said: %s)" % detail.strip())
            return exc.code, "Engine rejected the request (%s): %s" % (exc.code, detail)
        except urllib.error.URLError as exc:
            return 502, ("Could not reach the decompiler engine (%s).\n"
                         "Check your internet connection." % exc.reason)
        except Exception as exc:
            return 500, "Unexpected failure: %s" % exc

    # ---- the game, as a .rbxl place file ----------------------------------
    def _handle_build(self, data):
        """Build the place the document header points at, and hand it over in
        pieces (base64 text, so it can travel the same way big answers do)."""
        req = data.get("oracle_build") or {}
        pid = tx_id(req.get("id"))
        record = _PLACES.get(pid)
        if record is None:                       # the instance may have restarted
            blob = tx_load("place", pid)
            if blob is not None:
                try:
                    record = {"name": "game", "scripts": json.loads(blob.decode("utf-8")),
                              "ts": time.time()}
                except Exception:
                    record = None
        if not record or not record.get("scripts"):
            self._reply(200, json.dumps({
                "ok": False,
                "error": "the engine no longer has this game in memory - "
                         "drop the file in again and press Download"},),
                "application/json")
            return

        try:
            blob = build_binary_place(record["scripts"])
        except Exception as exc:
            self._reply(200, json.dumps({"ok": False,
                                         "error": "could not build the place file: %s" % exc}),
                        "application/json")
            return

        tx_purge()
        b64 = base64.b64encode(blob).decode("ascii")
        tid = tx_id("b%d-%s" % (int(time.time()), os.urandom(4).hex()))
        parts = split_text(b64)
        _RESULTS[tid] = {"parts": parts, "filename": record.get("name") or "game",
                         "ts": time.time()}
        tx_save("result", tid, b64.encode("ascii"))
        self._reply(200, json.dumps({"ok": True,
                                     "result": {"id": tid, "parts": len(parts),
                                                "bytes": len(blob)}}),
                    "application/json")

    # ---- numbered pieces: how big files get in and big results get out -----
    def _handle_piece(self, data):
        req = data.get("oracle_upload") or {}
        tid = tx_id(req.get("id"))
        if not tid:
            self._reply(400, json.dumps({"ok": False, "error": "no transfer id"}), "application/json")
            return

        tx_purge()
        slot = _UPLOADS.get(tid)
        if slot is None:
            slot = _UPLOADS[tid] = {"chunks": {}, "total": 0, "filename": "file"}
        slot["ts"] = time.time()
        if req.get("filename"):
            slot["filename"] = str(req["filename"])[:200]
        if req.get("total"):
            try:
                slot["total"] = int(req["total"])
            except (TypeError, ValueError):
                pass

        # a piece of the file
        if not req.get("finish"):
            try:
                index = int(req.get("index"))
            except (TypeError, ValueError):
                self._reply(400, json.dumps({"ok": False, "error": "no piece number"}), "application/json")
                return
            chunk = data.get("chunk")
            if not isinstance(chunk, str):
                self._reply(400, json.dumps({"ok": False, "error": "piece was not text"}), "application/json")
                return
            slot["chunks"][index] = chunk
            tx_save("piece", "%s-%d" % (tid, index), chunk.encode("ascii", "ignore"))
            self._reply(200, json.dumps({"ok": True, "got": sorted(slot["chunks"])}),
                        "application/json")
            return

        # ...or "that was all of it, go ahead"
        total = slot["total"] or (max(slot["chunks"]) + 1 if slot["chunks"] else 0)
        missing = [i for i in range(max(total, 0)) if i not in slot["chunks"]]
        if missing:
            # a piece may have landed on another instance - look on disk too
            for i in list(missing):
                blob = tx_load("piece", "%s-%d" % (tid, i))
                if blob is not None:
                    slot["chunks"][i] = blob.decode("ascii", "ignore")
                    missing.remove(i)
        if missing:
            self._reply(200, json.dumps({"ok": False, "missing": missing[:200],
                                         "error": "some pieces did not arrive"}), "application/json")
            return

        blob = "".join(slot["chunks"].get(i, "") for i in range(total))
        _UPLOADS.pop(tid, None)
        try:
            raw = assemble(blob)
        except Exception:
            self._reply(200, json.dumps({"ok": False,
                                         "error": "the transferred file was not valid base64"}),
                        "application/json")
            return
        if not raw:
            self._reply(200, json.dumps({"ok": False, "error": "no data arrived"}), "application/json")
            return
        if len(raw) > MAX_UPLOAD:
            self._reply(200, json.dumps({"ok": False,
                                         "error": "That file is bigger than this version accepts."}),
                        "application/json")
            return

        status, text = self._decompile(raw, slot["filename"], data.get("options"))
        if status != 200:
            self._reply(200, json.dumps({"ok": False, "error": text.strip()}), "application/json")
            return

        parts = split_text(text)
        _RESULTS[tid] = {"parts": parts, "filename": slot["filename"], "ts": time.time()}
        tx_save("result", tid, text.encode("utf-8"))
        self._reply(200, json.dumps({"ok": True,
                                     "result": {"id": tid, "parts": len(parts),
                                                "bytes": len(text.encode("utf-8"))}}),
                    "application/json")

    def _handle_piece_result(self, data):
        req = data.get("oracle_result") or {}
        tid = tx_id(req.get("id"))
        try:
            index = int(req.get("part") or 0)
        except (TypeError, ValueError):
            index = 0
        rec = _RESULTS.get(tid)
        if rec is None:
            blob = tx_load("result", tid)          # memory gone, disk last chance
            if blob is None:
                self._reply(409, "-- The engine lost this transfer (it restarted mid-flight).\n"
                                 "-- Please drop the file in again.\n")
                return
            rec = _RESULTS[tid] = {"parts": split_text(blob.decode("utf-8", "replace")),
                                   "filename": "file", "ts": time.time()}
        rec["ts"] = time.time()
        parts = rec["parts"]
        if index < 0 or index >= len(parts):
            self._reply(400, "-- No such piece (%d).\n" % index)
            return
        self._reply(200, parts[index], newline=False)

    def _park_result(self, text):
        """Hand a big answer over in pieces instead of one reply.

        Hosts cap a single response body (4.5 MB on Vercel), and a 200 KB game
        can easily decompile to more text than that. The text is kept under a
        transfer id and the caller is told how many pieces to pull; the pieces
        come back through _handle_piece_result below, which is the same route a
        chunked upload's answer takes.
        """
        blob = text.encode("utf-8")
        tid = tx_id("r%d-%s" % (int(time.time()), os.urandom(4).hex()))
        _RESULTS[tid] = {"parts": split_text(text), "filename": "file", "ts": time.time()}
        tx_save("result", tid, blob)
        self._reply(200, json.dumps({"ok": True,
                                     "result": {"id": tid,
                                                "parts": len(_RESULTS[tid]["parts"]),
                                                "bytes": len(blob)}}),
                    "application/json")

    def do_POST(self):
        # always drain the body first: replying before reading it leaves the rest
        # of the request in the socket and breaks the next request on a kept-alive
        # connection (this is what a proxy sees as a broken pipe)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
        except Exception as exc:
            self._reply(400, "Could not read the request body: %s" % exc)
            return

        if self._route() not in ("/", "/decompile"):
            self._reply(404, "POST your bytecode or game file to /decompile")
            return

        try:
            data = json.loads(raw.decode() or "{}")
        except Exception as exc:
            self._reply(400, "Could not read the request body: %s" % exc)
            return

        if isinstance(data.get("oracle_build"), dict):
            self._handle_build(data)
            return
        if isinstance(data.get("oracle_upload"), dict):
            self._handle_piece(data)
            return
        if isinstance(data.get("oracle_result"), dict):
            self._handle_piece_result(data)
            return

        b64 = data.get("bytecode") or data.get("script") or ""
        if not b64:
            self._reply(400, "No data supplied (expected 'script' or 'bytecode').")
            return
        try:
            raw = base64.b64decode(b64, validate=True)
        except Exception:
            self._reply(400, "The payload was not valid base64.")
            return

        status, text = self._decompile(raw, data.get("filename") or "file", data.get("options"))
        if status == 200 and len(text.encode("utf-8")) > PARK_LIMIT:
            self._park_result(text)                 # too big for one reply
            return
        self._reply(status, text)
