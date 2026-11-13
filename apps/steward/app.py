"""Databricks Wizard: data steward workbench (Databricks App, Streamlit)."""
import streamlit as st

import data

st.set_page_config(page_title="Supplier steward", layout="wide")
steward = st.context.headers.get("X-Forwarded-Email") or st.context.headers.get("X-Forwarded-Preferred-Username") or "unknown"

st.title("Supplier data steward")
st.caption(f"Signed in as {steward}. Decisions are recorded with your name and take effect on the next "
           "entity-resolution run.")

tab_match, tab_alerts, tab_agent, tab_log = st.tabs(["Match review", "Sanctions alerts", "Agent proposals",
                                                     "Decisions and apply"])


PAGE = 25


def paged(df, key: str, cols: list[str]):
    """Filter (substring over cols) and page a queue; returns the rows to render."""
    f, pg = st.columns([3, 1])
    needle = f.text_input("Filter", key=f"{key}_filter", placeholder="name, ID, country or reason")
    if needle:
        hay = df[cols].astype(str).agg(" ".join, axis=1).str.lower()
        df = df[hay.str.contains(needle.lower(), regex=False)]
    pages = max(1, -(-len(df) // PAGE))
    page = pg.number_input(f"Page (of {pages})", min_value=1, max_value=pages, value=1, key=f"{key}_page")
    st.caption(f"{len(df)} matching · showing {(page - 1) * PAGE + 1 if len(df) else 0}-"
               f"{min(page * PAGE, len(df))}")
    return df.iloc[(page - 1) * PAGE: page * PAGE]


def fmt(name, local, country, city, id_type, nid):
    lines = [f"**{name}**"]
    if local:
        lines.append(f"local script: {local}")
    lines.append(" · ".join(x for x in [country, city] if x) or "no location")
    lines.append(f"{id_type or 'ID'}: {nid}" if nid else "no national ID")
    return "  \n".join(lines)


with tab_match:
    q = data.review_queue()
    st.subheader(f"{len(q)} pairs waiting for a decision")
    st.write("Pairs the matcher could not settle: the LLM was unsure, or a link was refused because it would merge "
             "records with conflicting facts.")
    for _, r in paged(q, "match", ["pair_key", "reason", "l_name", "r_name", "l_local", "r_local", "l_id",
                                   "r_id", "l_country", "r_country"]).iterrows():
        with st.container(border=True):
            st.markdown(f"`{r.pair_key}` · probability **{r.match_probability:.2f}** · {r.reason}")
            a, b = st.columns(2)
            a.markdown(fmt(r.l_name, r.l_local, r.l_country, r.l_city, r.l_id_type, r.l_id))
            b.markdown(fmt(r.r_name, r.r_local, r.r_country, r.r_city, r.r_id_type, r.r_id))
            if isinstance(r.llm_reason, str) and r.llm_reason:
                st.caption(f"LLM: {r.llm_reason}")
            note = st.text_input("Note", key=f"n_{r.pair_key}", placeholder="why (optional)")
            c1, c2, _ = st.columns([1, 1, 4])
            if c1.button("Same supplier", key=f"m_{r.pair_key}", type="primary"):
                data.decide_pair(r.pair_key, "match", steward, note)
                st.rerun()
            if c2.button("Different suppliers", key=f"x_{r.pair_key}"):
                data.decide_pair(r.pair_key, "no_match", steward, note)
                st.rerun()

with tab_alerts:
    al = data.sanctions_alerts()
    st.subheader(f"{len(al)} open screening alerts")
    st.warning("An alert is a NAME SIMILARITY with an OFAC sanctions list entry. It is not a finding. Confirm only "
               "with corroborating evidence (address, registration, ownership).")
    for i, r in paged(al, "alerts", ["legal_name", "legal_name_local", "sanctioned_name", "screened_name",
                                     "country", "program"]).iterrows():
        with st.container(border=True):
            a, b = st.columns(2)
            a.markdown("**Supplier**  \n" + fmt(r.legal_name, r.legal_name_local, r.country, r.city,
                                                r.national_id_type, r.national_id))
            b.markdown(f"**OFAC entry {r.ent_num}**  \n**{r.sanctioned_name}**  \n"
                       f"program: {r.program or '-'} · type: {r.sdn_type or 'entity'} · name type: {r.name_type}  \n"
                       f"similarity: {r.score:.2f} (matched on: {r.screened_name})")
            note = st.text_input("Note", key=f"an_{i}", placeholder="evidence for the decision")
            c1, c2, _ = st.columns([1, 1, 4])
            if c1.button("Confirm match", key=f"ac_{i}", type="primary"):
                data.decide_alert(r.to_dict(), "confirm", steward, note)
                st.rerun()
            if c2.button("Dismiss", key=f"ad_{i}"):
                data.decide_alert(r.to_dict(), "dismiss", steward, note)
                st.rerun()

with tab_agent:
    pr = data.proposals("pending")
    st.subheader(f"{len(pr)} agent proposals waiting for review")
    st.write("Agents using the stewardship MCP server can propose decisions but never approve them. Approving writes "
             "the decision under your name, citing the proposal.")
    for _, p in pr.iterrows():
        with st.container(border=True):
            st.markdown(f"**{p.kind}** `{p.item_key}` → proposed **{p.decision}** by {p.proposed_by} "
                        f"({str(p.created_at)[:16]})")
            st.markdown(f"> {p.rationale}")
            note = st.text_input("Note", key=f"pn_{p.proposal_id}", placeholder="optional")
            c1, c2, _ = st.columns([1, 1, 4])
            if c1.button("Approve", key=f"pa_{p.proposal_id}", type="primary"):
                data.review_proposal(p.to_dict(), True, steward, note)
                st.rerun()
            if c2.button("Reject", key=f"pr_{p.proposal_id}"):
                data.review_proposal(p.to_dict(), False, steward, note)
                st.rerun()

with tab_log:
    if st.button("Apply decisions now (re-run entity resolution and gold)", type="primary"):
        run_id = data.apply_decisions()
        st.success(f"Started job run {run_id}. Results update in a few minutes.")
    st.dataframe(data.decision_log(), use_container_width=True, hide_index=True)
