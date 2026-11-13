"""wizard-sources: build and run the simulated source systems.

  universe   build supplier universe + ground truth          -> data/stage
  gt-refine  apply answer-key corrections to an existing build (no data changes)
  sap        load SAP tables (+ regional POs, drift) into Postgres `sap`
  drift-patch  re-apply delivery drift (current settings) to the loaded SAP mock, via UPDATEs (flows through CDC)
  coupa      generate indirect spend and load Postgres `coupa`
  hicx       write HICX export files                           -> data/exports/hicx
  docs       generate supplier documents (certificates, contracts) -> data/exports/docs
  seed       all of the above, in order
  simulate   keep making live changes in SAP and Coupa (CDC / incremental feed)
  api        run the Coupa mock API
"""
from __future__ import annotations

import argparse


def main() -> None:
    p = argparse.ArgumentParser(prog="wizard-sources", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    for c in ("universe", "sap", "coupa", "hicx", "docs", "seed", "gt-refine", "drift-patch"):
        sub.add_parser(c)
    s = sub.add_parser("simulate")
    s.add_argument("--interval", type=float, default=5.0, help="seconds between change batches")
    s.add_argument("--batches", type=int, default=0, help="stop after N batches (0 = run forever)")
    a = sub.add_parser("api")
    a.add_argument("--port", type=int, default=8081)
    args = p.parse_args()

    if args.cmd in ("universe", "seed"):
        from .universe import build, refine_sap_identity
        build()
        refine_sap_identity()
    if args.cmd == "gt-refine":
        from .universe import refine_sap_identity
        refine_sap_identity()
    if args.cmd == "drift-patch":
        from .sap_load import drift_patch
        drift_patch()
    if args.cmd in ("sap", "seed"):
        from .sap_load import load
        load()
    if args.cmd in ("coupa", "seed"):
        from .coupa_gen import load
        load()
    if args.cmd in ("hicx", "seed"):
        from .hicx_export import export
        export()
    if args.cmd in ("docs", "seed"):
        from .docs_gen import generate
        generate()
    if args.cmd == "simulate":
        from .simulate import run
        run(args.interval, args.batches)
    if args.cmd == "api":
        import uvicorn
        uvicorn.run("wizard_sources.coupa_api:app", host="0.0.0.0", port=args.port)


if __name__ == "__main__":
    main()
