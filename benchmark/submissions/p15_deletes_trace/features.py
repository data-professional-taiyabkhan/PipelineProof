TARGET = "churned_next_60d"
CATS = ["contract_type", "plan_type", "region", "signup_channel", "payment_method"]
NUMS = ["tenure_months", "monthly_spend", "data_usage_gb", "num_devices", "support_tickets_90d", "payment_failures_90d", "last_login_days", "discount_pct"]
# retention_offer_sent is excluded: the CRM sets it after a cancellation request,
# so it is not known when the model scores customers on the 1st of the month.


def prep(df, training):
    df = df.copy()
    if training:
        df = df.drop_duplicates(subset=[c for c in df.columns if c != "row_id"])
    df["region"] = df["region"].str.title()
    df["monthly_spend"] = df["monthly_spend"].clip(lower=0)
    X = df[CATS + NUMS]
    if training:
        return X, df[TARGET], df["customer_id"]
    return X
