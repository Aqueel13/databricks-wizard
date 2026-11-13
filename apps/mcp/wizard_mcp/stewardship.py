"""wizard-stewardship: review queue and proposals. Agents may propose; only a person approves (steward app)."""

import uuid

from mcp.server.fastmcp import Context, FastMCP

from .db import CAT, audited, caller, execute, query

INSTRUCTIONS = """Master-data stewardship for Larkspur Foods suppliers. list_review_items shows supplier-record pairs the
matcher could not settle and open sanctions screening alerts. You may propose a decision with your rationale; you cannot
approve it. A person reviews every proposal in the steward app, and only approved decisions change the data."""

mcp = FastMCP("wizard-stewardship", instructions=INSTRUCTIONS, stateless_http=True)

DECISIONS = {"match": {"match", "no_match"}, "sanctions": {"confirm", "dismiss"}}


@mcp.tool()
@audited("stewardship")
def list_review_items(kind: str = "match", limit: int = 20, ctx: Context | None = None) -> dict:
    """Items waiting for a steward. kind='match': supplier-record pairs the matcher could not settle (item_key =
    pair key); kind='sanctions': open screening alerts (item_key = supplier_id|ent_num). Excludes items already
    decided or with a pending proposal."""
    lim = max(1, min(int(limit), 100))
    if kind == "match":
        rows = query(f"""
            SELECT q.pair_key AS item_key, q.reason, round(q.match_probability, 3) AS match_probability,
                   l.name AS left_name, l.name_local AS left_local, l.country AS left_country, l.national_id AS left_id,
                   r.name AS right_name, r.name_local AS right_local, r.country AS right_country, r.national_id AS right_id
            FROM {CAT}.silver.er_review_queue q
            JOIN {CAT}.silver.supplier_records l ON concat(l.source, ':', l.source_id) = q.unique_id_l
            JOIN {CAT}.silver.supplier_records r ON concat(r.source, ':', r.source_id) = q.unique_id_r
            WHERE q.pair_key NOT IN (SELECT pair_key FROM {CAT}.silver.er_steward_decisions)
              AND q.pair_key NOT IN (SELECT item_key FROM {CAT}.ops.agent_proposals WHERE status = 'pending')
            ORDER BY q.match_probability DESC LIMIT {lim}""")
    elif kind == "sanctions":
        rows = query(f"""
            SELECT concat(h.supplier_id, '|', h.ent_num) AS item_key, d.legal_name, d.legal_name_local, d.country,
                   h.sanctioned_name, h.program, h.sdn_type, round(h.score, 3) AS score
            FROM {CAT}.gold.supplier_sanctions_hits h JOIN {CAT}.gold.dim_supplier d USING (supplier_id)
            WHERE h.status = 'open'
              AND concat(h.supplier_id, '|', h.ent_num) NOT IN
                  (SELECT item_key FROM {CAT}.ops.agent_proposals WHERE status = 'pending')
            ORDER BY h.score DESC LIMIT {lim}""")
    else:
        raise ValueError("kind must be 'match' or 'sanctions'")
    return {"result": rows}


@mcp.tool()
@audited("stewardship")
def propose_decision(kind: str, item_key: str, decision: str, rationale: str, ctx: Context | None = None) -> dict:
    """Propose a decision for a steward to review. kind='match' with decision 'match' / 'no_match', or
    kind='sanctions' with 'confirm' / 'dismiss'. The rationale must state the evidence. Nothing changes until a
    person approves the proposal in the steward app."""
    if decision not in DECISIONS.get(kind, set()):
        raise ValueError(f"kind={kind!r} allows decisions {sorted(DECISIONS.get(kind, []))}")
    if len(rationale.strip()) < 20:
        raise ValueError("give a rationale that states the evidence (at least 20 characters)")
    pid = uuid.uuid4().hex[:12]
    execute(f"""INSERT INTO {CAT}.ops.agent_proposals (proposal_id, kind, item_key, decision, rationale, proposed_by,
                status, created_at) VALUES (:id, :kind, :item, :decision, :rationale, :by, 'pending', current_timestamp())""",
            {"id": pid, "kind": kind, "item": item_key, "decision": decision, "rationale": rationale,
             "by": caller(ctx)})
    return {"result": {"proposal_id": pid, "status": "pending"},
            "note": "A steward must approve this proposal in the steward app before anything changes."}


@mcp.tool()
@audited("stewardship")
def list_proposals(status: str | None = None, limit: int = 20, ctx: Context | None = None) -> dict:
    """Proposals and their review status (pending, approved, rejected), newest first."""
    cond = "WHERE status = :st" if status else ""
    rows = query(f"""SELECT proposal_id, kind, item_key, decision, rationale, proposed_by, status, created_at,
                            reviewed_by, reviewed_at, review_note
                     FROM {CAT}.ops.agent_proposals {cond} ORDER BY created_at DESC LIMIT {max(1, min(int(limit), 100))}""",
                 {"st": status} if status else None)
    return {"result": rows}
