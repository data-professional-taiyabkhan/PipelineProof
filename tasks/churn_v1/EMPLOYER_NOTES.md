# What this task tests

Candidates never see this page.

The dataset is synthetic and built so that careful and careless pipelines score differently on
customers the model has never seen. It contains three planted traps and some ordinary data
quality problems.

| Trap | What a careful candidate does | What PipelineProof measures |
|---|---|---|
| **A field from the future.** `retention_offer_sent` is set at month end, after a customer asks to cancel, so it is 0 for everyone on the 1st. On its own it predicts churn with an AUC of about 0.91 in the training data. | Reads the data dictionary, notices when the field is filled in, and leaves it out. | An ablation re-run sets the field to its prediction-time value (0). If the candidate's own score falls by 0.03 or more, the report raises a critical leakage finding. |
| **The same customer in many rows.** Each customer appears in 2 to 6 monthly snapshots. | Splits train and test by customer (for example `GroupShuffleSplit` or `GroupKFold` on `customer_id`). | The trace records the split. If test rows share customers with training, the report compares the candidate's own score on seen and unseen customers, with a 95% interval. |
| **Duplicated rows.** About 4% of rows are exact duplicates from a CRM export bug. | Removes them before splitting. | Data handling evidence, and duplicates that cross the split inflate the candidate's own score. |

Ordinary data problems: inconsistent capitalisation in `region`, missing values in
`monthly_spend` and `last_login_days`, and negative `monthly_spend` (refunds).

## The holdout

The holdout is 1,500 customer snapshots from customers who are not in `train.csv`, in their state
on the 1st of a month (`retention_offer_sent` is 0 for all of them). The holdout labels never
enter the sandbox that runs the candidate's code; predictions are scored outside it.

## Reading the gap

A careful pipeline usually scores slightly lower on the holdout than on its own validation split,
because unseen customers are harder. A gap above about 0.06 deserves a look; above 0.10 the report
asks for review.
