"""Synthetic churn task with planted traps, for the PipelineProof feasibility spike.

Traps (known to the task author, not to the candidate):
  T1  retention_offer_sent  - post-outcome field (set by CRM after a cancellation request).
                              Always 0 at prediction time, so it is 0 for every holdout row.
  T2  repeated customers    - 2-6 monthly snapshots per customer; a random row split puts the
                              same customer in train and test. Holdout = unseen customers.
  T3  exact duplicate rows  - ~4% of rows duplicated by a CRM export bug.
  Data quality: inconsistent region casing, missing values, negative spend (refunds).
"""
import json
import numpy as np
import pandas as pd

rng = np.random.default_rng(20261023)

REGIONS = ["London", "South East", "North West", "Midlands", "Scotland", "Wales"]
CONTRACTS = ["monthly", "annual", "two_year"]
PLANS = ["basic", "standard", "premium"]
CHANNELS = ["web", "store", "partner", "phone"]
PAYMENTS = ["direct_debit", "card", "invoice"]


def make_customers(n, id_start):
    z = rng.normal(0, 1, n)  # latent churn propensity, only partly observable
    contract = rng.choice(CONTRACTS, n, p=[0.45, 0.35, 0.20])
    plan = rng.choice(PLANS, n, p=[0.4, 0.4, 0.2])
    region = rng.choice(REGIONS, n)
    channel = rng.choice(CHANNELS, n, p=[0.5, 0.2, 0.2, 0.1])
    payment = rng.choice(PAYMENTS, n, p=[0.6, 0.3, 0.1])
    base_spend = np.round(rng.gamma(4, 9, n) + np.where(plan == "premium", 25, np.where(plan == "standard", 10, 0)), 2)
    usage = np.round(rng.gamma(3, 40, n), 1)
    devices = rng.integers(1, 6, n)
    risk = (0.75 * z + 0.9 * (contract == "monthly") - 0.7 * (contract == "two_year")
            + 0.4 * (payment == "invoice") - 0.3 * (plan == "premium") - 0.8)
    churner = rng.random(n) < 1 / (1 + np.exp(-risk))
    return pd.DataFrame({
        "customer_id": np.arange(id_start, id_start + n), "z": z, "contract_type": contract,
        "plan_type": plan, "region": region, "signup_channel": channel, "payment_method": payment,
        "base_spend": base_spend, "base_usage": usage, "num_devices": devices, "churner": churner,
    })


def snapshots(cust, first_month, last_month, k_low=2, k_high=6):
    rows = []
    months = pd.period_range(first_month, last_month, freq="M")
    for c in cust.itertuples(index=False):
        k = int(rng.integers(k_low, k_high + 1))
        start = int(rng.integers(0, max(1, len(months) - k)))
        tenure0 = int(rng.integers(1, 48))
        for j in range(k):
            last = j == k - 1
            near_churn = c.churner and j >= k - 2          # final two snapshots of a churner
            label = int(near_churn)
            stress = (0.7 if near_churn else 0.0) + 0.35 * c.z
            tickets = rng.poisson(max(0.05, 0.6 + 0.9 * stress))
            failures = rng.poisson(max(0.02, 0.15 + 0.6 * stress))
            login = rng.exponential(max(1.0, 6 + 9 * stress))
            spend = c.base_spend * rng.normal(1, 0.06)
            discount = rng.choice([0, 10, 20], p=[0.8, 0.15, 0.05])
            # T1: post-outcome CRM field
            if label == 1:
                offer = int(rng.random() < 0.85)
            else:
                offer = int(rng.random() < 0.03)
            rows.append({
                "customer_id": c.customer_id,
                "snapshot_month": str(months[min(start + j, len(months) - 1)]),
                "tenure_months": tenure0 + j,
                "contract_type": c.contract_type, "plan_type": c.plan_type,
                "region": c.region, "signup_channel": c.signup_channel,
                "payment_method": c.payment_method,
                "monthly_spend": round(float(spend), 2),
                "data_usage_gb": round(float(c.base_usage * rng.normal(1, 0.1)), 1),
                "num_devices": int(c.num_devices),
                "support_tickets_90d": int(tickets), "payment_failures_90d": int(failures),
                "last_login_days": round(float(login), 1), "discount_pct": int(discount),
                "retention_offer_sent": offer,
                "churned_next_60d": label,
            })
    return pd.DataFrame(rows)


def dirty(df):
    df = df.copy()
    n = len(df)
    # inconsistent casing in region
    idx = rng.choice(n, int(0.08 * n), replace=False)
    df.loc[df.index[idx], "region"] = df.loc[df.index[idx], "region"].str.lower()
    idx = rng.choice(n, int(0.03 * n), replace=False)
    df.loc[df.index[idx], "region"] = df.loc[df.index[idx], "region"].str.upper()
    # missing values
    df.loc[df.index[rng.choice(n, int(0.10 * n), replace=False)], "last_login_days"] = np.nan
    df.loc[df.index[rng.choice(n, int(0.05 * n), replace=False)], "monthly_spend"] = np.nan
    # refunds show up as negative spend
    idx = rng.choice(n, int(0.01 * n), replace=False)
    df.loc[df.index[idx], "monthly_spend"] = -df.loc[df.index[idx], "monthly_spend"].abs()
    return df


# ---- training data (given to candidates)
train_c = make_customers(5000, 100000)
train = snapshots(train_c, "2024-01", "2025-09")
train = dirty(train)
dups = train.sample(frac=0.04, random_state=7)                      # T3
train = pd.concat([train, dups]).sample(frac=1, random_state=11).reset_index(drop=True)
train.insert(0, "row_id", np.arange(len(train)))

# ---- holdout (never given): unseen customers, one scoring snapshot each, prediction-time view
hold_c = make_customers(1500, 900000)
hold_all = snapshots(hold_c, "2025-10", "2026-03", 2, 6)
hold = hold_all.groupby("customer_id", group_keys=False).sample(n=1, random_state=3)
hold = dirty(hold)
hold["retention_offer_sent"] = 0                                     # T1 at prediction time
hold = hold.reset_index(drop=True)
hold.insert(0, "row_id", np.arange(len(hold)) + 5_000_000)

train.to_csv("data/train.csv", index=False)
hold.drop(columns=["churned_next_60d"]).to_csv("holdout/holdout_features.csv", index=False)
hold[["row_id", "churned_next_60d"]].to_csv("holdout/holdout_labels.csv", index=False)

stats = {
    "train_rows": len(train), "train_customers": int(train.customer_id.nunique()),
    "train_positive_rate": round(float(train.churned_next_60d.mean()), 3),
    "exact_duplicate_rows": int(train.drop(columns="row_id").duplicated().sum()),
    "holdout_rows": len(hold), "holdout_positive_rate": round(float(hold.churned_next_60d.mean()), 3),
    "offer_rate_pos": round(float(train.loc[train.churned_next_60d == 1, "retention_offer_sent"].mean()), 3),
    "offer_rate_neg": round(float(train.loc[train.churned_next_60d == 0, "retention_offer_sent"].mean()), 3),
}
print(json.dumps(stats, indent=2))
