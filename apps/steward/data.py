"""Data access for the steward app (SQL warehouse via databricks-sql-connector). Testable outside the app."""
from __future__ import annotations

import os

import pandas as pd
from databricks import sql
from databricks.sdk.core import Config

CAT = os.environ.get("WIZARD_CATALOG", "wizard")
_cfg = Config()


def _conn():
    wh = os.environ["DATABRICKS_WAREHOUSE_ID"]
    return sql.connect(server_hostname=_cfg.host, http_path=f"/sql/1.0/warehouses/{wh}",
                       credentials_provider=lambda: _cfg.authenticate)


def query(q: str, params: dict | None = None) -> pd.DataFrame:
    with _conn() as c, c.cursor() as cur:
        cur.execute(q, params or {})
        return cur.fetchall_arrow().to_pandas()


def execute(q: str, params: dict | None = None) -> None:
    with _conn() as c, c.cursor() as cur:
        cur.execute(q, params or {})


def review_queue() -> pd.DataFrame:
    """Open review items not yet decided, with both records and the evidence behind the pair."""
    return query(f"""
        WITH q AS (
          SELECT pair_key, unique_id_l, unique_id_r, match_probability, reason
          FROM {CAT}.silver.er_review_queue
          WHERE pair_key NOT IN (SELECT pair_key FROM {CAT}.silver.er_steward_decisions)
        )
        SELECT q.*, p.method, p.llm_reason,
               l.name AS l_name, l.name_local AS l_local, l.country AS l_country, l.city AS l_city,
               l.national_id AS l_id, l.national_id_type AS l_id_type,
               r.name AS r_name, r.name_local AS r_local, r.country AS r_country, r.city AS r_city,
               r.national_id AS r_id, r.national_id_type AS r_id_type
        FROM q
        LEFT JOIN {CAT}.silver.er_scored_pairs p USING (pair_key)
        JOIN {CAT}.silver.supplier_records l ON concat(l.source, ':', l.source_id) = q.unique_id_l
        JOIN {CAT}.silver.supplier_records r ON concat(r.source, ':', r.source_id) = q.unique_id_r
        ORDER BY q.match_probability DESC""")


def sanctions_alerts() -> pd.DataFrame:
    """Open screening alerts with the supplier's golden record and the sanctioned entry."""
    return query(f"""
        SELECT h.supplier_id, h.ent_num, h.screened_name, h.sanctioned_name, h.name_type, h.program, h.sdn_type,
               h.score, d.legal_name, d.legal_name_local, d.country, d.city, d.national_id_type, d.national_id,
               d.source_systems
        FROM {CAT}.gold.supplier_sanctions_hits h
        JOIN {CAT}.gold.dim_supplier d USING (supplier_id)
        WHERE h.status = 'open'
          AND NOT EXISTS (SELECT 1 FROM {CAT}.silver.sanctions_alert_decisions x
                          WHERE x.supplier_id = h.supplier_id AND x.ent_num = h.ent_num)
        ORDER BY h.score DESC, d.legal_name""")


def decide_pair(pair_key: str, decision: str, steward: str, note: str) -> None:
    assert decision in ("match", "no_match")
    execute(f"""INSERT INTO {CAT}.silver.er_steward_decisions (pair_key, decision, steward, note, decided_at)
                VALUES (:pair_key, :decision, :steward, :note, current_timestamp())""",
            {"pair_key": pair_key, "decision": decision, "steward": steward, "note": note})


def decide_alert(row: dict, decision: str, steward: str, note: str) -> None:
    assert decision in ("confirm", "dismiss")
    execute(f"""INSERT INTO {CAT}.silver.sanctions_alert_decisions
                (supplier_id, ent_num, decision, steward, note, screened_name, sanctioned_name, decided_at)
                VALUES (:supplier_id, :ent_num, :decision, :steward, :note, :screened, :sanctioned, current_timestamp())""",
            {"supplier_id": row["supplier_id"], "ent_num": int(row["ent_num"]), "decision": decision,
             "steward": steward, "note": note, "screened": row["screened_name"], "sanctioned": row["sanctioned_name"]})


def decision_log(limit: int = 50) -> pd.DataFrame:
    return query(f"""
        SELECT 'match review' AS kind, pair_key AS item, decision, steward, note, decided_at
        FROM {CAT}.silver.er_steward_decisions
        UNION ALL
        SELECT 'sanctions alert', concat(screened_name, ' vs ', sanctioned_name), decision, steward, note, decided_at
        FROM {CAT}.silver.sanctions_alert_decisions
        ORDER BY decided_at DESC LIMIT {int(limit)}""")


def apply_decisions() -> int:
    """Re-run entity resolution, gold and the semantic layer so decisions take effect. Returns the run id."""
    from databricks.sdk import WorkspaceClient
    w = WorkspaceClient()
    r = w.api_client.do("POST", "/api/2.2/jobs/run-now", body={
        "job_id": int(os.environ["WIZARD_JOB_ID"]), "only": ["entity_resolution", "gold", "semantic_layer"]})
    return r["run_id"]
