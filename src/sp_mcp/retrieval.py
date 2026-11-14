"""Hybrid BM25/RRF retrieval with optional local DigUp embedding worker. No generation API."""

import atexit
import base64
import difflib
import hashlib
import json
import math
import os
import queue
import re
import struct
import subprocess
import tempfile
import threading
from pathlib import Path

from .errors import NormsError
from .linguistics import concepts, stems
from .storage import dump, now, sha


def bounded_utf8(value, size=1400):
    return value.encode("utf-8")[:size].decode("utf-8", errors="ignore")


class LocalEmbeddings:
    def __init__(self, config):
        self.lock = threading.RLock()
        self.sequence = 0
        self.log = tempfile.TemporaryFile()
        models = Path(config["models"])
        assets = {}
        for name in ("embeddinggemma-2-Q8_0.gguf", "mmproj-embeddinggemma-2-Q8_0.gguf"):
            path = models / name
            if path.is_file():
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    while block := stream.read(4 * 1024 * 1024):
                        digest.update(block)
                assets[name] = digest.hexdigest()
        command = config["command"]
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.log,
            env={**os.environ, "DIGUP_MODELS": str(models)},
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self.incoming = queue.Queue()

        def read():
            try:
                for line in self.process.stdout:
                    self.incoming.put(line)
            finally:
                self.incoming.put(None)

        threading.Thread(target=read, daemon=True).start()
        ready = self.receive()
        if not ready.get("ready"):
            self.close()
            raise NormsError("EMBEDDING_UNAVAILABLE", "Local embedding worker did not become ready")
        info = ready.get("info", {})
        if not isinstance(info.get("dim"), int) or not 1 <= info["dim"] <= 4096:
            self.close()
            raise NormsError("EMBEDDING_PROTOCOL_ERROR", "Invalid embedding dimension")
        self.info = {k: info.get(k) for k in ("model", "revision", "dim", "runtime", "dtype")}
        self.info["asset_sha256"] = assets
        self.space = sha(dump(self.info).encode())
        atexit.register(self.close)

    def receive(self):
        try:
            raw = self.incoming.get(timeout=120)
        except queue.Empty as exc:
            raise NormsError("EMBEDDING_TIMEOUT", "Local embedding worker timed out", retryable=True) from exc
        if not raw or len(raw) > 4 * 1024 * 1024:
            raise NormsError("EMBEDDING_PROTOCOL_ERROR", "Local embedding worker stopped or reply too large")
        return json.loads(raw)

    def encode(self, texts, query=False):
        with self.lock:
            self.sequence += 1
            inputs = [
                bounded_utf8(
                    ("task: search result | query: " if query else "title: Normative document | text: ")
                    + text
                )
                for text in texts
            ]
            raw = dump({"id": self.sequence, "op": "text", "inputs": inputs}) + "\n"
            self.process.stdin.write(raw.encode())
            self.process.stdin.flush()
            result = self.receive()
            dim = self.info["dim"]
            if (
                result.get("id") != self.sequence
                or not result.get("ok")
                or result.get("count") != len(texts)
                or result.get("dim") != dim
            ):
                raise NormsError("EMBEDDING_PROTOCOL_ERROR", "Unexpected local embedding reply")
            data = base64.b64decode(result["vectors"], validate=True)
            if len(data) != 4 * dim * len(texts):
                raise NormsError("EMBEDDING_PROTOCOL_ERROR", "Embedding vector size mismatch")
            result_vectors = []
            for index, error in enumerate(result.get("errors", [])):
                if error:
                    raise NormsError("EMBEDDING_INPUT_ERROR", "Local embedding worker could not encode input")
                vector = struct.unpack("<" + "f" * dim, data[index * 4 * dim : (index + 1) * 4 * dim])
                norm = math.sqrt(sum(v * v for v in vector))
                if not math.isfinite(norm) or norm < 1e-12:
                    raise NormsError("EMBEDDING_PROTOCOL_ERROR", "Nonfinite or empty vector")
                result_vectors.append([v / norm for v in vector])
            if len(result_vectors) != len(texts):
                raise NormsError("EMBEDDING_PROTOCOL_ERROR", "Missing per-input embedding status")
            return result_vectors

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.log.close()


class Retrieval:
    def __init__(self, store):
        self.store, self.worker = store, None
        self.lock = threading.RLock()
        self.job_lock = threading.Lock()
        with store.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS retrieval_segments(id TEXT PRIMARY KEY,kind TEXT,payload TEXT,active INTEGER);
                CREATE VIRTUAL TABLE IF NOT EXISTS retrieval_fts USING fts5(id UNINDEXED,terms);
                CREATE TABLE IF NOT EXISTS retrieval_vectors(id TEXT,space TEXT,vector BLOB,PRIMARY KEY(id,space));
                CREATE TABLE IF NOT EXISTS retrieval_meta(key TEXT PRIMARY KEY,payload TEXT);
            """)

    def configuration(self):
        path = Path(os.getenv("SP_MCP_EMBEDDING_CONFIG") or self.store.root / "embedding-worker.json")
        if not path.is_file():
            return None
        config = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(config.get("command"), list)
            or not config["command"]
            or not all(isinstance(s, str) for s in config["command"])
            or not Path(config["command"][0]).is_file()
            or not Path(config.get("models", "")).is_dir()
        ):
            raise NormsError("EMBEDDING_CONFIG_ERROR", "Embedding executable or model directory unavailable")
        return config

    def provider(self):
        with self.lock:
            if self.worker is None or self.worker.process.poll() is not None:
                config = self.configuration()
                if not config:
                    raise NormsError("SEMANTIC_NOT_CONFIGURED", "Configure a local embedding worker first")
                self.worker = LocalEmbeddings(config)
            return self.worker

    def sync(self):
        entries = []
        with self.store.db() as db:
            candidates = [json.loads(row[0]) for row in db.execute("SELECT payload FROM candidates")]
        for item in candidates:
            label = item["title"].split("Открыть карточку")[0]
            if not item.get("change_number"):
                entries.append(
                    {
                        "kind": "title",
                        "code": item["code"],
                        "url": item["url"],
                        "title": label,
                        "text": bounded_utf8(label),
                        "source": item["source"],
                    }
                )
        latest = {}
        for item in self.store.library(limit=10000)["items"]:
            if item["text_status"] == "metadata_only":
                continue
            previous = latest.get(item["code"])
            if not previous or (item.get("edition_date") or "") > (previous.get("edition_date") or ""):
                latest[item["code"]] = item
        for item in latest.values():
            try:
                doc = self.store.get(item["snapshot_id"])
            except NormsError:
                continue
            for block in doc["blocks"]:
                match = re.match(r"^((?:[А-ЯA-Z]\.)?\d+(?:\.\d+)+)\s", block["text"])
                heading = match[1] if match else None
                for local_offset in range(0, len(block["text"]), 550):
                    text = block["text"][local_offset : local_offset + 650]
                    if len(text) < 40:
                        continue
                    entries.append(
                        {
                            "kind": "text",
                            "code": doc["code"],
                            "snapshot_id": doc["snapshot_id"],
                            "url": doc["provenance"]["source_url"],
                            "title": doc["title"],
                            "clause": heading,
                            "start": block["start"] + local_offset,
                            "end": block["start"] + local_offset + len(text),
                            "text": text,
                            "sha256": doc["provenance"]["sha256"],
                            "source": doc["source"],
                        }
                    )
        with self.store.db() as db:
            db.execute("UPDATE retrieval_segments SET active=0")
            for item in entries:
                ident = sha(dump(item).encode())
                inserted = db.execute(
                    "INSERT OR IGNORE INTO retrieval_segments VALUES (?,?,?,1)",
                    (ident, item["kind"], dump(item)),
                ).rowcount
                db.execute("UPDATE retrieval_segments SET active=1 WHERE id=?", (ident,))
                if inserted:
                    db.execute(
                        "INSERT INTO retrieval_fts VALUES (?,?)", (ident, " ".join(stems(item["text"])))
                    )
        return len(entries)

    def status(self):
        with self.store.db() as db:
            total = db.execute("SELECT count(*) FROM retrieval_segments WHERE active=1").fetchone()[0]
            row = db.execute("SELECT payload FROM retrieval_meta WHERE key='job'").fetchone()
            job = json.loads(row[0]) if row else {"state": "not_started"}
            active = db.execute("SELECT payload FROM retrieval_meta WHERE key='space'").fetchone()
            space = json.loads(active[0]) if active else None
            ready = db.execute(
                """SELECT count(*) FROM retrieval_vectors v JOIN retrieval_segments s ON s.id=v.id
                WHERE v.space=? AND s.active=1""",
                (space["id"] if space else "",),
            ).fetchone()[0]
        configuration_issue = None
        try:
            configured = self.configuration() is not None
        except (ValueError, OSError, NormsError) as exc:
            configured = False
            configuration_issue = exc.code if isinstance(exc, NormsError) else type(exc).__name__
        return {
            "configured": configured,
            "configuration_issue": configuration_issue,
            "total_segments": total,
            "ready_segments": ready,
            "pending_segments": max(0, total - ready),
            "job": job,
            "model": space.get("info") if space else None,
            "lexical": "russian_snowball_bm25",
            "semantic": "local_embeddings",
            "generation": False,
        }

    def start(self):
        if not self.configuration():
            raise NormsError("SEMANTIC_NOT_CONFIGURED", "Configure local semantic search before indexing")
        if not self.job_lock.acquire(blocking=False):
            return self.status()

        def run():
            try:
                self.update_job({"state": "preparing", "processed": 0, "started_at": now()})
                self.sync()
                worker = self.provider()
                with self.store.db() as db:
                    db.execute(
                        "INSERT OR REPLACE INTO retrieval_meta VALUES (?,?)",
                        ("space", dump({"id": worker.space, "info": worker.info})),
                    )
                    rows = db.execute(
                        """SELECT s.id,s.payload FROM retrieval_segments s
                        LEFT JOIN retrieval_vectors v ON v.id=s.id AND v.space=?
                        WHERE s.active=1 AND v.id IS NULL ORDER BY s.kind DESC,s.id""",
                        (worker.space,),
                    ).fetchall()
                processed = 0
                for start in range(0, len(rows), 8):
                    batch = rows[start : start + 8]
                    vectors = worker.encode([json.loads(row["payload"])["text"] for row in batch])
                    with self.store.db() as db:
                        for row, vector in zip(batch, vectors):
                            db.execute(
                                "INSERT OR REPLACE INTO retrieval_vectors VALUES (?,?,?)",
                                (row["id"], worker.space, struct.pack("<" + "f" * len(vector), *vector)),
                            )
                    processed += len(batch)
                    self.update_job(
                        {
                            "state": "running",
                            "processed": processed,
                            "requested": len(rows),
                            "updated_at": now(),
                        }
                    )
                self.update_job({"state": "complete", "processed": processed, "finished_at": now()})
            except Exception as exc:
                self.update_job({"state": "failed", "error": type(exc).__name__, "message": str(exc)[:400]})
            finally:
                self.job_lock.release()

        threading.Thread(target=run, daemon=True).start()
        return self.status()

    def update_job(self, payload):
        with self.store.db() as db:
            db.execute("INSERT OR REPLACE INTO retrieval_meta VALUES (?,?)", ("job", dump(payload)))

    def search(self, query, mode="concept", limit=20):
        self.sync()
        tokens = set(stems(query))
        query_groups = concepts(query) if mode == "concept" else [{t} for t in tokens]
        corrections = {}
        literal = []
        with self.store.db() as db:
            if tokens:
                vocabulary = set()
                for row in db.execute("SELECT terms FROM retrieval_fts"):
                    vocabulary.update(row[0].split())
                if mode == "concept":
                    for group in query_groups:
                        for term in tuple(group):
                            if term not in vocabulary and len(term) > 5:
                                matches = difflib.get_close_matches(term, vocabulary, n=1, cutoff=0.85)
                                if matches:
                                    group.update(matches)
                                    corrections[term] = matches[0]
                expanded = set().union(*query_groups)
                expression = " OR ".join('"' + token + '"' for token in sorted(expanded))
                rows = db.execute(
                    """SELECT s.id,s.payload,bm25(retrieval_fts) rank FROM retrieval_fts f
                    JOIN retrieval_segments s ON s.id=f.id WHERE retrieval_fts MATCH ? AND s.active=1
                    ORDER BY rank LIMIT 150""",
                    (expression,),
                ).fetchall()
                literal = [(r["id"], json.loads(r["payload"])) for r in rows]

                def coverage(item):
                    present = set(stems(item["text"]))
                    return sum(bool(g & present) for g in query_groups) / max(1, len(query_groups))

                literal.sort(key=lambda r: -coverage(r[1]))
                literal = [
                    (ident, item | {"word_coverage": round(coverage(item), 3)}) for ident, item in literal
                ]
        semantic, reason = [], None
        if mode in {"semantic", "hybrid"}:
            try:
                worker = self.provider()
                vector = worker.encode([query], query=True)[0]
                with self.store.db() as db:
                    rows = db.execute(
                        """SELECT s.id,s.payload,v.vector FROM retrieval_vectors v
                        JOIN retrieval_segments s ON s.id=v.id WHERE s.active=1 AND v.space=?""",
                        (worker.space,),
                    ).fetchall()
                scores = []
                for row in rows:
                    if len(row["vector"]) != 4 * len(vector):
                        continue
                    saved = struct.unpack("<" + "f" * len(vector), row["vector"])
                    score = sum(a * b for a, b in zip(saved, vector))
                    if score >= 0.5:
                        scores.append((score, row["id"], json.loads(row["payload"])))
                scores.sort(key=lambda x: -x[0])
                semantic = [
                    (ident, item | {"similarity": round(score, 5)}) for score, ident, item in scores[:100]
                ]
                if not rows:
                    reason = "semantic_index_not_ready"
            except (NormsError, OSError, ValueError) as exc:
                reason = exc.code if isinstance(exc, NormsError) else type(exc).__name__
        combined = {}
        groups = (
            [(literal, 2.0, "concepts" if mode == "concept" else "words")]
            if mode in {"lexical", "concept"}
            else (
                [(semantic, 1.0, "meaning")]
                if mode == "semantic"
                else [(literal, 1.5, "words"), (semantic, 1.0, "meaning")]
            )
        )
        for group, weight, kind in groups:
            for rank, (ident, item) in enumerate(group):
                entry = combined.setdefault(ident, item | {"retrieval_kinds": [], "score": 0})
                entry["score"] += weight / (30 + rank)
                entry["retrieval_kinds"].append(kind)
                if item.get("similarity") is not None:
                    entry["similarity"] = item["similarity"]
        results = sorted(combined.values(), key=lambda r: -r["score"])
        return {
            "items": results[:limit],
            "mode": mode,
            "semantic_limitation": reason,
            "index": self.status(),
            "model_used": mode in {"semantic", "hybrid"} and bool(semantic),
            "query_corrections": corrections,
        }
