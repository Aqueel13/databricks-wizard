"""wizard-shipper: move local source data into the Databricks landing volume.

  run            ship all sources once, or keep running with --interval
  ground-truth   upload the generator's answer key (data/stage/*.parquet) to the ops volume
  status         recent batches from the local state store

Auth: uses the Databricks CLI profile in DATABRICKS_CONFIG_PROFILE (default `wizard`).
Use --sink local to write into data/landing_local instead (no workspace needed).
"""
from __future__ import annotations

import argparse
import time

from . import sources as S
from .core import GT_VOLUME, ROOT, State, VolumeSink, make_sink

ALL = ["sap", "coupa", "hicx", "docs", "public"]


def run_once(sink, state, only: list[str]) -> None:
    for name in only:
        t = time.time()
        res = getattr(S, f"ship_{name}")(sink, state)
        ms = res if isinstance(res, list) else ([res] if res else [])
        files = sum(len(m["files"]) for m in ms)
        recs = sum(f["records"] for m in ms for f in m["files"])
        print(f"{name:7s} batches={len(ms)} files={files} records={recs} ({time.time() - t:.1f}s)", flush=True)


def main() -> None:
    p = argparse.ArgumentParser(prog="wizard-shipper", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--sink", choices=["volume", "local"], default="volume")
    r.add_argument("--only", default=",".join(ALL), help=f"comma list of {ALL}")
    r.add_argument("--interval", type=float, default=0, help="seconds between cycles; 0 = run once")
    g = sub.add_parser("ground-truth")
    g.add_argument("--sink", choices=["volume", "local"], default="volume")
    sc = sub.add_parser("scenario", help="ship a test scenario phase (see scenario.py)")
    sc.add_argument("name", choices=["out-of-order"])
    sc.add_argument("--phase", type=int, required=True, choices=[1, 2, 3])
    sc.add_argument("--sink", choices=["volume", "local"], default="volume")
    st = sub.add_parser("status")
    st.add_argument("--sink", choices=["volume", "local"], default="volume")
    a = p.parse_args()

    if a.cmd == "run":
        sink, state = make_sink(a.sink), State(a.sink)
        only = [x.strip() for x in a.only.split(",") if x.strip()]
        while True:
            run_once(sink, state, only)
            if not a.interval:
                break
            time.sleep(a.interval)
    elif a.cmd == "ground-truth":
        sink = VolumeSink(root=GT_VOLUME) if a.sink == "volume" else make_sink("local")
        for f in sorted((ROOT / "data" / "stage").glob("*.parquet")):
            print("uploaded", sink.put(f"{f.name}" if a.sink == "volume" else f"_ground_truth/{f.name}", f.read_bytes()))
    elif a.cmd == "scenario":
        from .scenario import ship_phase
        m = ship_phase(make_sink(a.sink), State(a.sink), a.phase)
        print(f"shipped phase {a.phase}: {m['label']} -> {m['files'][0]['path']}")
    elif a.cmd == "status":
        for row in State(a.sink).db.execute("select created_at, source, entity, files, records, bytes from batch "
                                      "order by created_at desc limit 20"):
            print(*row)


if __name__ == "__main__":
    main()
