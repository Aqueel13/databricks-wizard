"""Test scenarios that exercise silver's handling of delivery problems.

out-of-order: Debezium events for one test vendor (LFA1 key ZZOOO00001) are shipped in the wrong order,
in separate batches, so each lands in a different pipeline update:
  phase 1  UPDATE (v2)          arrives first, before the row was ever inserted
  phase 2  INSERT (v1)          arrives late, with an OLDER sequence (LSN) than the update
  phase 3  DELETE               closes the record
Expected silver state after each phase is asserted by scripts/m3_out_of_order_test.sh.

Events use LSNs far above the real ones and Kafka partition 99, so they never collide with real CDC.
"""
from __future__ import annotations

import time

from .core import Batch, Sink, State, jsonl_gz, utcnow

LIFNR = "ZZOOO00001"
BASE_LSN = 9_000_000_000_000_000
V1 = dict(lifnr=LIFNR, name1="OUT-OF-ORDER TEST VENDOR", name2=None, land1="NL", ort01="Rotterdam", pstlz="3011",
          regio=None, stras="Testweg 1", stcd1=None, stcd2=None, stceg=None, telf1=None, ktokk="KRED", loevm=None,
          sperr=None, spras="E", erdat=20760, ernam="SCENARIO")
V2 = dict(V1, name1="OUT-OF-ORDER TEST VENDOR B.V.", telf1="+31 10 0000000", stras="Testweg 2")


def _event(op: str, before, after, k: int) -> dict:
    now_ms = int(time.time() * 1000)
    return {"kafka": {"topic": "erp.sap.lfa1", "partition": 99, "offset": 10**12 + k, "timestamp_ms": now_ms,
                      "key": {"lifnr": LIFNR}},
            "event": {"before": before, "after": after, "op": op, "ts_ms": now_ms,
                      "source": {"connector": "scenario", "db": "erp", "schema": "sap", "table": "lfa1",
                                 "lsn": BASE_LSN + k}}}


PHASES = {
    1: ("update arrives first", _event("u", V1, V2, 2)),
    2: ("insert arrives late (older LSN)", _event("c", None, V1, 1)),
    3: ("delete", _event("d", V2, None, 3)),
}


def ship_phase(sink: Sink, state: State, phase: int) -> dict:
    label, ev = PHASES[phase]
    b = Batch("sap", "scenario")
    b.add(sink, f"sap/cdc/lfa1/{utcnow():%Y-%m-%d}/{b.batch_id}-001.jsonl.gz", jsonl_gz([ev]), 1)
    b.extra = {"scenario": "out-of-order", "phase": phase, "label": label}
    return b.close(sink, state)
