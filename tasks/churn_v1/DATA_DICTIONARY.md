| Column | Type | Meaning |
|---|---|---|
| `row_id` | integer | Row identifier |
| `customer_id` | integer | Customer identifier. Customers appear in several monthly snapshots |
| `snapshot_month` | YYYY-MM | Month the snapshot describes (taken on the 1st) |
| `tenure_months` | integer | Months since the customer joined |
| `contract_type` | category | `monthly`, `annual` or `two_year` |
| `plan_type` | category | `basic`, `standard` or `premium` |
| `region` | category | Region of the service address |
| `signup_channel` | category | How the customer joined: `web`, `store`, `partner`, `phone` |
| `payment_method` | category | `direct_debit`, `card` or `invoice` |
| `monthly_spend` | £ | Amount billed in the month before the snapshot |
| `data_usage_gb` | GB | Data used in the month before the snapshot |
| `num_devices` | integer | Devices registered on the account |
| `support_tickets_90d` | integer | Support tickets in the 90 days before the snapshot |
| `payment_failures_90d` | integer | Failed payments in the 90 days before the snapshot |
| `last_login_days` | days | Days since the customer last logged in to their account |
| `discount_pct` | % | Discount currently applied to the bill |
| `retention_offer_sent` | 0 / 1 | 1 if the retention team sent an offer during the month. Set by the CRM workflow at month end |
| `churned_next_60d` | 0 / 1 | **Target.** 1 if the customer cancelled within 60 days of the snapshot |

Notes from the data owner: the CRM export is known to contain some duplicated rows, and a few
fields were entered by hand.
