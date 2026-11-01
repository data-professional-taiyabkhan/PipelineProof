# Predict which broadband customers will cancel in the next 60 days

You have joined the data team of a UK broadband provider. On the 1st of every month the retention
team wants a ranked list of active customers, most likely to cancel within the next 60 days first,
so they can contact them before they leave.

## Your data

`train.csv` holds monthly customer snapshots from January 2024 to September 2025. The data
dictionary explains every column. Treat it as a real export from the company's systems.

## What your model will be used for

On the 1st of a month, your model scores customers using the information available **on that day**.
We will test it the same way: on customers it has never seen, in the state they were in at the start
of a month.

## What to hand in

A zip file containing:

- `train.py` and `predict.py` (or notebooks), following the submission guide
- `requirements.txt` with every package pinned (`pandas==2.2.3`, not `pandas`)
- `pipeline.toml` naming your Python version and your train and predict commands
- `README.md` explaining how your pipeline works
- `DECISIONS.md` stating your validation score as `claimed_metric: roc_auc = <value>`, the seed you
  used as `seed: <int>`, how you validated your model, and which AI tools you used, if any

Start from the starter template: it already reads and writes the right files.

## Rules

- Time box: about 3 hours. Any tools are allowed, including AI assistants; say which you used.
- Read your random seed from the `PP_SEED` environment variable, so your result can be reproduced.
- Your code runs without internet access, using only the files you submit and the data we provide.
- The metric is ROC AUC on the predicted probability of `churned_next_60d`.
