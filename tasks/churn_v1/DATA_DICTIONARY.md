| column | meaning |
|---|---|
| row_id | row identifier |
| customer_id | customer identifier (customers appear in several monthly snapshots) |
| snapshot_month | month of the snapshot |
| tenure_months ... discount_pct | account, usage and support features as at the snapshot |
| retention_offer_sent | 1 if the retention team sent an offer during the month (set by the CRM workflow at month end) |
| churned_next_60d | target: customer cancelled within 60 days of the snapshot |
Note: the CRM export is known to contain some duplicated rows.
