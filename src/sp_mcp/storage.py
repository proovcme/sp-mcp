import hashlib
import json
import os
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone

from .errors import NormsError
from .identity import candidate_code, prefer_candidate


def now():
    return datetime.now(timezone.utc).isoformat()


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def save_blob(root, digest, data):
    target = root / "raw" / digest
    if target.exists():
        if sha(target.read_bytes()) != digest:
            raise NormsError("INTEGRITY_ERROR", "Existing source blob was modified")
        return
    fd, name = tempfile.mkstemp(dir=root / "raw", prefix=".incoming-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(name, target)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class Store:
    def __init__(self, root):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        (root / "raw").mkdir(exist_ok=True)
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS snapshots (
                    id TEXT PRIMARY KEY, url TEXT NOT NULL, sha256 TEXT NOT NULL,
                    observed_at TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS observations (
                    snapshot_id TEXT NOT NULL, checked_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS candidates (
                    url TEXT PRIMARY KEY, code TEXT, title TEXT NOT NULL, source TEXT NOT NULL,
                    payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS catalog_pages (
                    url TEXT PRIMARY KEY, checked_at TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS verifications (
                    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE VIRTUAL TABLE IF NOT EXISTS text_search USING fts5(
                    snapshot_id UNINDEXED, code, title, text, tokenize='unicode61');
                CREATE INDEX IF NOT EXISTS snapshots_url ON snapshots(url, observed_at);
            """)

    @contextmanager
    def db(self):
        with sqlite3.connect(self.root / "norms.sqlite3", timeout=30) as db:
            db.row_factory = sqlite3.Row
            yield db

    def save(self, fetched, document):
        digest = sha(fetched["raw"])
        extraction_digest = sha(dump(document).encode())
        ident = sha((fetched["url"] + "\n" + digest + "\n" + extraction_digest).encode())[:32]
        save_blob(self.root, digest, fetched["raw"])
        payload = document | {
            "snapshot_id": ident,
            "provenance": {
                "source_url": fetched["url"],
                "sha256": digest,
                "text_sha256": sha(document.get("text", "").encode()),
                "extraction_sha256": extraction_digest,
                "checked_at": fetched["checked_at"],
                "source": document["source"],
                "authority": document["authority"],
                "parser_version": "0.1.0",
            },
        }
        with self.db() as db:
            inserted = db.execute(
                "INSERT OR IGNORE INTO snapshots VALUES (?,?,?,?,?)",
                (ident, fetched["url"], digest, fetched["checked_at"], dump(payload)),
            ).rowcount
            db.execute("INSERT INTO observations VALUES (?,?)", (ident, fetched["checked_at"]))
            if inserted:
                db.execute(
                    "INSERT INTO text_search VALUES (?,?,?,?)",
                    (ident, document.get("code") or "", document.get("title", ""), document.get("text", "")),
                )
        return payload

    def get(self, ident, expected_sha256=None):
        with self.db() as db:
            row = db.execute("SELECT * FROM snapshots WHERE id=?", (ident,)).fetchone()
            if not row:
                raise NormsError("SNAPSHOT_NOT_FOUND", "Snapshot not present in this local database")
            checked = db.execute(
                "SELECT MAX(checked_at) FROM observations WHERE snapshot_id=?", (ident,)
            ).fetchone()[0]
        path = self.root / "raw" / row["sha256"]
        if not path.exists() or sha(path.read_bytes()) != row["sha256"]:
            raise NormsError("INTEGRITY_ERROR", "Source bytes are missing or their checksum changed")
        if expected_sha256 and expected_sha256 != row["sha256"]:
            raise NormsError("SOURCE_CHANGED", "Expected SHA-256 does not match selected snapshot")
        payload = json.loads(row["payload"])
        extraction = {k: v for k, v in payload.items() if k not in {"snapshot_id", "provenance"}}
        if sha(dump(extraction).encode()) != payload["provenance"]["extraction_sha256"]:
            raise NormsError("INTEGRITY_ERROR", "Saved extraction payload was modified")
        payload["provenance"]["checked_at"] = checked
        return payload

    def latest(self, url):
        with self.db() as db:
            row = db.execute(
                """SELECT s.id FROM snapshots s JOIN observations o ON s.id=o.snapshot_id
                WHERE s.url=? ORDER BY o.checked_at DESC LIMIT 1""",
                (url,),
            ).fetchone()
        return self.get(row[0]) if row else None

    def add_candidates(self, items):
        with self.db() as db:
            for item in items:
                previous = db.execute("SELECT payload FROM candidates WHERE url=?", (item["url"],)).fetchone()
                item = prefer_candidate(json.loads(previous[0]) if previous else None, item)
                db.execute(
                    "INSERT OR REPLACE INTO candidates VALUES (?,?,?,?,?)",
                    (item["url"], item.get("code"), item["title"], item["source"], dump(item)),
                )

    def save_catalog(self, fetched, payload):
        digest = sha(fetched["raw"])
        save_blob(self.root, digest, fetched["raw"])
        payload = payload | {
            "provenance": {
                "source_url": fetched["url"],
                "sha256": digest,
                "checked_at": fetched["checked_at"],
            }
        }
        with self.db() as db:
            db.execute(
                "INSERT OR REPLACE INTO catalog_pages VALUES (?,?,?)",
                (fetched["url"], fetched["checked_at"], dump(payload)),
            )
        return payload

    def catalog(self, url):
        with self.db() as db:
            row = db.execute("SELECT payload FROM catalog_pages WHERE url=?", (url,)).fetchone()
        if not row:
            return None
        payload = json.loads(row[0])
        digest = payload["provenance"]["sha256"]
        path = self.root / "raw" / digest
        if not path.exists() or sha(path.read_bytes()) != digest:
            raise NormsError("INTEGRITY_ERROR", "Saved catalogue bytes failed checksum verification")
        return payload

    def candidates(self, query, limit=20, source=None):
        from .linguistics import relevance

        with self.db() as db:
            rows = db.execute(
                "SELECT * FROM candidates" + (" WHERE source=?" if source else ""),
                (source,) if source else (),
            ).fetchall()
        scored = [
            (relevance(query, row["title"] + " " + (row["code"] or "")), json.loads(row["payload"]))
            for row in rows
        ]
        hits = [item for score, item in sorted(scored, key=lambda x: -x[0]) if score > 0]
        return [item | {"code": candidate_code(item)} for item in hits[:limit]]

    def search_text(self, query, limit=10, offset=0):
        import re

        tokens = re.findall(r"[\w]+", query, re.UNICODE)[:20]
        if not tokens:
            return []
        expression = " AND ".join('"' + token + '"*' for token in tokens)
        with self.db() as db:
            rows = db.execute(
                """SELECT snapshot_id, code, title,
                snippet(text_search, 3, '[', ']', '…', 40) excerpt FROM text_search
                WHERE text_search MATCH ? ORDER BY rank LIMIT ? OFFSET ?""",
                (expression, limit, offset),
            ).fetchall()
        return [dict(row) for row in rows]

    def stats(self):
        with self.db() as db:
            return {
                table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("snapshots", "candidates", "observations", "verifications")
            }

    def save_verification(self, report):
        report = report | {"checked_at": now()}
        ident = sha(dump(report).encode())[:32]
        with self.db() as db:
            db.execute(
                "INSERT INTO verifications VALUES (?,?,?)", (ident, report["checked_at"], dump(report))
            )
        return report | {"verification_id": ident}

    def verification(self, ident):
        with self.db() as db:
            row = db.execute("SELECT payload FROM verifications WHERE id=?", (ident,)).fetchone()
        if not row:
            raise NormsError(
                "VERIFICATION_NOT_FOUND", "Check status first and use the returned verification_id"
            )
        report = json.loads(row[0])
        if sha(dump(report).encode())[:32] != ident:
            raise NormsError("INTEGRITY_ERROR", "Saved verification payload was modified")
        return report | {"verification_id": ident}

    def library(self, query="", limit=30, offset=0, code=None, excluded_codes=None):
        """Latest extraction per URL, from the same database used by MCP; no network."""
        with self.db() as db:
            rows = db.execute("""SELECT s.id,s.payload,MAX(o.checked_at) checked_at
                FROM snapshots s JOIN observations o ON o.snapshot_id=s.id
                GROUP BY s.id ORDER BY checked_at DESC,s.id""").fetchall()
        seen, items = set(), []
        for row in rows:
            doc = json.loads(row["payload"])
            # Pre-release development extractions lack the immutable extraction manifest.
            if not doc.get("provenance", {}).get("extraction_sha256"):
                continue
            if code and doc.get("code") != code:
                continue
            if excluded_codes and doc.get("code") in excluded_codes:
                continue
            if any(
                token not in (doc["title"] + " " + doc["code"]).casefold()
                for token in query.casefold().split()
            ):
                continue
            url = doc["provenance"]["source_url"]
            if url in seen:
                continue
            seen.add(url)
            doc["provenance"]["checked_at"] = row["checked_at"]
            items.append({k: v for k, v in doc.items() if k not in {"text", "blocks", "tables", "images"}})
        return {
            "items": items[offset : offset + limit],
            "total": len(items),
            "next_offset": offset + limit if offset + limit < len(items) else None,
        }

    def verification_history(self, snapshot_id=None, limit=30, offset=0):
        with self.db() as db:
            where = " WHERE json_extract(payload,'$.snapshot_id')=?" if snapshot_id else ""
            args = (snapshot_id,) if snapshot_id else ()
            total = db.execute("SELECT count(*) FROM verifications" + where, args).fetchone()[0]
            rows = db.execute(
                "SELECT id FROM verifications" + where + " ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (*args, limit, offset),
            ).fetchall()
        return {
            "items": [self.verification(row[0]) for row in rows],
            "total": total,
            "next_offset": offset + limit if offset + limit < total else None,
        }
