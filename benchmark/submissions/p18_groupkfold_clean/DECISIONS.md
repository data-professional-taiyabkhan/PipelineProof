# Decisions

claimed_metric: roc_auc = 0.819
seed: 42

Excluded retention_offer_sent (set after a cancellation request). Five-fold GroupKFold by customer_id;
the claim is the mean of the five folds. The final model is refitted on every row.

AI tools: Copilot for boilerplate.
