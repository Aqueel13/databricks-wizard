"""Settings, state store, sinks and manifests.

Landing layout (under the volume root):
  sap/cdc/<table>/<yyyy-mm-dd>/<batch_id>.jsonl.gz     one Debezium event per line, wrapped with Kafka coordinates
  coupa/<entity>/<yyyy-mm-dd>/<batch_id>.jsonl.gz       one API record per line
  hicx/<export_date>/<file>                             export files as delivered
  docs/<owner>/<...>.pdf                                documents as delivered
  public/ofac/<snapshot_date>/<file>.csv
  _manifests/<source>/<batch_id>.json                   written LAST; lists every file of the batch

A batch is complete when its manifest exists. The ingest job is triggered by new manifests.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(os.environ.get("WIZARD_ROOT", Path(__file__).resolve().parents[2]))
VOLUME = os.environ.get("WIZARD_LANDING", "/Volumes/wizard/bronze/landing")
GT_VOLUME = os.environ.get("WIZARD_GT_VOLUME", "/Volumes/wizard/ops/ground_truth")
PROFILE = os.environ.get("DATABRICKS_CONFIG_PROFILE", "wizard")
KAFKA = os.environ.get("WIZARD_KAFKA", "localhost:19092")
COUPA_URL = os.environ.get("WIZARD_COUPA_URL", "http://localhost:8081")
COUPA_KEY = os.environ.get("COUPA_API_KEY", "wizard-dev-key")
STATE_DIR = Path(os.environ.get("WIZARD_STATE_DIR", ROOT / "data"))
UPLOAD_WORKERS = int(os.environ.get("WIZARD_UPLOAD_WORKERS", "8"))
LOCAL_LANDING = ROOT / "data" / "landing_local"


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_batch_id(source: str, entity: str) -> str:
    return f"{source}-{entity}-{utcnow():%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}"


# --------------------------------------------------------------------------- state
class State:
    """Watermarks and shipped-file registry (SQLite, local to the shipper)."""

    def __init__(self, sink_kind: str = "volume") -> None:
        # separate state per sink, so a local test run never moves the real watermarks
        path = STATE_DIR / f"shipper_state_{sink_kind}.sqlite"
        self.kind = sink_kind
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript("""
            create table if not exists watermark (source text, key text, value text, primary key (source, key));
            create table if not exists shipped (path text primary key, sha256 text, shipped_at text);
            create table if not exists batch (batch_id text primary key, source text, entity text, files integer,
                                              records integer, bytes integer, created_at text);
        """)

    def get(self, source: str, key: str) -> str | None:
        r = self.db.execute("select value from watermark where source=? and key=?", (source, key)).fetchone()
        return r[0] if r else None

    def set(self, source: str, key: str, value: str) -> None:
        self.db.execute("insert or replace into watermark values (?,?,?)", (source, key, value))
        self.db.commit()

    def shipped_sha(self, path: str) -> str | None:
        r = self.db.execute("select sha256 from shipped where path=?", (path,)).fetchone()
        return r[0] if r else None

    def mark_shipped(self, path: str, sha: str) -> None:
        self.db.execute("insert or replace into shipped values (?,?,?)", (path, sha, utcnow().isoformat()))
        self.db.commit()

    def log_batch(self, m: dict) -> None:
        self.db.execute("insert or replace into batch values (?,?,?,?,?,?,?)",
                        (m["batch_id"], m["source"], m["entity"], len(m["files"]),
                         sum(f["records"] for f in m["files"]), sum(f["bytes"] for f in m["files"]), m["created_at"]))
        self.db.commit()


# --------------------------------------------------------------------------- sinks
class Sink:
    root: str

    def put(self, rel: str, data: bytes) -> str:
        raise NotImplementedError

    def full(self, rel: str) -> str:
        return f"{self.root.rstrip('/')}/{rel}"


class LocalSink(Sink):
    """Writes to data/landing_local — for testing without a workspace."""

    def __init__(self, root: Path = LOCAL_LANDING) -> None:
        self.root = str(root)

    def put(self, rel: str, data: bytes) -> str:
        p = Path(self.root) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(p)  # atomic: readers never see a partial file
        return str(p)


class VolumeSink(Sink):
    """Uploads to a Unity Catalog volume through the Databricks Files API (atomic per file)."""

    def __init__(self, root: str = VOLUME, profile: str = PROFILE) -> None:
        from databricks.sdk import WorkspaceClient
        self.root = root
        self.w = WorkspaceClient(profile=profile)

    def put(self, rel: str, data: bytes) -> str:
        path = self.full(rel)
        self.w.files.upload(path, io.BytesIO(data), overwrite=True)
        return path


def make_sink(kind: str) -> Sink:
    return VolumeSink() if kind == "volume" else LocalSink()


# --------------------------------------------------------------------------- batches
@dataclass
class Batch:
    source: str
    entity: str
    batch_id: str = ""
    files: list[dict] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.batch_id = self.batch_id or new_batch_id(self.source, self.entity)

    def add(self, sink: Sink, rel: str, data: bytes, records: int) -> None:
        path = sink.put(rel, data)
        self.files.append(dict(path=path, records=records,
                               bytes=len(data), sha256=hashlib.sha256(data).hexdigest()))

    def add_many(self, sink: Sink, items: list[tuple[str, bytes, int]], workers: int = UPLOAD_WORKERS) -> None:
        """Upload many files concurrently. Every upload must succeed before close() writes the manifest, so the
        batch stays all-or-nothing from the reader's point of view; files are listed in input order."""
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max(1, workers)) as ex:
            paths = list(ex.map(lambda it: sink.put(it[0], it[1]), items))  # raises if any upload fails
        for (_rel, data, records), path in zip(items, paths, strict=True):
            self.files.append(dict(path=path, records=records, bytes=len(data),
                                   sha256=hashlib.sha256(data).hexdigest()))

    def close(self, sink: Sink, state: State) -> dict | None:
        """Write the manifest last. Returns the manifest (None if the batch is empty)."""
        if not self.files:
            return None
        m = dict(batch_id=self.batch_id, source=self.source, entity=self.entity, created_at=utcnow().isoformat(),
                 files=self.files, **self.extra)
        sink.put(f"_manifests/{self.source}/{self.batch_id}.json", json.dumps(m, indent=1).encode())
        state.log_batch(m)
        return m


def jsonl_gz(rows: list) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        for r in rows:
            gz.write((json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n").encode())
    return buf.getvalue()
