"""Your files in Jarvis's brain: index folders of notes, PDFs, documents and code.

Pick folders in Settings → Files. Jarvis reads every supported file in them, splits it into chunks
(per page for PDFs, per heading/paragraph for prose, per block of lines for code) and stores them in
two places on this computer:

  * the knowledge graph (~/.jarvis/graph.db):
        (:Document {id, path, name, ext, folder, mtime, size, summary, indexed, chunks})
        (:Document)-[:HasChunk]->(:Chunk {id, doc, seq, loc, text})
        (:Document)-[:About]->(:Entity)      people, organisations, projects... the file is about
        (:Chunk)-[:Cites]->(:Entity)         chunks that mention something Jarvis knows
    so files join the same graph as everything Jarvis remembers about you;
  * a full-text index (~/.jarvis/files.db, SQLite FTS5, BM25 ranking) for fast keyword search.

Folders are watched (watchdog) and re-scanned every 20 minutes and at startup, so new, edited, moved
and deleted files are picked up. Entity extraction uses one LLM call per prose document (not code),
with the cheaper `memory.model` when set, and can be turned off.

Answers cite their sources: search results carry the file name, path and page/lines.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import zipfile
from html import unescape
from pathlib import Path
from typing import Callable

from . import config
from . import memory as gm

DB_FILE = config.HOME / "files.db"

PROSE = {".txt", ".md", ".markdown", ".rst", ".org", ".tex", ".rtf", ".log", ".csv", ".tsv"}
DOCS = {".pdf", ".docx", ".pptx", ".html", ".htm", ".ipynb"}
CODE = {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".java", ".kt", ".go", ".rs", ".c", ".h", ".cpp",
        ".hpp", ".cc", ".cs", ".rb", ".php", ".swift", ".scala", ".lua", ".dart", ".r", ".m", ".sql", ".sh",
        ".bash", ".ps1", ".bat", ".css", ".scss", ".vue", ".svelte", ".json", ".yaml", ".yml", ".toml", ".ini",
        ".cfg", ".xml", ".gradle", ".proto", ".graphql"}
SUPPORTED = PROSE | DOCS | CODE
SKIP_DIRS = {"node_modules", "__pycache__", "venv", ".venv", "env", "site-packages", "dist", "build", "target",
             "bin", "obj", "out", ".git", ".hg", ".svn", ".idea", ".vscode", ".next", ".nuxt", "coverage",
             "$recycle.bin", "system volume information", "appdata"}
SKIP_FILES = re.compile(r"(\.min\.(js|css)$|-lock\.json$|\.lock$|^package-lock\.json$|\.map$)", re.I)
CHUNK_CHARS = 1400
MAX_CHUNKS = 500
MAX_TEXT_MB = 4               # plain-text and code files bigger than this are skipped
RESCAN_S = 20 * 60
DEBOUNCE_S = 2.5
# words that say "look in my files" rather than what to look for
QUERY_NOISE = {"say", "said", "says", "tell", "told", "find", "file", "files", "document", "documents", "doc", "docs",
               "note", "notes", "read", "show", "look", "write", "wrote", "written", "mention", "mentioned"}


def doc_id(path: str) -> str:
    return "doc-" + hashlib.sha1(os.path.normcase(os.path.abspath(path)).encode("utf-8")).hexdigest()[:16]


def norm(path: str) -> str:
    return os.path.normpath(os.path.abspath(os.path.expanduser(path)))


# --------------------------------------------------------------------------- text extraction
def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    if b"\x00" in raw[:4096]:
        raise ValueError("binary file")
    for enc in ("utf-8", "utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else ("utf-8", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _strip_html(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h[1-6]|tr|section|article)>", "\n", html)
    return unescape(re.sub(r"<[^>]+>", " ", html))


def extract(path: Path) -> list[tuple[str, str]]:
    """Return [(location, text)] sections: ("p. 3", ...) for PDF pages, ("slide 2", ...), ("", whole text)..."""
    ext = path.suffix.lower()
    if ext == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:  # noqa: BLE001
                raise ValueError("password-protected PDF")
        out = []
        for i, page in enumerate(reader.pages, 1):
            try:
                t = page.extract_text() or ""
            except Exception:  # noqa: BLE001
                t = ""
            if t.strip():
                out.append((f"p. {i}", t))
        return out
    if ext == ".docx":
        import docx
        d = docx.Document(str(path))
        out, head, buf = [], "", []
        for para in d.paragraphs:
            style = (para.style.name if para.style is not None else "") or ""
            if style.lower().startswith("heading") and para.text.strip():
                if buf:
                    out.append((head, "\n".join(buf)))
                head, buf = para.text.strip()[:80], [para.text]
            elif para.text.strip():
                buf.append(para.text)
        if buf:
            out.append((head, "\n".join(buf)))
        for t in d.tables:
            rows = [" | ".join(c.text.strip() for c in r.cells) for r in t.rows]
            if rows:
                out.append(("table", "\n".join(rows)))
        return out
    if ext == ".pptx":
        out = []
        with zipfile.ZipFile(path) as z:
            slides = sorted((n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                            key=lambda n: int(re.findall(r"\d+", n)[-1]))
            for n in slides:
                xml = z.read(n).decode("utf-8", errors="replace")
                text = "\n".join(unescape(t) for t in re.findall(r"<a:t>([^<]*)</a:t>", xml))
                if text.strip():
                    out.append((f"slide {re.findall(r'[0-9]+', n)[-1]}", text))
        return out
    if path.stat().st_size > MAX_TEXT_MB * 1024 * 1024:
        raise ValueError("too large")
    if ext in (".html", ".htm"):
        return [("", _strip_html(_read_text(path)))]
    if ext == ".ipynb":
        nb = json.loads(_read_text(path))
        return [(f"cell {i}", "".join(c.get("source") or [])) for i, c in enumerate(nb.get("cells") or [], 1)
                if "".join(c.get("source") or []).strip()]
    return [("", _read_text(path))]


def chunk(sections: list[tuple[str, str]], code: bool) -> list[tuple[str, str]]:
    """Split sections into ~CHUNK_CHARS pieces. Code is cut at blank lines and labelled with line numbers."""
    out = []
    for loc, text in sections:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if code:
            lines = text.split("\n")
            start, size = 0, 0
            for i, line in enumerate(lines):
                size += len(line) + 1
                if (size >= CHUNK_CHARS and not line.strip()) or size >= CHUNK_CHARS * 2 or i == len(lines) - 1:
                    piece = "\n".join(lines[start:i + 1])
                    if piece.strip():
                        out.append((f"lines {start + 1}-{i + 1}", piece))
                    start, size = i + 1, 0
            continue
        paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        head, buf = loc, ""
        for p in paras:
            m = re.match(r"^#{1,6}\s+(.+)$", p.split("\n", 1)[0])
            if m and not loc:            # markdown heading: start a new chunk named after it
                if buf:
                    out.append((head, buf))
                    buf = ""
                head = m.group(1).strip()[:80]
            while len(p) > CHUNK_CHARS * 1.5:   # one huge paragraph (PDF text often has no blank lines)
                cut = p.rfind(". ", 0, CHUNK_CHARS) + 1 or p.rfind("\n", 0, CHUNK_CHARS) or CHUNK_CHARS
                cut = cut if cut > CHUNK_CHARS // 3 else CHUNK_CHARS
                if buf:
                    out.append((head, buf))
                    buf = ""
                out.append((head, p[:cut].strip()))
                p = p[cut:].strip()
            if buf and len(buf) + len(p) > CHUNK_CHARS:
                out.append((head, buf))
                tail = buf[-200:]                    # a little overlap keeps sentences that span chunks findable
                buf = tail[tail.find(" ") + 1:] + "\n\n" + p if " " in tail else p
            else:
                buf = (buf + "\n\n" + p) if buf else p
        if buf.strip():
            out.append((head, buf))
    return [(loc, t) for loc, t in out if len(t.strip()) >= 20][:MAX_CHUNKS]


def wanted(path: Path) -> bool:
    if path.suffix.lower() not in SUPPORTED or SKIP_FILES.search(path.name) or path.name.startswith(("~$", ".")):
        return False
    return not any(p.lower() in SKIP_DIRS or (p.startswith(".") and len(p) > 1) for p in path.parts[:-1])


ENTITY_SYSTEM = """You index a document from the user's own files into their personal knowledge graph.
From the excerpt, give a one-sentence summary of what the document is, and the few named entities it is
mainly about (people, organisations, projects, places, products, events: at most 8, the important ones,
canonical names, no generic concepts).
type is one of: Person, Organization, Place, Project, Event, Thing, Concept, Device.
Reply with JSON only: {"summary": "", "entities": [{"name": "", "type": "", "summary": ""}]}"""


# --------------------------------------------------------------------------- the index
class FileIndex:
    def __init__(self, settings: dict, mem: gm.GraphMemory, emit: Callable[[dict], None] | None = None,
                 db_path: Path | str = DB_FILE, watch: bool = True):
        self.settings = settings
        self.mem = mem
        self.emit = emit or (lambda ev: None)
        self.watch_enabled = watch
        self._db_lock = threading.RLock()
        self.db = sqlite3.connect(str(db_path), check_same_thread=False)
        self.fts = self._schema()
        self._graph_schema()
        mem.on_merge = self._on_entity_merge
        self.q: queue.Queue = queue.Queue()
        self._pending: dict[str, float] = {}     # path -> due time (debounced watcher events)
        self._pending_lock = threading.Lock()
        self.current = ""
        self.progress: dict[str, dict] = {}       # folder -> {done, total, state}
        self._observer = None
        self._watched: set[str] = set()
        self._stop = threading.Event()
        self._names_cache: tuple[float, list] = (0.0, [])
        self._threads_started = False

    # ------------------------------------------------------------------ storage
    def _schema(self) -> bool:
        with self._db_lock:
            self.db.execute("CREATE TABLE IF NOT EXISTS docs(id TEXT PRIMARY KEY, path TEXT, folder TEXT, name TEXT, "
                            "ext TEXT, mtime REAL, size INTEGER, chunks INTEGER, indexed TEXT, status TEXT, "
                            "entities_done INTEGER DEFAULT 0)")
            try:
                self.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(text, name, loc UNINDEXED, "
                                "chunk_id UNINDEXED, doc_id UNINDEXED, tokenize='porter unicode61')")
                fts = True
            except sqlite3.OperationalError:     # SQLite built without FTS5: plain table + LIKE search
                self.db.execute("CREATE TABLE IF NOT EXISTS chunks(text TEXT, name TEXT, loc TEXT, chunk_id TEXT, "
                                "doc_id TEXT)")
                fts = False
            self.db.commit()
        return fts

    def _graph_schema(self):
        m = self.mem
        m.q("CREATE NODE TABLE IF NOT EXISTS Document(id STRING, path STRING, name STRING, ext STRING, folder STRING, "
            "mtime DOUBLE, size INT64, summary STRING, indexed STRING, chunks INT64, PRIMARY KEY(id))")
        m.q("CREATE NODE TABLE IF NOT EXISTS Chunk(id STRING, doc STRING, seq INT64, loc STRING, text STRING, "
            "PRIMARY KEY(id))")
        m.q("CREATE REL TABLE IF NOT EXISTS HasChunk(FROM Document TO Chunk)")
        m.q("CREATE REL TABLE IF NOT EXISTS About(FROM Document TO Entity)")
        m.q("CREATE REL TABLE IF NOT EXISTS Cites(FROM Chunk TO Entity)")

    @property
    def cfg(self) -> dict:
        return self.settings.get("files") or {}

    def folders(self) -> list[str]:
        return [norm(f) for f in self.cfg.get("folders") or [] if f]

    def _folder_of(self, path: str) -> str | None:
        p = os.path.normcase(norm(path))
        for f in self.folders():
            nf = os.path.normcase(f)
            if p == nf or p.startswith(nf.rstrip("\\/") + os.sep):
                return f
        return None

    # ------------------------------------------------------------------ lifecycle
    def start(self):
        if not self._threads_started:
            self._threads_started = True
            threading.Thread(target=self._worker, daemon=True, name="file-index").start()
            threading.Thread(target=self._rescan_loop, daemon=True, name="file-rescan").start()
        self.sync()

    def stop(self):
        self._stop.set()
        self.q.put(None)
        if self._observer is not None:
            try:
                self._observer.stop()
            except Exception:  # noqa: BLE001
                pass

    def sync(self):
        """Settings changed: drop folders that were removed, scan new ones, update watchers."""
        current = set(self.folders())
        with self._db_lock:
            known = {r[0] for r in self.db.execute("SELECT DISTINCT folder FROM docs")}
        for f in known - current:
            self.remove_folder_data(f)
        for f in current:
            self.q.put(("scan", f))
        self._sync_watchers(current)

    def _sync_watchers(self, folders: set[str]):
        if not self.watch_enabled:
            return
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError:
            return       # periodic rescans still catch changes
        if folders == self._watched and self._observer is not None:
            return
        if self._observer is not None:
            try:
                self._observer.stop()
            except Exception:  # noqa: BLE001
                pass
            self._observer = None
        self._watched = set(folders)
        if not folders:
            return
        idx = self

        class Handler(FileSystemEventHandler):
            def on_any_event(self, ev):
                if ev.event_type in ("opened", "closed_no_write"):
                    return
                for p in (getattr(ev, "src_path", ""), getattr(ev, "dest_path", "")):
                    if p:
                        idx._touch(os.fsdecode(p), ev.is_directory)

        obs = Observer()
        for f in folders:
            if os.path.isdir(f):
                try:
                    obs.schedule(Handler(), f, recursive=True)
                except Exception as e:  # noqa: BLE001
                    print(f"[jarvis] can't watch {f}: {e}")
        obs.daemon = True
        obs.start()
        self._observer = obs

    def _touch(self, path: str, is_dir: bool = False):
        if is_dir:
            if not os.path.exists(path):     # a folder was deleted or moved away
                self.q.put(("gone_dir", path))
            return
        if not wanted(Path(path)):
            return
        with self._pending_lock:
            self._pending[path] = time.time() + DEBOUNCE_S

    def _rescan_loop(self):
        last = time.time()
        while not self._stop.wait(1.0):
            now = time.time()
            with self._pending_lock:
                due = [p for p, t in self._pending.items() if t <= now]
                for p in due:
                    self._pending.pop(p, None)
            for p in due:
                self.q.put(("file", p))
            if now - last >= RESCAN_S:
                last = now
                for f in self.folders():
                    self.q.put(("scan", f))

    def _worker(self):
        while not self._stop.is_set():
            item = self.q.get()
            if item is None:
                return
            kind, arg = item
            try:
                if kind == "scan":
                    self.scan(arg)
                elif kind == "file":
                    folder = self._folder_of(arg)
                    if folder:
                        if os.path.isfile(arg):
                            self.index_file(arg, folder)
                            self._queue_entities(folder)
                        else:
                            self.remove_file(arg)
                        self._emit_status()
                elif kind == "gone_dir":
                    prefix = os.path.normcase(norm(arg)).rstrip("\\/") + os.sep
                    with self._db_lock:
                        paths = [r[0] for r in self.db.execute("SELECT path FROM docs")]
                    for p in paths:
                        if os.path.normcase(p).startswith(prefix) and not os.path.exists(p):
                            self.remove_file(p)
                    self._emit_status()
                elif kind == "entities":
                    self._extract_entities(arg)
            except Exception:  # noqa: BLE001
                traceback.print_exc()
            finally:
                self.current = ""

    # ------------------------------------------------------------------ scanning + indexing
    def walk(self, folder: str):
        for root, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if d.lower() not in SKIP_DIRS and not d.startswith(".")]
            for name in files:
                p = Path(root) / name
                if wanted(p):
                    yield str(p)

    def scan(self, folder: str):
        if folder not in self.folders():
            return
        if not os.path.isdir(folder):
            self.progress[folder] = {"done": 0, "total": 0, "state": "missing"}
            self._emit_status()
            return
        with self._db_lock:
            known = {r[0]: (r[1], r[2]) for r in
                     self.db.execute("SELECT path, mtime, size FROM docs WHERE folder=?", (folder,))}
        seen, todo = set(), []
        max_mb = float(self.cfg.get("max_file_mb") or 25)
        for p in self.walk(folder):
            seen.add(p)
            try:
                st = os.stat(p)
            except OSError:
                continue
            if st.st_size > max_mb * 1024 * 1024:
                continue
            if known.get(p) != (st.st_mtime, st.st_size):
                todo.append(p)
        for p in set(known) - seen:
            self.remove_file(p)
        self.progress[folder] = {"done": 0, "total": len(todo), "state": "indexing" if todo else "ready"}
        self._emit_status()
        for i, p in enumerate(todo, 1):
            if self._stop.is_set() or folder not in self.folders():
                return
            self.index_file(p, folder)
            self.progress[folder]["done"] = i
            if i % 10 == 0 or i == len(todo):
                self._emit_status()
        self.progress[folder]["state"] = "ready"
        self._emit_status()
        self._queue_entities(folder)

    def _queue_entities(self, folder: str):
        """Documents still waiting to be linked to entities (e.g. indexed before an API key was set)."""
        if not self.cfg.get("extract_entities", True) or not self._llm_ok():
            return
        with self._db_lock:
            ids = [r[0] for r in self.db.execute(
                "SELECT id FROM docs WHERE folder=? AND entities_done=0 AND chunks>0 AND ext NOT IN (%s)"
                % ",".join("?" * len(CODE)), (folder, *sorted(CODE)))]
        for did in ids:
            self.q.put(("entities", did))

    def _llm_ok(self) -> bool:
        from .consolidation import llm_configured
        return llm_configured(self.settings)

    def index_file(self, path: str, folder: str) -> int:
        """(Re)index one file. Returns the number of chunks stored."""
        self.current = path
        p = Path(path)
        try:
            st = p.stat()
        except OSError:
            self.remove_file(path)
            return 0
        did = doc_id(path)
        ext = p.suffix.lower()
        status, chunks = "ok", []
        try:
            chunks = chunk(extract(p), code=ext in CODE)
            if not chunks:
                status = "no text found" + (" (scanned PDF?)" if ext == ".pdf" else "")
        except Exception as e:  # noqa: BLE001
            status = f"skipped: {type(e).__name__}: {e}"[:160]
        self._delete_doc(did)
        now = gm.now()
        with self._db_lock:
            self.db.execute("INSERT OR REPLACE INTO docs(id, path, folder, name, ext, mtime, size, chunks, indexed, "
                            "status, entities_done) VALUES (?,?,?,?,?,?,?,?,?,?,0)",
                            (did, path, folder, p.name, ext, st.st_mtime, st.st_size, len(chunks), now, status))
            self.db.executemany("INSERT INTO chunks(text, name, loc, chunk_id, doc_id) VALUES (?,?,?,?,?)",
                                [(t, p.name, loc, f"{did}-{i}", did) for i, (loc, t) in enumerate(chunks)])
            self.db.commit()
        m = self.mem
        m.q("CREATE (:Document {id:$id, path:$p, name:$n, ext:$e, folder:$f, mtime:$mt, size:$s, summary:'', "
            "indexed:$t, chunks:$c})", {"id": did, "p": path, "n": p.name, "e": ext, "f": folder,
                                        "mt": float(st.st_mtime), "s": int(st.st_size), "t": now, "c": len(chunks)})
        for i, (loc, t) in enumerate(chunks):
            cid = f"{did}-{i}"
            m.q("MATCH (d:Document {id:$d}) CREATE (d)-[:HasChunk]->(:Chunk {id:$id, doc:$d, seq:$i, loc:$l, text:$x})",
                {"d": did, "id": cid, "i": i, "l": loc, "x": t})
        self._link_known_entities(did, chunks)
        return len(chunks)

    def _delete_doc(self, did: str):
        with self._db_lock:
            self.db.execute("DELETE FROM chunks WHERE doc_id=?", (did,))
            self.db.execute("DELETE FROM docs WHERE id=?", (did,))
            self.db.commit()
        self.mem.q("MATCH (c:Chunk {doc:$d}) DETACH DELETE c", {"d": did})
        self.mem.q("MATCH (d:Document {id:$d}) DETACH DELETE d", {"d": did})

    def remove_file(self, path: str):
        self._delete_doc(doc_id(path))

    def remove_folder_data(self, folder: str):
        with self._db_lock:
            ids = [r[0] for r in self.db.execute("SELECT id FROM docs WHERE folder=?", (folder,))]
        for did in ids:
            self._delete_doc(did)
        self.progress.pop(folder, None)
        self._emit_status()

    def reindex(self, folder: str | None = None):
        for f in ([norm(folder)] if folder else self.folders()):
            with self._db_lock:
                self.db.execute("UPDATE docs SET mtime=-1 WHERE folder=?", (f,))
                self.db.commit()
            self.q.put(("scan", f))

    # ------------------------------------------------------------------ entities
    def _known_names(self) -> list[tuple[str, re.Pattern]]:
        at, names = self._names_cache
        if time.time() - at < 30:
            return names
        out = []
        for e in self.mem.entities():
            n = (e["name"] or "").strip()
            if e["id"] == gm.USER_ID or len(n) < 4 or n.lower() in gm.STOP:
                continue
            out.append((e["id"], re.compile(rf"(?<![\w]){re.escape(n)}(?![\w])", re.I)))
        self._names_cache = (time.time(), out)
        return out

    def _link_known_entities(self, did: str, chunks: list[tuple[str, str]]):
        """Connect chunks to things Jarvis already knows about when they're named in the text."""
        names = self._known_names()
        if not names:
            return
        hits: dict[str, int] = {}
        for i, (_, text) in enumerate(chunks):
            for eid, rx in names:
                if rx.search(text):
                    self.mem.q("MATCH (c:Chunk {id:$c}), (e:Entity {id:$e}) MERGE (c)-[:Cites]->(e)",
                               {"c": f"{did}-{i}", "e": eid})
                    hits[eid] = hits.get(eid, 0) + 1
        for eid, n in hits.items():
            if n >= 2 or len(chunks) <= 3:
                self.mem.q("MATCH (d:Document {id:$d}), (e:Entity {id:$e}) MERGE (d)-[:About]->(e)",
                           {"d": did, "e": eid})

    def _extract_entities(self, did: str):
        with self._db_lock:
            row = self.db.execute("SELECT path, name, entities_done FROM docs WHERE id=?", (did,)).fetchone()
            if not row or row[2]:
                return
            texts = [r[0] for r in self.db.execute("SELECT text FROM chunks WHERE doc_id=? ORDER BY rowid", (did,))]
        excerpt, total = [], 0
        for t in texts:
            if total + len(t) > 7000:
                break
            excerpt.append(t)
            total += len(t)
        from . import llm
        try:
            raw = llm.complete(self.settings, ENTITY_SYSTEM, f"File: {row[1]}\n\n" + "\n\n".join(excerpt),
                               max_tokens=700, model=((self.settings.get("memory") or {}).get("model") or None))
        except Exception as e:  # noqa: BLE001  (offline, quota...): keep the text index, retry after a re-read
            print(f"[jarvis] file entities skipped for {row[1]}: {e}")
            with self._db_lock:
                self.db.execute("UPDATE docs SET entities_done=-1 WHERE id=?", (did,))
                self.db.commit()
            return
        data = gm.parse_json(raw)
        summary = str(data.get("summary") or "")[:400]
        self.mem.q("MATCH (d:Document {id:$d}) SET d.summary=$s", {"d": did, "s": summary})
        new_ids = []
        for e in (data.get("entities") or [])[:8]:
            name = str((e or {}).get("name") or "").strip()
            if len(name) < 2 or name.upper() in ("USER", "ME", "I"):
                continue
            existing = {x["name"].lower(): x["id"] for x in self.mem.entities()}
            eid = existing.get(name.lower())
            if eid is None:
                eid = self.mem.upsert_entity(name, e.get("type") or "Thing", e.get("summary") or "")
            new_ids.append(eid)
            self.mem.q("MATCH (d:Document {id:$d}), (x:Entity {id:$e}) MERGE (d)-[:About]->(x)", {"d": did, "e": eid})
        if new_ids:
            self._names_cache = (0.0, [])
            rx = [(i, r) for i, r in self._known_names() if i in set(new_ids)]
            for i, t in enumerate(texts):
                for eid, r in rx:
                    if r.search(t):
                        self.mem.q("MATCH (c:Chunk {id:$c}), (e:Entity {id:$e}) MERGE (c)-[:Cites]->(e)",
                                   {"c": f"{did}-{i}", "e": eid})
        with self._db_lock:
            self.db.execute("UPDATE docs SET entities_done=1 WHERE id=?", (did,))
            self.db.commit()
        self.emit({"type": "files_entities", "doc": did, "nodes": new_ids})

    def _on_entity_merge(self, keep: str, other: str):
        self.mem.q("MATCH (d:Document)-[:About]->(:Entity {id:$o}), (k:Entity {id:$k}) MERGE (d)-[:About]->(k)",
                   {"o": other, "k": keep})
        self.mem.q("MATCH (c:Chunk)-[:Cites]->(:Entity {id:$o}), (k:Entity {id:$k}) MERGE (c)-[:Cites]->(k)",
                   {"o": other, "k": keep})

    # ------------------------------------------------------------------ search
    def search(self, query: str, limit: int = 6, folder: str | None = None) -> list[dict]:
        words = [w for w in re.findall(r"[\w'][\w'\-.]*", query.lower()) if len(w) >= 2 and w not in gm.STOP]
        words = list(dict.fromkeys(w.strip("'.-") for w in words if w.strip("'.-")))[:16]
        if not words:
            return []
        rows = []
        with self._db_lock:
            if self.fts:
                match = " OR ".join('"' + w.replace('"', "") + '"' + ("*" if len(w) >= 4 else "") for w in words)
                try:
                    rows = self.db.execute(
                        "SELECT c.chunk_id, c.doc_id, c.loc, c.text, d.path, d.name, bm25(chunks, 1.0, 2.0) AS r "
                        "FROM chunks c JOIN docs d ON d.id = c.doc_id WHERE chunks MATCH ? "
                        + ("AND d.folder = ? " if folder else "") + "ORDER BY r LIMIT ?",
                        ((match, norm(folder)) if folder else (match,)) + (limit * 6,)).fetchall()
                except sqlite3.OperationalError:
                    rows = []
            if not rows:
                like = " OR ".join("lower(c.text) LIKE ?" for _ in words)
                rows = self.db.execute(
                    "SELECT c.chunk_id, c.doc_id, c.loc, c.text, d.path, d.name, 0 FROM chunks c "
                    f"JOIN docs d ON d.id = c.doc_id WHERE ({like}) LIMIT ?",
                    tuple(f"%{w}%" for w in words) + (limit * 6,)).fetchall()
        stems = [k for k in gm.keywords(query) if k not in QUERY_NOISE] or words
        hits = []
        for cid, did, loc, text, path, name, rank in rows:
            low, lname = text.lower(), name.lower()
            covered = sum(1 for k in stems if k in low or k in lname)
            score = covered / max(1, len(stems)) + (-float(rank or 0)) * 0.05 + \
                0.3 * sum(1 for k in stems if k in lname)
            hits.append({"chunk_id": cid, "doc_id": did, "path": path, "name": name, "loc": loc or "",
                         "text": text, "score": round(score, 3), "coverage": round(covered / max(1, len(stems)), 2)})
        hits.sort(key=lambda h: -h["score"])
        out, per_doc = [], {}
        for h in hits:                       # at most 2 chunks per file so several sources can show up
            if per_doc.get(h["doc_id"], 0) >= 2:
                continue
            per_doc[h["doc_id"]] = per_doc.get(h["doc_id"], 0) + 1
            out.append(h)
            if len(out) >= limit:
                break
        return out

    @staticmethod
    def format_hits(hits: list[dict], max_chars: int = 1200) -> str:
        blocks = []
        for i, h in enumerate(hits, 1):
            where = f"{h['name']}" + (f", {h['loc']}" if h["loc"] else "")
            text = h["text"].strip()
            if len(text) > max_chars:
                text = text[:max_chars].rsplit(" ", 1)[0] + " …"
            blocks.append(f"[{i}] {where} — {h['path']}\n{text}")
        return "\n\n".join(blocks)

    def tool(self, args: dict):
        from .tools import ToolOutput
        q = str(args.get("query") or "").strip()
        if not q:
            return ToolOutput("Give a query.", is_error=True)
        if not self.folders():
            return ToolOutput("No folders are indexed yet. The user can add some in Settings → Files.")
        hits = self.search(q, limit=int(args.get("limit") or 6), folder=args.get("folder") or None)
        self.emit({"type": "file_search", "query": q, "sources": [self.public_hit(h) for h in hits],
                   "nodes": list(dict.fromkeys(h["doc_id"] for h in hits))})
        if not hits:
            return ToolOutput(f"Nothing in the indexed files matches “{q}”.")
        return ToolOutput(self.format_hits(hits) + "\n\nCite the sources you use by file name (and page/lines).")

    @staticmethod
    def public_hit(h: dict) -> dict:
        return {k: h[k] for k in ("doc_id", "path", "name", "loc")} | {"snippet": h["text"][:220]}

    # ------------------------------------------------------------------ status, graph, opening
    def status(self) -> dict:
        with self._db_lock:
            rows = self.db.execute("SELECT folder, count(*), sum(chunks), sum(status <> 'ok') FROM docs GROUP BY folder")
            per = {r[0]: {"files": r[1], "chunks": r[2] or 0, "skipped": r[3] or 0} for r in rows}
        with self._pending_lock:
            pending = len(self._pending)
        folders = []
        for f in self.folders():
            pr = self.progress.get(f, {})
            folders.append({"path": f, **per.get(f, {"files": 0, "chunks": 0, "skipped": 0}),
                            "state": pr.get("state") or ("missing" if not os.path.isdir(f) else "queued"),
                            "done": pr.get("done", 0), "total": pr.get("total", 0)})
        return {"folders": folders, "current": self.current, "queued": self.q.qsize() + pending,
                "watching": self._observer is not None}

    def _emit_status(self):
        self.emit({"type": "files_status", **self.status()})

    def is_indexed(self, path: str) -> bool:
        with self._db_lock:
            return self.db.execute("SELECT 1 FROM docs WHERE id=?", (doc_id(path),)).fetchone() is not None

    def graph_part(self, limit: int = 300) -> dict:
        """Documents linked to entities, for the memory graph view (chunks stay out: too many)."""
        rows = self.mem.q("MATCH (d:Document)-[:About]->(e:Entity) RETURN d.id, d.name, d.summary, d.indexed, d.path, "
                          "collect(e.id) ORDER BY d.indexed DESC LIMIT $n", {"n": limit})
        nodes, edges = [], []
        for did, name, summary, indexed, path, ents in rows:
            nodes.append({"id": did, "name": name, "type": "Document", "summary": summary or path,
                          "updated": indexed or "", "mentions": len(ents), "path": path})
            for e in ents:
                edges.append({"id": f"{did}>{e}", "source": did, "target": e, "relation": "about",
                              "fact": f"{name} is about this", "created": indexed or "", "valid": True,
                              "invalidated": "", "uses": 0, "reinforced": "", "reason": ""})
        return {"nodes": nodes, "edges": edges}

    def document(self, did: str) -> dict | None:
        rows = self.mem.q("MATCH (d:Document {id:$d}) OPTIONAL MATCH (d)-[:About]->(e:Entity) "
                          "RETURN d.name, d.path, d.summary, d.indexed, d.chunks, collect(e.id)", {"d": did})
        if not rows:
            return None
        r = rows[0]
        return {"id": did, "name": r[0], "path": r[1], "summary": r[2], "indexed": r[3], "chunks": r[4],
                "entities": [x for x in r[5] or [] if x]}


def open_path(path: str):
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def pick_folder() -> str:
    """Show the OS folder picker on this PC (the backend runs locally next to the app)."""
    if sys.platform == "win32":
        ps = ("Add-Type -AssemblyName System.Windows.Forms;"
              "$f = New-Object System.Windows.Forms.FolderBrowserDialog;"
              "$f.Description = 'Choose a folder for Jarvis to read';$f.ShowNewFolderButton = $false;"
              "$o = New-Object System.Windows.Forms.Form -Property @{TopMost=$true};"
              "if ($f.ShowDialog($o) -eq 'OK') { [Console]::Out.Write($f.SelectedPath) }")
        r = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", ps], capture_output=True, text=True,
                           timeout=600, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.stdout.strip()
    if sys.platform == "darwin":
        r = subprocess.run(["osascript", "-e", 'POSIX path of (choose folder with prompt "Choose a folder for Jarvis")'],
                           capture_output=True, text=True, timeout=600)
        return r.stdout.strip()
    for cmd in (["zenity", "--file-selection", "--directory"], ["kdialog", "--getexistingdirectory"]):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            return r.stdout.strip()
        except FileNotFoundError:
            continue
    return ""
