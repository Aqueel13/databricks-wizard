-- M6 exit test: planted document-vs-system mismatches found by reconciliation (gold.supplier_documents).
-- Recall: every certificate/contract whose PDF was generated with a different date or value than HICX/Coupa
-- holds must be flagged metadata_mismatch. False positives: flagged documents with no planted mismatch.
with gt as (
  select defect_type, entity_key as doc_key from parquet.`/Volumes/wizard/ops/ground_truth/gt_defect.parquet`
  where defect_type in ('certificate_metadata_mismatch', 'contract_metadata_mismatch')),
flagged as (select doc_key, doc_type from wizard.gold.supplier_documents where metadata_mismatch)
select coalesce(g.defect_type, f.doc_type || ' (not planted)') as kind,
       count(g.doc_key) planted, count_if(g.doc_key is not null and f.doc_key is not null) found,
       count_if(g.doc_key is null) false_positives
from gt g full outer join flagged f using (doc_key)
group by 1 order by 1
