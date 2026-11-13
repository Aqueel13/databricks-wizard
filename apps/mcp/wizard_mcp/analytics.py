"""wizard-analytics: governed supplier metrics, supplier lookup, lineage and what-if (read-only)."""

import re
import time

import yaml
from mcp.server.fastmcp import Context, FastMCP

from .db import CAT, audited, query
from .normalize import normalize_name

INSTRUCTIONS = """Read-only supplier intelligence for Larkspur Foods (a fictional food manufacturer; company codes US01,
NL01, PL01, UA01), built on governed Databricks gold tables. Suppliers are resolved across SAP, Coupa and HICX into one
supplier_id: find it with search_suppliers, then use get_supplier_360. Prefer query_metric for numbers: its metrics are
governed definitions (Unity Catalog metric views) and the SQL used is returned. trace_metric shows where a number comes
from. simulate_consolidation is a what-if, not a forecast. run_exploratory_sql is a fallback for questions no tool
covers; say that its results are exploratory. A sanctions screening alert is a name similarity pending review, never a
finding. Money is USD at static FX rates."""

mcp = FastMCP("wizard-analytics", instructions=INSTRUCTIONS, stateless_http=True)

VIEWS = {"metrics_spend": "Order Month", "metrics_delivery": "Receipt Month", "metrics_supplier": None}
COUNT_MEASURE = {"metrics_spend": "PO Lines", "metrics_delivery": "Receipts", "metrics_supplier": "Suppliers"}
LOW_SAMPLE = 5
_registry: dict | None = None
_registry_at = 0.0


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def registry() -> dict:
    """Metric definitions read live from the metric views (cached 10 minutes)."""
    global _registry, _registry_at
    if _registry and time.time() - _registry_at < 600:
        return _registry
    reg = {}
    for view in VIEWS:
        ddl = query(f"SHOW CREATE TABLE {CAT}.gold.{view}")[0]["createtab_stmt"]
        spec = yaml.safe_load(ddl.split("$$")[1])
        comment = re.search(r"COMMENT '(.*?)'", ddl)
        dims = {slug(d["name"]): d["name"] for d in spec["dimensions"]}
        for m in spec["measures"]:
            reg[slug(m["name"])] = {"view": view, "measure": m["name"], "expr": m["expr"], "dimensions": dims,
                                    "view_description": comment.group(1) if comment else ""}
    _registry, _registry_at = reg, time.time()
    return reg


def _dim_values(view: str, label: str) -> list | None:
    if label in ("Supplier", "Supplier ID") or "Month" in label:
        return None
    rows = query(f"SELECT DISTINCT CAST(`{label}` AS STRING) v FROM {CAT}.gold.{view} ORDER BY 1 LIMIT 41")
    vals = [r["v"] for r in rows if r["v"] is not None]
    return vals if len(vals) <= 40 else None


@mcp.tool()
@audited("analytics")
def list_metrics(ctx: Context | None = None) -> dict:
    """List the governed metrics, each with the dimensions it can be filtered and grouped by (and their allowed
    values, where fixed). Definitions come from Unity Catalog metric views."""
    reg = registry()
    cache: dict = {}
    out = []
    for mid, m in reg.items():
        dims = {}
        for ds, label in m["dimensions"].items():
            key = (m["view"], label)
            if key not in cache:
                cache[key] = _dim_values(m["view"], label)
            dims[ds] = cache[key]
        out.append({"metric": mid, "label": m["measure"], "definition": m["expr"], "source": f"{CAT}.gold.{m['view']}",
                    "about": m["view_description"], "time_dimension": slug(VIEWS[m["view"]]) if VIEWS[m["view"]] else None,
                    "dimensions": dims})
    return {"result": out}


@mcp.tool()
@audited("analytics")
def query_metric(metric: str, filters: dict[str, list[str]] | None = None, group_by: list[str] | None = None,
                 period_from: str | None = None, period_to: str | None = None, ctx: Context | None = None) -> dict:
    """Compute a governed metric. filters: {dimension: [values]}; group_by: up to three dimensions;
    period_from/period_to: YYYY-MM-DD on the metric's time dimension (order month for spend, receipt month for
    delivery). Rows flagged low_sample rest on fewer than 5 records. The SQL used is returned for transparency."""
    reg = registry()
    if metric not in reg:
        raise ValueError(f"unknown metric {metric!r}; call list_metrics")
    m = reg[metric]
    dims, view = m["dimensions"], m["view"]

    def dim(name: str) -> str:
        d = dims.get(slug(name))
        if not d:
            raise ValueError(f"metric {metric} has no dimension {name!r}; allowed: {sorted(dims)}")
        return d

    gb = [dim(g) for g in (group_by or [])]
    if len(gb) > 3:
        raise ValueError("group_by allows at most three dimensions")
    where, params = [], {}
    for i, (k, vals) in enumerate((filters or {}).items()):
        names = [f"f{i}_{j}" for j in range(len(vals))]
        params.update(dict(zip(names, [str(v) for v in vals], strict=True)))
        where.append(f"CAST(`{dim(k)}` AS STRING) IN ({', '.join(':' + n for n in names)})")
    tdim = VIEWS[view]
    if (period_from or period_to) and not tdim:
        raise ValueError(f"metric {metric} has no time dimension")
    if period_from:
        where.append(f"`{tdim}` >= :period_from")
        params["period_from"] = period_from
    if period_to:
        where.append(f"`{tdim}` <= :period_to")
        params["period_to"] = period_to
    count = COUNT_MEASURE[view]
    select = [f"`{g}`" for g in gb] + [f"MEASURE(`{m['measure']}`) AS value", f"MEASURE(`{count}`) AS n"]
    q = (f"SELECT {', '.join(select)} FROM {CAT}.gold.{view}" + (f" WHERE {' AND '.join(where)}" if where else "")
         + (" GROUP BY ALL ORDER BY value DESC" if gb else "") + " LIMIT 500")
    rows = query(q, params)
    for r in rows:
        r["low_sample"] = (r.get("n") or 0) < LOW_SAMPLE and m["measure"] != count
    return {"result": rows, "metric": metric, "label": m["measure"], "definition": m["expr"], "sql": q,
            "parameters": params}


@mcp.tool()
@audited("analytics")
def search_suppliers(query_text: str, country: str | None = None, limit: int = 10, ctx: Context | None = None) -> dict:
    """Find golden suppliers by name in any spelling or script (Latin, Cyrillic, Polish; transliterations,
    abbreviations and legal forms are normalized). Returns supplier_id with the matched name and a score."""
    q = normalize_name(query_text)["skeleton"]
    if not q:
        raise ValueError("query has no letters or digits")
    params = {"q": q, "raw": f"%{query_text.lower()}%", "lim": max(1, min(int(limit), 50))}
    cond = "AND s.country = :country" if country else ""
    if country:
        params["country"] = country.upper()
    rows = query(f"""
        WITH scored AS (
          SELECT n.supplier_id, n.name AS matched_name,
                 CASE WHEN n.skeleton = :q THEN 1.0
                      WHEN lower(n.name) LIKE :raw THEN 0.95
                      WHEN n.skeleton LIKE concat('%', :q, '%') THEN 0.9
                      ELSE 1 - levenshtein(n.skeleton, :q) / greatest(length(n.skeleton), length(:q), 1) END AS score
          FROM {CAT}.gold.supplier_name_index n)
        SELECT s.supplier_id, s.legal_name, s.legal_name_local, s.country, s.city, s.main_category, s.risk_band,
               round(s.lifetime_spend_usd, 0) AS lifetime_spend_usd, max_by(c.matched_name, c.score) AS matched_name,
               round(max(c.score), 3) AS score
        FROM scored c JOIN {CAT}.gold.supplier_360 s USING (supplier_id)
        WHERE c.score >= 0.75 {cond}
        GROUP BY ALL ORDER BY score DESC, lifetime_spend_usd DESC NULLS LAST LIMIT :lim""", params)
    return {"result": rows, "normalized_query": q}


@mcp.tool()
@audited("analytics")
def get_spend_concentration(category: str | None = None, ctx: Context | None = None) -> dict:
    """Spend concentration per category over the last 12 months: Herfindahl-Hirschman index (HHI, 0-10,000;
    >= 2,500 highly concentrated, 1,500-2,500 moderately), number of suppliers, top-1 and top-3 shares and the top
    supplier. Optionally one category (name or id)."""
    cond, p = "", {}
    if category:
        cond, p = "WHERE upper(c.category_name) = upper(:c) OR upper(c.category_id) = upper(:c)", {"c": category}
    q = f"""SELECT c.category_name, c.spend_type, round(c.spend_usd_12m, 2) AS spend_usd_12m, c.suppliers, c.hhi,
                   c.top1_share, c.top3_share, c.concentration, c.top_supplier_id, s.legal_name AS top_supplier
            FROM {CAT}.gold.spend_concentration c LEFT JOIN {CAT}.gold.supplier_360 s ON s.supplier_id = c.top_supplier_id
            {cond} ORDER BY c.hhi DESC"""
    return {"result": query(q, p), "definition": "HHI = sum over suppliers of (share of category spend x 100)^2",
            "sql": q}


SORTS = {"risk": "risk_score DESC", "spend": "lifetime_spend_usd DESC", "dq": "dq_score ASC",
         "deterioration": "on_time_drop DESC", "name": "legal_name ASC"}


@mcp.tool()
@audited("analytics")
def find_suppliers(risk_band: str | None = None, deteriorating: bool | None = None, category: str | None = None,
                   country: str | None = None, has_dq_issues: bool | None = None,
                   compliance_flag: str | None = None, sort: str = "risk", limit: int = 25,
                   ctx: Context | None = None) -> dict:
    """List golden suppliers matching governed conditions (all optional, combined with AND):
    risk_band 'high' | 'medium' | 'low'; deteriorating (delivery performance, market-adjusted, statistically tested);
    category (e.g. 'Logistics', 'IT services', 'Direct ingredients'); country (ISO code); has_dq_issues (master data);
    compliance_flag 'expired_certificate' | 'sanctions_alert' | 'sanctions_confirmed' | 'purchasing_blocked'.
    sort: risk | spend | dq | deterioration | name."""
    where, p = [], {}
    if risk_band:
        where.append("s.risk_band = :rb")
        p["rb"] = risk_band.lower()
    if deteriorating is not None:
        where.append("s.deteriorating = :det")
        p["det"] = bool(deteriorating)
    if category:
        where.append("(upper(c.category_name) = upper(:cat) OR upper(s.main_category) = upper(:cat))")
        p["cat"] = category
    if country:
        where.append("s.country = :cc")
        p["cc"] = country.upper()
    if has_dq_issues is not None:
        where.append("(size(s.dq_issues) > 0) = :dq")
        p["dq"] = bool(has_dq_issues)
    flags = {"expired_certificate": "s.expired_certificates > 0", "sanctions_alert": "s.sanctions_alerts > 0",
             "sanctions_confirmed": "array_contains(s.risk_drivers, 'confirmed sanctions match')",
             "purchasing_blocked": "s.purchasing_blocked"}
    if compliance_flag:
        if compliance_flag not in flags:
            raise ValueError(f"compliance_flag must be one of {sorted(flags)}")
        where.append(flags[compliance_flag])
    if sort not in SORTS:
        raise ValueError(f"sort must be one of {sorted(SORTS)}")
    q = f"""SELECT s.supplier_id, s.legal_name, s.legal_name_local, s.country, c.category_name AS main_category,
                   s.risk_band, s.risk_score, array_join(s.risk_drivers, '; ') AS risk_drivers,
                   round(s.lifetime_spend_usd, 0) AS lifetime_spend_usd, s.deteriorating,
                   round(s.on_time_baseline, 3) AS on_time_baseline, round(s.on_time_recent, 3) AS on_time_recent,
                   s.dq_score, array_join(s.dq_issues, ', ') AS dq_issues
            FROM {CAT}.gold.supplier_360 s LEFT JOIN {CAT}.gold.dim_category c ON c.category_id = s.main_category
            {"WHERE " + " AND ".join(where) if where else ""}
            ORDER BY {SORTS[sort].replace("risk_score", "s.risk_score").replace("legal_name", "s.legal_name")} NULLS LAST
            LIMIT {max(1, min(int(limit), 200))}"""
    total = query(f"""SELECT count(*) n FROM {CAT}.gold.supplier_360 s
                      LEFT JOIN {CAT}.gold.dim_category c ON c.category_id = s.main_category
                      {"WHERE " + " AND ".join(where) if where else ""}""", p)[0]["n"]
    return {"result": query(q, p), "total_matching": total, "sql": q, "parameters": p}


@mcp.tool()
@audited("analytics")
def get_supplier_360(supplier_id: str, ctx: Context | None = None) -> dict:
    """Everything about one golden supplier: identity, spend, delivery trend, risk (with drivers), data quality,
    the source-system records it was resolved from, its documents, and sanctions screening status."""
    p = {"sid": supplier_id}
    s = query(f"SELECT * FROM {CAT}.gold.supplier_360 WHERE supplier_id = :sid", p)
    if not s:
        raise ValueError(f"no supplier {supplier_id}; use search_suppliers")
    records = query(f"""SELECT r.source, r.source_id, r.name, r.name_local, r.country, r.city, r.national_id_type,
                               r.national_id, r.national_id_valid, r.status
                        FROM {CAT}.silver.er_clusters c JOIN {CAT}.silver.supplier_records r USING (source, source_id)
                        WHERE c.supplier_id = :sid""", p)
    docs = query(f"""SELECT doc_type, doc_key, file_name, party_name, start_date, end_date, certificate_type,
                            contract_value, currency, payment_terms_days, metadata_mismatch
                     FROM {CAT}.gold.supplier_documents WHERE supplier_id = :sid ORDER BY doc_type, end_date""", p)
    screening = query(f"""SELECT sanctioned_name, program, score, status FROM {CAT}.gold.supplier_sanctions_hits
                          WHERE supplier_id = :sid ORDER BY score DESC""", p)
    return {"result": s[0], "source_records": records, "documents": docs, "sanctions_screening": screening,
            "notes": ["sanctions screening rows are name-similarity alerts unless status = 'confirmed'"],
            "provenance": [f"{CAT}.gold.supplier_360", f"{CAT}.silver.er_clusters", f"{CAT}.silver.supplier_records",
                           f"{CAT}.gold.supplier_documents", f"{CAT}.gold.supplier_sanctions_hits"]}


@mcp.tool()
@audited("analytics")
def explain_supplier_match(supplier_id: str, ctx: Context | None = None) -> dict:
    """Why these source records were resolved into one supplier: every accepted link with its method
    (steward, deterministic ID, exact name, probabilistic, LLM), probability, evidence and LLM reasoning."""
    p = {"sid": supplier_id}
    links = query(f"""SELECT unique_id_l, unique_id_r, method, round(match_probability, 4) AS match_probability,
                             evidence, llm_reason
                      FROM {CAT}.gold.supplier_match_evidence WHERE supplier_id = :sid
                      ORDER BY method, match_probability DESC""", p)
    records = query(f"""SELECT concat(r.source, ':', r.source_id) AS unique_id, r.name, r.name_local, r.country,
                               r.city, r.national_id
                        FROM {CAT}.silver.er_clusters c JOIN {CAT}.silver.supplier_records r USING (source, source_id)
                        WHERE c.supplier_id = :sid""", p)
    return {"result": links, "records": records,
            "methods": {"steward": "decided by a person", "deterministic": "same valid national ID",
                        "exact_name": "same normalized name and country, nothing contradicting",
                        "probabilistic": "Splink match probability >= 0.95",
                        "llm": "uncertain pair (0.50-0.95) judged a match by the LLM"}}


@mcp.tool()
@audited("analytics")
def trace_metric(metric: str | None = None, table: str | None = None, supplier_id: str | None = None,
                 ctx: Context | None = None) -> dict:
    """Where a number comes from: Unity Catalog lineage from a metric (or table) back through gold, silver and
    bronze to the source systems. With supplier_id, also the supplier's source records and documents."""
    if metric:
        reg = registry()
        if metric not in reg:
            raise ValueError(f"unknown metric {metric!r}")
        start = f"{CAT}.gold.{reg[metric]['view']}"
    elif table:
        start = table if table.count(".") == 2 else f"{CAT}.{table}"
    else:
        raise ValueError("give a metric or a table")
    edges = query(f"SELECT source, target FROM {CAT}.ops.lineage_edges")
    up: dict[str, set] = {}
    for e in edges:
        up.setdefault(e["target"], set()).add(e["source"])
    levels, seen, frontier = [], {start}, [start]
    while frontier:
        level = []
        for t in frontier:
            level.append({"table": t, "built_from": sorted(up.get(t, set())),
                          "origin": "landing files (source systems)" if ".bronze." in t else None})
        levels.append(level)
        frontier = [s for t in frontier for s in sorted(up.get(t, set())) if s not in seen]
        seen.update(frontier)
    out = {"result": levels, "start": start, "tables": len(seen)}
    if supplier_id:
        out["supplier_records"] = query(f"""SELECT source, source_id FROM {CAT}.silver.er_clusters
                                            WHERE supplier_id = :sid""", {"sid": supplier_id})
        out["supplier_documents"] = query(f"""SELECT doc_key, file_name FROM {CAT}.gold.supplier_documents
                                              WHERE supplier_id = :sid""", {"sid": supplier_id})
    return out


@mcp.tool()
@audited("analytics")
def simulate_consolidation(category: str, consolidate_into: str, from_suppliers: list[str] | None = None,
                           price_reduction_pct: float = 0.0, ctx: Context | None = None) -> dict:
    """What-if: move last-12-month spend in a category from other suppliers (all, or from_suppliers) to one
    supplier (consolidate_into = supplier_id). Returns concentration (HHI, top share) before and after, moved spend,
    savings at the assumed price reduction, and the target's delivery and risk profile. A what-if, not a forecast."""
    cats = {r["category_id"]: r["category_name"] for r in query(f"SELECT * FROM {CAT}.gold.dim_category")}
    cid = next((k for k, v in cats.items() if category.upper() in (k, v.upper())), None)
    if not cid:
        raise ValueError(f"unknown category {category!r}; one of {sorted(cats.values())}")
    spend = query(f"""WITH a AS (SELECT max(order_date) d FROM {CAT}.gold.fact_po_line)
        SELECT f.supplier_id, sum(f.amount_usd) AS spend FROM {CAT}.gold.fact_po_line f CROSS JOIN a
        WHERE f.category_id = :c AND f.order_date > a.d - INTERVAL 365 DAYS GROUP BY 1""", {"c": cid})
    before = {r["supplier_id"]: float(r["spend"] or 0) for r in spend}
    if consolidate_into not in before:
        raise ValueError(f"{consolidate_into} has no {cats[cid]} spend in the last 12 months")
    movers = set(from_suppliers) if from_suppliers else set(before) - {consolidate_into}
    after = dict(before)
    moved = sum(after.pop(s, 0.0) for s in movers if s != consolidate_into)
    after[consolidate_into] = after.get(consolidate_into, 0.0) + moved * (1 - price_reduction_pct / 100)

    def stats(d):
        tot = sum(d.values()) or 1.0
        shares = sorted((v / tot for v in d.values()), reverse=True)
        return {"spend_usd": round(tot, 2), "suppliers": len(d), "hhi": round(sum((s * 100) ** 2 for s in shares)),
                "top1_share": round(shares[0], 4) if shares else 0}
    target = query(f"""SELECT legal_name, country, risk_band, risk_score, deteriorating, on_time_recent,
                              array_join(risk_drivers, '; ') AS risk_drivers
                       FROM {CAT}.gold.supplier_360 WHERE supplier_id = :s""", {"s": consolidate_into})[0]
    a = stats(after)
    warnings = []
    if target["deteriorating"]:
        warnings.append("target supplier's delivery performance is deteriorating")
    if target["risk_band"] in ("high", "medium"):
        warnings.append(f"target supplier risk band is {target['risk_band']}: {target['risk_drivers']}")
    if a["top1_share"] > 0.5:
        warnings.append("target would hold more than half of the category: single-source dependency")
    return {"result": {"category": cats[cid], "before": stats(before), "after": a,
                       "moved_spend_usd": round(moved, 2),
                       "estimated_savings_usd": round(moved * price_reduction_pct / 100, 2),
                       "target": {"supplier_id": consolidate_into, **target}, "warnings": warnings},
            "assumptions": ["last 12 months of PO spend", f"{price_reduction_pct}% price reduction on moved spend",
                            "volumes unchanged; no switching costs"]}


_READONLY = re.compile(r"^\s*(select|with)\b", re.I)


@mcp.tool()
@audited("analytics")
def run_exploratory_sql(sql: str, ctx: Context | None = None) -> dict:
    """Fallback for questions no governed metric covers: one read-only SELECT over the wizard.gold tables
    (max 200 rows). Results are exploratory, not governed definitions; say so when you use them."""
    if not _READONLY.match(sql) or ";" in sql.strip().rstrip(";"):
        raise ValueError("only a single SELECT / WITH statement is allowed")
    if re.search(r"\b(silver|bronze|system|ops)\.", sql, re.I):
        raise ValueError("exploratory SQL may only read wizard.gold tables")
    rows = query(f"SELECT * FROM ({sql.strip().rstrip(';')}) LIMIT 200")
    return {"result": rows, "exploratory": True, "note": "exploratory query, not a governed metric"}
