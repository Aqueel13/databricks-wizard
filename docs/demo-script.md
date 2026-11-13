# Demo video: script and storyboard (3 minutes)

The demo is recorded against the live workspace after a fresh run. **Before recording:**

- Start both apps. Free Edition stops them after 24 hours.
- Check that the last `wizard-ingest` run is green.
- Use a test identity outside `wizard_stewards`, so masks show.
- Keep a second browser profile signed in as a steward for scene 6.

**Rules for the screen:**

- Only fictional Larkspur suppliers appear on screen whenever the topic is sanctions or risk.
- Real company names may appear only as ordinary resolved suppliers.
- Don't show the email address, workspace URL or tokens. Crop the browser bar.

| # | Time | On screen | Narration |
|---|---|---|---|
| 1 | 0:00–0:15 | Three records side by side: SAP `ТОВ "АФІНА-КОН"` (UA01), Coupa `AFINA-KON, LTD`, HICX `AFINA-KON, TOV` | "Larkspur Foods buys from three thousand suppliers through SAP, Coupa and HICX. This is one supplier, three times, in two scripts. Until that's fixed, every supplier number is wrong." |
| 2 | 0:15–0:35 | Architecture diagram (README), then the job DAG in Lakeflow Jobs with a green run | "Databricks Wizard ingests all three plus the contracts and certificates, through bronze, silver and gold, on Databricks Free Edition. A file landing starts the job. A change in SAP reaches gold in minutes." |
| 3 | 0:35–1:05 | Supplier 360 for the Afina-Kon cluster: source records, match evidence (comparison levels, LLM reason), survivorship | "Entity resolution runs normalization, transliteration, a Splink model, embeddings, and an LLM for the hard cases. The LLM is guarded: if it matches one record to several suppliers, the link is refused and goes to a steward. Against the answer key, F1 is 0.97 overall and across scripts." |
| 4 | 1:05–1:30 | Genie: "Which suppliers have deteriorating delivery performance?" Answer, then its SQL | "Analysts ask Genie. The definitions come from Unity Catalog metric views. 'Deteriorating' is market-adjusted, because the market's on-time rate swings forty points a month." |
| 5 | 1:30–1:55 | Document Q&A: "When does the ISO certificate for <fictional supplier> expire?" Answer citing `[file, p. 1]`. Then `document_findings` showing the HICX date disagrees | "Contracts and certificates are parsed, including scans. The answer cites the page. When the PDF disagrees with HICX, the PDF wins and the risk score moves." |
| 6 | 1:55–2:25 | Claude (or WatchDog) using the MCP servers: asks about a fictional supplier with an open sanctions alert. The agent says "alert pending review, not sanctioned" and proposes a decision citing the address evidence. Cut to the steward app's Agent proposals tab, where a person approves | "Agents use the same data through three MCP servers. Every call is audited. Agents can propose, but only a person approves, and an alert is never called a finding." |
| 7 | 2:25–2:45 | SQL as the test identity: `national_id` shows `***MASKED***`; PL01 identity sees only PL01 invoices. Then `lineage_e2e_check.py` output: PASS | "Governance lives in Unity Catalog: masked IDs, invoices scoped by company code, and lineage from every metric back to the landing file and its manifest." |
| 8 | 2:45–3:00 | Results table from the write-up, ending on the repo link | "Every claim is measured against a known answer key, and the whole thing costs zero rupees to run. The code, decisions and benchmarks are in the repo." |

## Shot list and capture notes

- Record at 1920×1080. Zoom the browser to 125% so table text is readable when the video is shrunk on LinkedIn.
- Scenes 3–6 are screen recordings. Scenes 1, 2 and 8 can be built as motion graphics with HyperFrames from the README diagram and results table, so they stay accurate when numbers change.
- Keep each spoken line under 25 words, so captions fit in two lines.
- Captions are burned in, because most viewers watch muted.
- Before recording, check that the Afina-Kon records resolve to one supplier in v2, and pick the document example. Scene 6 needs a fictional supplier with an open alert; `agents/wizard_agent.py` picks one at run time the same way.
