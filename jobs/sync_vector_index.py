# Databricks Wizard: trigger a sync of the document vector index after silver has updated doc_chunks.
# (Delta Sync index in TRIGGERED mode; serverless Python task.)
import time

from databricks.sdk import WorkspaceClient

INDEX = "wizard.silver.doc_chunks_index"
w = WorkspaceClient()
before = w.vector_search_indexes.get_index(INDEX).status.indexed_row_count
w.vector_search_indexes.sync_index(INDEX)
for _ in range(60):  # wait up to ~10 minutes for the sync to finish
    time.sleep(10)
    st = w.vector_search_indexes.get_index(INDEX).status
    if st.ready and "SYNC" not in (st.message or "").upper().replace("SYNCED", ""):
        break
print(f"index {INDEX}: {before} -> {st.indexed_row_count} rows; {st.message}")
