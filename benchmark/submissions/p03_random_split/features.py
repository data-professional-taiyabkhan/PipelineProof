TARGET = "churned_next_60d"
CATS = ["contract_type", "plan_type", "region", "signup_channel", "payment_method"]
NUMS = ["tenure_months", "monthly_spend", "data_usage_gb", "num_devices", "support_tickets_90d", "payment_failures_90d", "last_login_days", "discount_pct"]


def prep(df, training):
    X = df[CATS + NUMS]
    if training:
        return X, df[TARGET], df["customer_id"]
    return X
