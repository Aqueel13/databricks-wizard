"""LLM adjudication of uncertain pairs (ADR 0004, layer 3).

Each pair in the uncertain probability band goes to a foundation model with both records and a fixed rubric.
The model returns JSON: {"verdict": "match" | "no_match" | "unsure", "reason": "..."}.
Verdicts are cached by (pair, model, prompt version), so re-runs only pay for new pairs.
"""
from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

MODEL = os.environ.get("WIZARD_ER_MODEL", "databricks-qwen3-next-80b-a3b-instruct")  # chosen by benchmark (ADR 0005)
PROMPT_VERSION = "er-adjudicate-v1"

SYSTEM = """You are a master-data steward deciding whether two supplier records from different business systems
refer to the same legal entity. Records may come from SAP (local buyers), Coupa (group procurement, Latin script)
or HICX (verified supplier master). Rules:
- Names may be transliterated (Cyrillic <-> Latin, several standards), translated, abbreviated, re-ordered,
  upper-cased, truncated at 35 characters, or contain typos. These alone do NOT make records different.
- Legal forms may be translated (TOV = TzOV = LLC = Sp. z o.o. class; PrAT/PJSC/JSC = joint-stock). A different
  legal-form TYPE or a different jurisdiction (GmbH vs B.V.) means different entities.
- Different national IDs (NIP, EDRPOU, EIN) mean different entities unless one is clearly a typo of the other.
- Numbers in names usually identify a specific entity (Plant 1500 vs Plant 1600 are different).
- Generic names (e.g. "National Research University", a dairy cooperative) with no other corroboration: unsure.
Answer with JSON only: {"verdict": "match" | "no_match" | "unsure", "reason": "<one sentence>"}"""


def _record(r: dict) -> str:
    fields = [("name", r.get("name")), ("name in local script", r.get("name_local")), ("country", r.get("country")),
              ("city", r.get("city")), ("national id", r.get("national_id")), ("system", r.get("source"))]
    return "\n".join(f"  {k}: {v}" for k, v in fields if v not in (None, "", float("nan")) and not pd.isna(v))


def _ask(w, a: dict, b: dict, model: str = MODEL) -> dict:
    from databricks.sdk.service.serving import ChatMessage, ChatMessageRole
    user = f"Record A:\n{_record(a)}\n\nRecord B:\n{_record(b)}\n\nSame legal entity?"
    try:
        resp = w.serving_endpoints.query(
            name=model, max_tokens=300, temperature=0.0,
            messages=[ChatMessage(role=ChatMessageRole.SYSTEM, content=SYSTEM),
                      ChatMessage(role=ChatMessageRole.USER, content=user)])
        text = resp.choices[0].message.content
        if isinstance(text, list):  # reasoning models return blocks; only "text" blocks hold the answer
            text = " ".join(x.get("text", "") for x in text if isinstance(x, dict) and x.get("type") == "text")
        m = re.search(r"\{.*\}", text or "", re.S)
        out = json.loads(m.group(0)) if m else {}
        verdict = out.get("verdict") if out.get("verdict") in ("match", "no_match", "unsure") else "unsure"
        return {"verdict": verdict, "reason": str(out.get("reason", ""))[:400], "raw": (text or "")[:1000]}
    except Exception as e:  # an unanswered pair is a review item, never a silent link
        return {"verdict": "unsure", "reason": f"model error: {type(e).__name__}", "raw": str(e)[:1000]}


def adjudicate(band: pd.DataFrame, df: pd.DataFrame, cache: Path | None = None, workers: int = 8,
               client=None) -> pd.DataFrame:
    """band: pairs needing a verdict (pair_key, unique_id_l, unique_id_r). Returns pair_key, verdict, reason, ..."""
    cached = pd.read_parquet(cache) if cache and Path(cache).exists() else pd.DataFrame(
        columns=["pair_key", "model", "prompt_version", "verdict", "reason", "raw"])
    cached = cached[(cached["model"] == MODEL) & (cached["prompt_version"] == PROMPT_VERSION)]
    todo = band[~band["pair_key"].isin(set(cached["pair_key"]))]
    if len(todo):
        if client is None:
            from databricks.sdk import WorkspaceClient
            client = WorkspaceClient(profile=os.environ.get("DATABRICKS_CONFIG_PROFILE", "wizard"))
        recs = df.set_index("unique_id").to_dict("index")
        for u, r in recs.items():
            r["source"] = u.split(":", 1)[0]
        with ThreadPoolExecutor(workers) as ex:
            res = list(ex.map(lambda p: _ask(client, recs[p[1]], recs[p[2]]),
                              zip(todo["pair_key"], todo["unique_id_l"], todo["unique_id_r"])))
        new = pd.DataFrame(res).assign(pair_key=todo["pair_key"].to_numpy(), model=MODEL, prompt_version=PROMPT_VERSION)
        cached = pd.concat([cached, new], ignore_index=True)
        if cache:
            cached.to_parquet(cache, index=False)
    print(f"llm: {len(band)} pairs, {len(todo)} new calls ({MODEL}); verdicts {cached['verdict'].value_counts().to_dict()}")
    return cached[cached["pair_key"].isin(set(band["pair_key"]))]
