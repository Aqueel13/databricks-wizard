"""Readers for each local source. Every reader is incremental and safe to re-run.

  sap     Kafka (Debezium) topics erp.sap.* -> offsets committed only after the manifest is written
  coupa   REST API, `updated-at[gt]` watermark per entity, small overlap window (dupes resolved in silver)
  hicx    export folders, shipped once per file content (sha256)
  docs    PDFs, shipped once per file content
  public  OFAC SDN + alternate names
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta

from .core import COUPA_KEY, COUPA_URL, KAFKA, ROOT, Batch, Sink, State, jsonl_gz, utcnow

EXPORTS = ROOT / "data" / "exports"


def _today() -> str:
    return f"{utcnow():%Y-%m-%d}"


# --------------------------------------------------------------------------- SAP CDC
def ship_sap(sink: Sink, state: State, max_per_file: int = 50_000, idle_s: float = 5.0) -> dict | None:
    from confluent_kafka import Consumer, KafkaError, TopicPartition

    c = Consumer({"bootstrap.servers": KAFKA, "group.id": f"wizard-shipper-{state.kind}", "enable.auto.commit": False,
                  "auto.offset.reset": "earliest", "enable.partition.eof": False})
    c.subscribe(["^erp\\.sap\\..*"])
    batch = Batch("sap", "cdc")
    buf: dict[str, list] = defaultdict(list)
    parts: dict[str, int] = defaultdict(int)
    last: dict[tuple[str, int], int] = {}
    first: dict[tuple[str, int], int] = {}
    started, last_msg = time.time(), time.time()

    def flush(topic: str) -> None:
        rows = buf.pop(topic, [])
        if rows:
            table = topic.split(".")[-1]
            parts[topic] += 1
            batch.add(sink, f"sap/cdc/{table}/{_today()}/{batch.batch_id}-{parts[topic]:03d}.jsonl.gz",
                      jsonl_gz(rows), len(rows))

    try:
        while True:
            msg = c.poll(1.0)
            if msg is None:
                waited_for_assignment = not c.assignment() and time.time() - started < 30
                if not waited_for_assignment and time.time() - last_msg > idle_s:
                    break
                continue
            if msg.error():
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    continue
                raise RuntimeError(msg.error())
            last_msg = time.time()
            tp = (msg.topic(), msg.partition())
            first.setdefault(tp, msg.offset())
            last[tp] = msg.offset()
            if msg.value() is None:  # tombstone after a delete; the 'd' event already carries the delete
                continue
            buf[msg.topic()].append({
                "kafka": {"topic": msg.topic(), "partition": msg.partition(), "offset": msg.offset(),
                          "timestamp_ms": msg.timestamp()[1],
                          "key": json.loads(msg.key()) if msg.key() else None},
                "event": json.loads(msg.value()),
            })
            if len(buf[msg.topic()]) >= max_per_file:
                flush(msg.topic())
        for t in list(buf):
            flush(t)
        batch.extra = {"offsets": {f"{t}[{p}]": [first[(t, p)], o] for (t, p), o in last.items()}}
        m = batch.close(sink, state)
        if last:  # commit only after the manifest exists -> at-least-once delivery
            c.commit(offsets=[TopicPartition(t, p, o + 1) for (t, p), o in last.items()], asynchronous=False)
        return m
    finally:
        c.close()


# --------------------------------------------------------------------------- Coupa
COUPA_ENTITIES = ["suppliers", "purchase_orders", "receipts", "invoices", "contracts"]


def ship_coupa(sink: Sink, state: State, overlap_s: int = 60) -> dict | None:
    import httpx

    batch = Batch("coupa", "api")
    new_wm, old_wm = {}, {}
    with httpx.Client(base_url=COUPA_URL, headers={"X-COUPA-API-KEY": COUPA_KEY}, timeout=60) as http:
        for e in COUPA_ENTITIES:
            wm = state.get("coupa", e)
            old_wm[e] = wm
            params: dict = {"limit": 50}
            if wm:
                since = datetime.fromisoformat(wm.rstrip("Z")) - timedelta(seconds=overlap_s)
                params["updated-at[gt]"] = since.isoformat() + "Z"
            rows, offset = [], 0
            while True:
                r = http.get(f"/api/{e}", params={**params, "offset": offset})
                r.raise_for_status()
                page = r.json()
                rows += page
                if len(page) < 50:
                    break
                offset += 50
            if not rows:
                continue
            top = max(x["updated-at"] for x in rows)
            if wm and top <= wm:  # only the overlap window came back; nothing new
                continue
            batch.add(sink, f"coupa/{e}/{_today()}/{batch.batch_id}.jsonl.gz", jsonl_gz(rows), len(rows))
            new_wm[e] = top
    batch.extra = {"watermark_from": old_wm, "watermark_to": new_wm, "overlap_seconds": overlap_s}
    m = batch.close(sink, state)
    for e, wm in new_wm.items():  # advance watermarks only after the manifest exists
        state.set("coupa", e, wm)
    return m


# --------------------------------------------------------------------------- files
def _sha(p) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _records(p) -> int:
    if p.suffix == ".csv":
        with open(p, encoding="utf-8", errors="replace") as fh:
            return max(sum(1 for _ in csv.reader(fh)) - 1, 0)
    if p.suffix == ".xlsx":
        from openpyxl import load_workbook
        ws = load_workbook(p, read_only=True).active
        return max((ws.max_row or 0) - 3, 0)  # two title rows + header
    return 1


def ship_hicx(sink: Sink, state: State) -> list[dict]:
    out = []
    for folder in sorted((EXPORTS / "hicx").glob("*")):
        batch = Batch("hicx", "export", extra={"export_date": folder.name})
        todo = []
        for f in sorted(folder.iterdir()):
            rel = f"hicx/{folder.name}/{f.name}"
            if state.shipped_sha(rel) != _sha(f):
                todo.append((rel, f.read_bytes(), _records(f)))
        batch.add_many(sink, todo)
        m = batch.close(sink, state)
        for fi in batch.files:
            state.mark_shipped(re.sub(r"^.*?(hicx/)", r"\1", fi["path"]), fi["sha256"])
        if m:
            out.append(m)
    return out


def ship_docs(sink: Sink, state: State) -> dict | None:
    batch = Batch("docs", "pdf")
    root = EXPORTS / "docs"
    todo = []
    for f in sorted(root.rglob("*.pdf")):
        rel = f"docs/{f.relative_to(root).as_posix()}"
        data = f.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        if state.shipped_sha(rel) != sha:
            todo.append((rel, data, 1, sha))
    batch.add_many(sink, [(rel, data, n) for rel, data, n, _ in todo])
    m = batch.close(sink, state)
    for rel, _, _, sha in todo:  # mark shipped only after the manifest exists
        state.mark_shipped(rel, sha)
    return m


def ship_public(sink: Sink, state: State) -> list[dict]:
    """Public reference snapshots: OFAC SDN + aliases + addresses, ECB euro FX reference rates. Shipped when they change."""
    out = []
    for entity, folder, names in (("ofac", "ofac", ("sdn.csv", "alt.csv", "add.csv")), ("ecb", "ecb", ("eurofxref-hist.csv",))):
        batch = Batch("public", entity)
        for name in names:
            f = ROOT / "data" / "raw" / folder / name
            if not f.exists():
                continue
            sha = _sha(f)
            if state.shipped_sha(f"{folder}/{name}") == sha:
                continue
            snap = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d")
            data = f.read_bytes()
            records = sum(1 for ln in data.decode("utf-8", "replace").splitlines() if re.match(r"^\d", ln))
            batch.add(sink, f"public/{folder}/{snap}/{name}", data, records)
            state.mark_shipped(f"{folder}/{name}", sha)
        if (m := batch.close(sink, state)):
            out.append(m)
    return out
