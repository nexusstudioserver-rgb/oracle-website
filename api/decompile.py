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
MAX_SCRIPTS = 4000
MAX_OUTPUT = 24 * 1024 * 1024          # bytes of text returned to the editor
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
MAX_UPLOAD = 64 * 1024 * 1024          # assembled size cap for the chunked path
_UPLOADS = {}                          # id -> {"chunks": {i: b64}, "total", "filename", "ts"}
_RESULTS = {}                          # id -> {"parts": [str], "filename", "ts"}

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
    if body[:4] == b'\x28\xb5\x2f\xfd':                   # ZSTD frame magic
        try:
            import zstandard
        except ImportError:
            raise RbxError(
                "This place stores its data with ZSTD compression, which needs one "
                "extra package.\nOn your own computer:  pip install zstandard   then "
                "try again.\n(The website version cannot open ZSTD-compressed places.)\n"
                "(Or re-save the place from Studio as XML / .rbxlx and drop that - "
                "XML needs no packages at all.)")
        return zstandard.ZstdDecompressor().decompress(body, max_output_size=uncomp_len)
    return lz4_decompress(body, uncomp_len)


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


def render_document(name, scripts, instance_count, fmt, extract_dir=None, notes=()):
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
    if extract_dir:
        head.append("-- Also written to: %s" % extract_dir)
    head.append("-- Note: source that was still stored in the file is shown as-is;")
    head.append("--       bytecode-only scripts were sent to the decompiler.")
    for n in notes:
        head.append("-- " + n)
    head.append("-- " + "=" * 72)
    head.append("")

    out = ["\n".join(head)]
    total = len(head)
    truncated = False
    for s in scripts:
        body = s['source'].decode('utf-8', 'replace').rstrip('\r\n')
        if not body.strip() and s['bytecode']:
            body = s.get('decompiled') or "-- (bytecode only - decompilation failed or was skipped)"
        if not body.strip():
            body = "-- (this script is empty in the file)"
        block = "\n-- %s\n-- %s   [%s]\n-- %s\n\n%s\n" % (
            "-" * 70, s['path'], s['class'], "-" * 70, body)
        if sum(len(x) for x in out) + len(block) > MAX_OUTPUT:
            truncated = True
            break
        out.append(block)
        total += 1
    if truncated:
        out.append("\n-- ... output cut short: too many/large scripts to show at once.\n"
                   "-- Check the extracted folder for the rest.\n")
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
        path += ".%s.lua" % s['class'].lower().replace('script', '')
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
        notes.append("Decompiling %d bytecode-only script(s)..." % len(bc_scripts))
        todo = bc_scripts[:MAX_SCRIPTS]

        def _one(s):
            try:
                payload = {"bytecode": base64.b64encode(s['bytecode']).decode(),
                           "filename": s['path']}
                return decompile_one(payload)
            except Exception as exc:
                return "-- decompilation failed: %s" % exc

        if len(todo) > 1 and WORKERS > 1:
            with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
                for s, text in zip(todo, pool.map(_one, todo)):
                    s['decompiled'] = text
        else:
            for s in todo:
                s['decompiled'] = _one(s)

    extract_dir = None
    if WRITE_FILES and scripts:
        try:
            root, written = write_scripts(filename, scripts)
            extract_dir = "%s  (%d files)" % (root, written)
        except Exception as exc:
            notes.append("could not write the extracted folder: %s" % exc)

    if len(scripts) > MAX_SCRIPTS:
        notes.append("Only the first %d scripts are shown." % MAX_SCRIPTS)
        scripts = scripts[:MAX_SCRIPTS]

    doc = render_document(filename, scripts, count, fmt, extract_dir, notes)
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
    for store in (_UPLOADS, _RESULTS):
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
        self._reply(status, text)
