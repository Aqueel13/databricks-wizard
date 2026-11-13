import gzip
import json

import pytest
from wizard_shipper import core
from wizard_shipper.core import Batch, LocalSink, State, jsonl_gz


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "STATE_DIR", tmp_path)
    return tmp_path


def test_jsonl_gz_roundtrip():
    rows = [{"a": 1, "name": "ТОВ «Ромашка»"}, {"a": 2}]
    lines = gzip.decompress(jsonl_gz(rows)).decode().splitlines()
    assert [json.loads(x) for x in lines] == rows


def test_manifest_written_last_and_lists_every_file(sandbox):
    sink, state = LocalSink(sandbox / "landing"), State("local")
    order = []
    real_put = sink.put
    sink.put = lambda rel, data: order.append(rel) or real_put(rel, data)
    b = Batch("sap", "cdc")
    b.add(sink, "sap/cdc/ekko/x-001.jsonl.gz", jsonl_gz([{"k": 1}]), 1)
    b.add(sink, "sap/cdc/ekpo/x-001.jsonl.gz", jsonl_gz([{"k": 1}, {"k": 2}]), 2)
    m = b.close(sink, state)
    assert order[-1].startswith("_manifests/sap/") and len(order) == 3
    assert [f["records"] for f in m["files"]] == [1, 2]
    assert json.loads((sandbox / "landing" / order[-1]).read_text())["batch_id"] == b.batch_id


def test_empty_batch_writes_no_manifest(sandbox):
    assert Batch("coupa", "api").close(LocalSink(sandbox / "landing"), State("local")) is None


def test_state_is_separate_per_sink(sandbox):
    State("local").set("coupa", "invoices", "2026-01-01T00:00:00Z")
    assert State("volume").get("coupa", "invoices") is None
    assert State("local").get("coupa", "invoices") == "2026-01-01T00:00:00Z"


def test_add_many_lists_files_in_order_and_fails_whole_batch(sandbox):
    sink = LocalSink(sandbox / "landing")
    b = Batch("docs", "pdf")
    b.add_many(sink, [(f"docs/{i}.pdf", bytes([i]), 1) for i in range(20)], workers=8)
    assert [f["path"].rsplit("/", 1)[-1] for f in b.files] == [f"{i}.pdf" for i in range(20)]

    def boom(rel, data):
        raise OSError("upload failed")
    bad = Batch("docs", "pdf")
    sink.put = boom
    with pytest.raises(OSError):
        bad.add_many(sink, [("docs/x.pdf", b"x", 1)])
    assert bad.files == []  # nothing recorded, so close() writes no manifest
