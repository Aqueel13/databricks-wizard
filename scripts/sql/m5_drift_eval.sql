-- M5: tune / evaluate the delivery-deterioration KPI against the generator's drift storyline.
with gs as (select * from parquet.`/Volumes/wizard/ops/ground_truth/gt_supplier.parquet`),
gr as (select * from parquet.`/Volumes/wizard/ops/ground_truth/gt_source_record.parquet`),
drift as (select distinct c.supplier_id from gs join gr using (true_supplier_id)
          join wizard.silver.er_clusters c on c.source = gr.source_system and c.source_id = gr.source_id
          where gs.storyline = 'delivery_drift'),
f as (select *, cast(on_time as int) ot, date_trunc('month', received_date) m from wizard.gold.fact_delivery where supplier_id is not null),
mkt as (select m, avg(ot) mkt from f group by m),
asof as (select max(received_date) d from f),
x as (select f.supplier_id, f.ot - mkt.mkt ex,
             case when f.received_date >= a.d - interval 180 days then 'r'
                  when f.received_date >= a.d - interval 540 days then 'b' end w
      from f join mkt using (m) cross join asof a),
s as (select supplier_id, count_if(w='b') nb, avg(case when w='b' then ex end) mb,
             count_if(w='r') nr, avg(case when w='r' then ex end) mr from x group by 1),
z as (select *, (mb - mr) effect, (mb - mr) / sqrt(0.25/nr + 0.25/nb) zscore,
             supplier_id in (select supplier_id from drift) is_drift from s where nb >= 8 and nr >= 4),
grid as (select * from values (1.5,0.10),(1.5,0.15),(2.0,0.10),(2.0,0.15),(2.0,0.20),(2.5,0.15),(2.5,0.20),(3.0,0.15) g(zt, et))
select 'z-test, market-adjusted' method, zt, et, count_if(is_drift and zscore >= zt and effect >= et) tp,
       count_if(not is_drift and zscore >= zt and effect >= et) fp, count_if(is_drift) drift_eligible, (select count(*) from drift) drift_total
from z cross join grid group by all
union all
select 'current: own baseline -15pp', null, null, count_if(t.supplier_id in (select supplier_id from drift) and deteriorating),
       count_if(t.supplier_id not in (select supplier_id from drift) and deteriorating), null, null
from wizard.gold.supplier_delivery_trend t
order by 1, 2, 3
