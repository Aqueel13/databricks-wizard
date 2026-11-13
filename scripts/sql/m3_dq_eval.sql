-- M3 exit test: recall of injected defects that single-source DQ rules are responsible for.
-- Defect types detected by later milestones (entity resolution, KPIs, document extraction) are excluded.
with gt as (
  select * from parquet.`/Volumes/wizard/ops/ground_truth/gt_defect.parquet`
  where defect_type in ('missing_national_id', 'invalid_national_id', 'invoice_amount_mismatch',
                        'duplicate_invoice', 'expired_certificate', 'schema_drift')
),
scored as (
  select gt.*,
         exists (select 1 from wizard.silver.dq_findings f
                 where f.rule = gt.defect_type and f.source = gt.source_system
                   and f.source_id = case when gt.defect_type = 'schema_drift' then replace(gt.field, '_', '-')
                                          else gt.entity_key end) as detected
  from gt
),
findings as (select rule, source, count(*) n from wizard.silver.dq_findings group by all)
select s.defect_type, s.source_system, count(*) injected, count_if(detected) detected,
       round(100.0 * count_if(detected) / count(*), 1) recall_pct,
       max(f.n) total_findings
from scored s left join findings f on f.rule = s.defect_type and f.source = s.source_system
group by all order by 1, 2
