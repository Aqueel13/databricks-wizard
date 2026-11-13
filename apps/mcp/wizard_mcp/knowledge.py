"""wizard-knowledge: supplier certificates and contracts; cited search and answers."""

import json
import re

from databricks.sdk.service.serving import ChatMessage, ChatMessageRole
from mcp.server.fastmcp import Context, FastMCP

from .db import CAT, audited, query, ws

INSTRUCTIONS = """Supplier documents (management-system certificates and contracts) for Larkspur Foods, parsed from
PDFs (including scanned ones) and indexed for search. Search, then cite passages as [file_name, p. N]. A document's
own fields (e.g. a certificate's expiry date) outrank the system of record when they disagree; get_document shows both.
answer_document_question answers only from retrieved passages and says when the documents don't contain the answer."""

mcp = FastMCP("wizard-knowledge", instructions=INSTRUCTIONS, stateless_http=True)

INDEX = f"{CAT}.silver.doc_chunks_index"
MODEL = "databricks-llama-4-maverick"
COLUMNS = ["chunk_id", "file_name", "doc_type", "owner_id", "page", "content"]
DOC_KEY = re.compile(r"\b(?:CTR|ISO|FSSC|BRCG)-\d{3,6}\b", re.IGNORECASE)
SYSTEM = """You answer questions about supplier documents (certificates and contracts) for a procurement team.
Use ONLY the sources provided. Cite every fact with its source as [file_name, p. N].
If the sources do not contain the answer, say "Not found in the documents." Never guess.
Answer in one or two sentences; give dates as YYYY-MM-DD."""


def _query(text: str, k: int, filters: dict | None) -> list[dict]:
    kw = {"filters_json": json.dumps(filters)} if filters else {}
    r = ws().vector_search_indexes.query_index(index_name=INDEX, columns=COLUMNS, query_text=text, num_results=k,
                                               query_type="HYBRID", **kw)
    cols = [c.name for c in r.manifest.columns]
    return [dict(zip(cols, row, strict=False)) for row in (r.result.data_array or [])]


def search(text: str, k: int = 5, doc_type: str | None = None, files: list[str] | None = None) -> list[dict]:
    """Identifier first, then semantics: a question naming a document key (CTR-00247, ISO-397951) looks that document
    up exactly, because embeddings treat CTR-00247 and CTR-00248 as near-identical; then hybrid search."""
    base: dict = {"doc_type": doc_type} if doc_type else {}
    if files:
        base["file_name"] = files
    keys = sorted({m.upper() for m in DOC_KEY.findall(text)})
    hits = _query(text, k, {**base, "file_name": [f"{x}.pdf" for x in keys]}) if keys else []
    if len(hits) < k:
        seen = {h["chunk_id"] for h in hits}
        hits += [h for h in _query(text, k, base or None) if h["chunk_id"] not in seen][: k - len(hits)]
    for h in hits:
        h["citation"] = f"[{h['file_name']}, p. {int(h['page'])}]"
    return hits


def answer(question: str, k: int = 5, files: list[str] | None = None) -> dict:
    hits = search(question, k, files=files)
    sources = "\n\n".join(f"{h['citation']}\n{h['content']}" for h in hits)
    resp = ws().serving_endpoints.query(
        name=MODEL, max_tokens=300, temperature=0.0,
        messages=[ChatMessage(role=ChatMessageRole.SYSTEM, content=SYSTEM),
                  ChatMessage(role=ChatMessageRole.USER, content=f"Sources:\n{sources}\n\nQuestion: {question}")])
    text = resp.choices[0].message.content
    if isinstance(text, list):
        text = " ".join(x.get("text", "") for x in text if isinstance(x, dict) and x.get("type") == "text")
    return {"answer": text, "citations": sorted(set(re.findall(r"\[([^\],]+\.pdf)", text or ""))),
            "retrieved": [h["file_name"] for h in hits], "passages": hits}


def _supplier_files(supplier_id: str | None) -> list[str] | None:
    if not supplier_id:
        return None
    rows = query(f"SELECT file_name FROM {CAT}.gold.supplier_documents WHERE supplier_id = :s", {"s": supplier_id})
    if not rows:
        raise ValueError(f"no documents for supplier {supplier_id}")
    return [r["file_name"] for r in rows]


@mcp.tool()
@audited("knowledge")
def search_documents(query_text: str, supplier_id: str | None = None, doc_type: str | None = None, k: int = 5,
                     ctx: Context | None = None) -> dict:
    """Find the document passages that best match a question, best first, each with a citation [file_name, p. N].
    Optional: restrict to one supplier's documents (supplier_id) or a type ('certificate' or 'contract')."""
    hits = search(query_text, max(1, min(int(k), 20)), doc_type, _supplier_files(supplier_id))
    return {"result": [{k_: h[k_] for k_ in ("citation", "file_name", "doc_type", "page", "content")} for h in hits]}


@mcp.tool()
@audited("knowledge")
def answer_document_question(question: str, supplier_id: str | None = None, ctx: Context | None = None) -> dict:
    """Answer a question from the documents only, with citations [file_name, p. N]; says "Not found in the
    documents." when the retrieved passages don't contain the answer."""
    r = answer(question, files=_supplier_files(supplier_id))
    return {"result": r["answer"], "citations": r["citations"], "retrieved": r["retrieved"]}


@mcp.tool()
@audited("knowledge")
def get_document(doc_key: str, ctx: Context | None = None) -> dict:
    """One document by key (e.g. CTR-00160, ISO-397951): fields extracted from the document, what the system of
    record says, whether they disagree, the supplier it belongs to, and the full parsed text."""
    p = {"k": doc_key.upper().removesuffix(".PDF")}
    rows = query(f"SELECT * FROM {CAT}.gold.supplier_documents WHERE upper(doc_key) = :k", p)
    if not rows:
        raise ValueError(f"no document {doc_key}")
    text = query(f"SELECT text FROM {CAT}.silver.doc_parsed WHERE doc_id = :d", {"d": rows[0]["doc_id"]})
    return {"result": rows[0], "text": text[0]["text"] if text else None,
            "citation": f"[{rows[0]['file_name']}, p. 1]"}
