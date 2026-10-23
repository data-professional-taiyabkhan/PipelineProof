# Task: predict which broadband customers will churn in the next 60 days

You are joining the data team of a UK broadband provider. Each month on the 1st, the retention
team wants a ranked list of active customers most likely to cancel within the next 60 days.

Data: `train.csv` - monthly customer snapshots from Jan 2024 to Sep 2025 (see DATA_DICTIONARY.md).
Your model will score customers you have not seen, at the start of a month.

Deliver: `train.py`, `predict.py`, `requirements.txt` (pinned), `pipeline.toml`, `README.md`,
`DECISIONS.md` (state `claimed_metric: roc_auc = <value>`, `seed: <int>`, how you validated,
and which AI tools you used). Read the seed from the `PP_SEED` environment variable.
Time box: 3 hours. Any tools allowed, including AI assistants.
