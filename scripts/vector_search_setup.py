"""Create the Vector Search endpoint and the document-chunk index (idempotent).

Free Edition allows one endpoint (one unit), so everything shares `wizard-vs`. The index is a Delta Sync index on
silver.doc_chunks with embeddings computed by the endpoint from `chunk_text` (qwen3-embedding, ADR 0005).

  uv run --project shipper python scripts/vector_search_setup.py
"""
from __future__ import annotations

import os
import time

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.vectorsearch import (
    DeltaSyncVectorIndexSpecRequest,
    EmbeddingSourceColumn,
    EndpointType,
    PipelineType,
    VectorIndexType,
)

ENDPOINT = "wizard-vs"
INDEX = "wizard.silver.doc_chunks_index"
SOURCE = "wizard.silver.doc_chunks"
EMBEDDING_MODEL = "databricks-qwen3-embedding-0-6b"
COLUMNS = ["chunk_id", "doc_id", "file_name", "doc_type", "owner_id", "page", "element_type", "content", "chunk_text"]

w = WorkspaceClient(profile=os.environ.get("DATABRICKS_CONFIG_PROFILE", "wizard"))


def endpoint_state() -> str | None:
    for e in w.vector_search_endpoints.list_endpoints():
        if e.name == ENDPOINT:
            return e.endpoint_status.state.value if e.endpoint_status and e.endpoint_status.state else "UNKNOWN"
    return None


def main() -> None:
    if endpoint_state() is None:
        print(f"creating endpoint {ENDPOINT}")
        w.vector_search_endpoints.create_endpoint(name=ENDPOINT, endpoint_type=EndpointType.STANDARD)
    t = time.time()
    while (s := endpoint_state()) != "ONLINE":
        print(f"  endpoint {s} ({time.time() - t:.0f}s)", flush=True)
        time.sleep(20)
    print("endpoint ONLINE")
    existing = [i.name for i in w.vector_search_indexes.list_indexes(endpoint_name=ENDPOINT)]
    if INDEX not in existing:
        print(f"creating index {INDEX}")
        w.vector_search_indexes.create_index(
            name=INDEX, endpoint_name=ENDPOINT, primary_key="chunk_id", index_type=VectorIndexType.DELTA_SYNC,
            delta_sync_index_spec=DeltaSyncVectorIndexSpecRequest(
                source_table=SOURCE, pipeline_type=PipelineType.TRIGGERED, columns_to_sync=COLUMNS,
                embedding_source_columns=[EmbeddingSourceColumn(name="chunk_text",
                                                                embedding_model_endpoint_name=EMBEDDING_MODEL)]))
    t = time.time()
    while True:
        idx = w.vector_search_indexes.get_index(INDEX)
        st = idx.status
        print(f"  index ready={st.ready} rows={st.indexed_row_count} {st.message[:120] if st.message else ''} "
              f"({time.time() - t:.0f}s)", flush=True)
        if st.ready and st.indexed_row_count:
            break
        time.sleep(30)
    print("index ready")


if __name__ == "__main__":
    main()
