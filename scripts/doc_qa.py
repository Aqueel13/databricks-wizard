"""Document question answering with citations over the supplier-document index (reused by the M7 MCP tools).

search()  exact lookup when the question names a document key, then hybrid (keyword + vector) retrieval
answer()  answers ONLY from the retrieved chunks and cites them as [file, page]
"""
from __future__ import annotations

import json
import os
import re

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.serving import ChatMessage, ChatMessageRole

INDEX = "wizard.silver.doc_chunks_index"
MODEL = "databricks-llama-4-maverick"
COLUMNS = ["chunk_id", "file_name", "doc_type", "owner_id", "page", "content"]
SYSTEM = """You answer questions about supplier documents (certificates and contracts) for a procurement team.
Use ONLY the sources provided. Cite every fact with its source as [file_name, p. N].
If the sources do not contain the answer, say "Not found in the documents." Never guess.
Answer in one or two sentences; give dates as YYYY-MM-DD."""

_w: WorkspaceClient | None = None


def client() -> WorkspaceClient:
    global _w
    if _w is None:
        prof = os.environ.get("DATABRICKS_CONFIG_PROFILE")
        _w = WorkspaceClient(profile=prof) if prof else WorkspaceClient()
    return _w


DOC_KEY = re.compile(r"\b(?:CTR|ISO|FSSC|BRCG)-\d{3,6}\b", re.IGNORECASE)


def _query(query: str, k: int, filters: dict | None) -> list[dict]:
    kw = {"filters_json": json.dumps(filters)} if filters else {}
    r = client().vector_search_indexes.query_index(index_name=INDEX, columns=COLUMNS, query_text=query,
                                                   num_results=k, query_type="HYBRID", **kw)
    cols = [c.name for c in r.manifest.columns]
    return [dict(zip(cols, row, strict=False)) for row in (r.result.data_array or [])]


def search(query: str, k: int = 5, doc_type: str | None = None) -> list[dict]:
    """Identifier first, then semantics: when the question names a document key (CTR-00247, ISO-397951, ...),
    that document is looked up exactly; embeddings treat CTR-00247 and CTR-00248 as near-identical."""
    base = {"doc_type": doc_type} if doc_type else {}
    keys = sorted({m.upper() for m in DOC_KEY.findall(query)})
    hits = _query(query, k, {**base, "file_name": [f"{x}.pdf" for x in keys]}) if keys else []
    if len(hits) < k:  # fill up with hybrid (keyword + vector) results
        seen = {h["chunk_id"] for h in hits}
        hits += [h for h in _query(query, k, base or None) if h["chunk_id"] not in seen][: k - len(hits)]
    return hits


def answer(question: str, k: int = 5) -> dict:
    hits = search(question, k)
    sources = "\n\n".join(f"[{h['file_name']}, p. {int(h['page'])}]\n{h['content']}" for h in hits)
    resp = client().serving_endpoints.query(
        name=MODEL, max_tokens=300, temperature=0.0,
        messages=[ChatMessage(role=ChatMessageRole.SYSTEM, content=SYSTEM),
                  ChatMessage(role=ChatMessageRole.USER, content=f"Sources:\n{sources}\n\nQuestion: {question}")])
    text = resp.choices[0].message.content
    if isinstance(text, list):
        text = " ".join(x.get("text", "") for x in text if isinstance(x, dict) and x.get("type") == "text")
    cited = sorted(set(re.findall(r"\[([^\],]+\.pdf)", text or "")))
    return {"answer": text, "citations": cited, "retrieved": [h["file_name"] for h in hits]}
