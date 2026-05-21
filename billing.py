"""
billing.py — Stripe + quota helpers for the job search app.

Design goals:
  - app.py owns DB connections; this module is pure helpers that take a
    db-connection callable so it stays test-friendly.
  - Free tier gets a small monthly allowance of AI cover letters (the
    expensive operation). Paid tiers get unlimited cover letters; Autopilot
    additionally unlocks the auto-apply queue.
  - Founders (is_founder=1) bypass all quota checks. George's "default"
    user_key is auto-flagged in _init_applications_db().
  - Stripe SDK is lazily imported so the app still boots in environments
    where `stripe` isn't installed yet (graceful local dev).

Env vars consumed:
  STRIPE_SECRET_KEY         (sk_live_... or sk_test_...)
  STRIPE_PUBLISHABLE_KEY    (pk_live_... or pk_test_...) — surfaced via /api/billing/status
  STRIPE_WEBHOOK_SECRET     (whsec_...) — used to verify webhook signatures
  STRIPE_PRICE_ID_PRO       (price_... for the $14/mo Pro plan)
  STRIPE_PRICE_ID_AUTOPILOT (price_... for the $39/mo Autopilot plan)
  BILLING_SUCCESS_URL       (defaults to "/?billing=success")
  BILLING_CANCEL_URL        (defaults to "/?billing=cancel")
"""
import os
from datetime import datetime, timedelta

try:
    import stripe as _stripe
except ImportError:
    _stripe = None


# ── Tier configuration ───────────────────────────────────────────────────────
# Single source of truth for what each tier gets. To raise the free letter
# allowance or add a new tier, edit only this dict.
TIERS = {
    "free": {
        "label":                "Free",
        "price_monthly":        0,
        "cover_letters_month":  3,
        "ai_cv_parse_month":    1,
        "auto_apply":           False,
    },
    "pro": {
        "label":                "Pro",
        "price_monthly":        14,
        "cover_letters_month":  None,   # unlimited
        "ai_cv_parse_month":    None,
        "auto_apply":           False,
    },
    "autopilot": {
        "label":                "Autopilot",
        "price_monthly":        39,
        "cover_letters_month":  None,
        "ai_cv_parse_month":    None,
        "auto_apply":           True,
    },
}

# Map Stripe price IDs → internal tier name. Webhook uses this to set users.tier.
def _price_id_map():
    return {
        os.environ.get("STRIPE_PRICE_ID_PRO", ""):       "pro",
        os.environ.get("STRIPE_PRICE_ID_AUTOPILOT", ""): "autopilot",
    }


def tier_from_price_id(price_id):
    """Given a Stripe price_id, return our internal tier name or 'free'."""
    return _price_id_map().get(price_id or "", "free")


# ── Stripe SDK init ──────────────────────────────────────────────────────────

def stripe_ready():
    """True iff the stripe SDK is importable AND STRIPE_SECRET_KEY is set.
    Routes use this to short-circuit with a clear error instead of crashing."""
    if _stripe is None:
        return False
    key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    if not key:
        return False
    _stripe.api_key = key
    return True


def stripe_sdk():
    """Return the configured Stripe module, or None if not ready.
    Callers should check stripe_ready() first; this is a convenience accessor."""
    if not stripe_ready():
        return None
    return _stripe


# ── Quota math ───────────────────────────────────────────────────────────────

def _row_get(row, key, default=None):
    """Mirrors app._row_get so this module stays standalone."""
    if row is None:
        return default
    try:
        v = row[key]
        return default if v is None else v
    except (KeyError, IndexError):
        return default


def _now_iso():
    return datetime.utcnow().isoformat()


def _next_reset_iso():
    """30-day rolling window from now. Simpler than calendar months and matches
    how customers actually perceive 'monthly' quota (per their signup date)."""
    return (datetime.utcnow() + timedelta(days=30)).isoformat()


def get_user(db_conn_factory, user_id):
    """Return the users row as a dict, or None."""
    with db_conn_factory() as conn:
        cur = conn.execute(
            "SELECT id, user_key, tier, stripe_customer_id, stripe_subscription_id, "
            "subscription_status, current_period_end, monthly_quota_used, "
            "quota_reset_at, is_founder FROM users WHERE id = ?",
            (user_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "id":                     _row_get(row, "id"),
        "user_key":               _row_get(row, "user_key", ""),
        "tier":                   _row_get(row, "tier", "free") or "free",
        "stripe_customer_id":     _row_get(row, "stripe_customer_id", ""),
        "stripe_subscription_id": _row_get(row, "stripe_subscription_id", ""),
        "subscription_status":    _row_get(row, "subscription_status", ""),
        "current_period_end":     _row_get(row, "current_period_end", ""),
        "monthly_quota_used":     int(_row_get(row, "monthly_quota_used", 0) or 0),
        "quota_reset_at":         _row_get(row, "quota_reset_at", ""),
        "is_founder":             bool(_row_get(row, "is_founder", 0)),
    }


def _reset_quota_if_due(db_conn_factory, user):
    """If the user's quota_reset_at is in the past (or unset), roll the meter
    back to 0 and bump the reset date forward 30 days. Returns the (possibly
    refreshed) user dict so callers see the new counters."""
    reset_at = user.get("quota_reset_at") or ""
    if reset_at:
        try:
            if datetime.fromisoformat(reset_at) > datetime.utcnow():
                return user  # still in current window
        except Exception:
            pass  # malformed — treat as expired
    new_reset = _next_reset_iso()
    with db_conn_factory() as conn:
        conn.execute(
            "UPDATE users SET monthly_quota_used = 0, quota_reset_at = ? WHERE id = ?",
            (new_reset, user["id"]),
        )
    user["monthly_quota_used"] = 0
    user["quota_reset_at"]     = new_reset
    return user


def check_quota(db_conn_factory, user_id, feature="cover_letter"):
    """Returns (allowed, info_dict).

    allowed: True if the user may consume this feature now.
    info_dict: {
        "tier": ..., "is_founder": ..., "used": int, "limit": int|None,
        "remaining": int|None, "reason": str ("ok" | "over_quota" | "not_in_plan"),
        "reset_at": iso, "upgrade_url": "/billing"
    }

    feature: "cover_letter", "cv_parse", or "auto_apply".
    """
    user = get_user(db_conn_factory, user_id)
    if user is None:
        return False, {"reason": "user_not_found"}

    # Founders: unlimited everything, no counter increments.
    if user["is_founder"]:
        return True, {
            "tier":        user["tier"] or "founder",
            "is_founder":  True,
            "used":        0,
            "limit":       None,
            "remaining":   None,
            "reason":      "founder",
            "reset_at":    "",
            "upgrade_url": "",
        }

    user = _reset_quota_if_due(db_conn_factory, user)

    tier_cfg = TIERS.get(user["tier"], TIERS["free"])

    # auto_apply is a binary entitlement, not a counted quota
    if feature == "auto_apply":
        allowed = bool(tier_cfg.get("auto_apply"))
        return allowed, {
            "tier":        user["tier"],
            "is_founder":  False,
            "used":        0,
            "limit":       None,
            "remaining":   None,
            "reason":      "ok" if allowed else "not_in_plan",
            "reset_at":    user["quota_reset_at"],
            "upgrade_url": "/billing",
        }

    # Counted features
    limit_key = {
        "cover_letter": "cover_letters_month",
        "cv_parse":     "ai_cv_parse_month",
    }.get(feature)
    if limit_key is None:
        return False, {"reason": "unknown_feature"}

    limit = tier_cfg.get(limit_key)
    used  = user["monthly_quota_used"]
    if limit is None:  # unlimited
        return True, {
            "tier":        user["tier"],
            "is_founder":  False,
            "used":        used,
            "limit":       None,
            "remaining":   None,
            "reason":      "ok",
            "reset_at":    user["quota_reset_at"],
            "upgrade_url": "/billing",
        }

    remaining = max(0, limit - used)
    allowed = remaining > 0
    return allowed, {
        "tier":        user["tier"],
        "is_founder":  False,
        "used":        used,
        "limit":       limit,
        "remaining":   remaining,
        "reason":      "ok" if allowed else "over_quota",
        "reset_at":    user["quota_reset_at"],
        "upgrade_url": "/billing",
    }


def increment_quota(db_conn_factory, user_id, amount=1):
    """Bump the monthly counter by `amount`. No-op for founders and unlimited
    tiers — we still call it after a successful AI call so the meter on the
    dashboard reflects activity even for paid users (used=raw, limit=None
    means "unlimited" in the UI). Safe to call concurrently because the
    UPDATE is single-statement."""
    user = get_user(db_conn_factory, user_id)
    if user is None or user["is_founder"]:
        return
    with db_conn_factory() as conn:
        conn.execute(
            "UPDATE users SET monthly_quota_used = monthly_quota_used + ? WHERE id = ?",
            (int(amount), user_id),
        )


# ── Stripe customer + checkout ───────────────────────────────────────────────

def get_or_create_customer(db_conn_factory, user_id, email=None):
    """Return the user's Stripe customer_id, creating one if needed.
    Stores the id on users.stripe_customer_id so we never create dupes.
    Returns None if Stripe isn't configured."""
    sdk = stripe_sdk()
    if sdk is None:
        return None
    user = get_user(db_conn_factory, user_id)
    if user is None:
        return None
    if user["stripe_customer_id"]:
        return user["stripe_customer_id"]
    # Create a fresh customer. We have no real email for passcode-only users,
    # so use a synthetic one keyed by user_key — Stripe accepts it and we can
    # backfill the real email later when we add email signup.
    synthetic_email = email or f"user-{user['user_key']}@job-search-app.local"
    customer = sdk.Customer.create(
        email=synthetic_email,
        metadata={"app_user_id": str(user_id), "user_key": user["user_key"]},
    )
    with db_conn_factory() as conn:
        conn.execute(
            "UPDATE users SET stripe_customer_id = ? WHERE id = ?",
            (customer.id, user_id),
        )
    return customer.id


def create_checkout_session(db_conn_factory, user_id, tier, base_url, email=None):
    """Create a Stripe Checkout Session for the requested tier ("pro" or
    "autopilot") and return its URL. Returns (url, error_message)."""
    if tier not in ("pro", "autopilot"):
        return None, f"Invalid tier: {tier}"
    sdk = stripe_sdk()
    if sdk is None:
        return None, "Stripe not configured. Set STRIPE_SECRET_KEY."
    price_id_env = "STRIPE_PRICE_ID_PRO" if tier == "pro" else "STRIPE_PRICE_ID_AUTOPILOT"
    price_id = os.environ.get(price_id_env, "").strip()
    if not price_id:
        return None, f"Missing env var: {price_id_env}"
    customer_id = get_or_create_customer(db_conn_factory, user_id, email=email)
    if not customer_id:
        return None, "Could not create or find Stripe customer."

    success_url = os.environ.get(
        "BILLING_SUCCESS_URL",
        f"{base_url.rstrip('/')}/?billing=success",
    )
    cancel_url = os.environ.get(
        "BILLING_CANCEL_URL",
        f"{base_url.rstrip('/')}/?billing=cancel",
    )

    try:
        sess = sdk.checkout.Session.create(
            customer=customer_id,
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=success_url,
            cancel_url=cancel_url,
            metadata={"app_user_id": str(user_id), "tier": tier},
            subscription_data={
                "metadata": {"app_user_id": str(user_id), "tier": tier},
            },
            allow_promotion_codes=True,
            billing_address_collection="auto",
        )
    except Exception as e:
        return None, f"Stripe error: {e}"
    return sess.url, None


def create_portal_session(db_conn_factory, user_id, base_url):
    """Create a Stripe Billing Portal session so the user can manage / cancel
    their subscription. Returns (url, error_message)."""
    sdk = stripe_sdk()
    if sdk is None:
        return None, "Stripe not configured."
    customer_id = get_or_create_customer(db_conn_factory, user_id)
    if not customer_id:
        return None, "No Stripe customer for this user."
    try:
        portal = sdk.billing_portal.Session.create(
            customer=customer_id,
            return_url=f"{base_url.rstrip('/')}/",
        )
    except Exception as e:
        return None, f"Stripe error: {e}"
    return portal.url, None


# ── Webhook handlers ─────────────────────────────────────────────────────────

def apply_subscription_event(db_conn_factory, event):
    """Mutate users.tier / subscription_status / current_period_end based on a
    Stripe event dict. Returns a short string describing what happened (for logs).
    Safe to call with any event type — unknown types are no-ops."""
    etype = event.get("type", "")
    data  = (event.get("data") or {}).get("object") or {}

    if etype in ("customer.subscription.created",
                 "customer.subscription.updated",
                 "customer.subscription.resumed"):
        return _apply_subscription_object(db_conn_factory, data)

    if etype == "customer.subscription.deleted":
        return _downgrade_subscription(db_conn_factory, data)

    if etype == "checkout.session.completed":
        # Subscription mode checkout — the subscription object will arrive in
        # a separate customer.subscription.created event, but we set a
        # provisional tier here so the user sees the upgrade immediately.
        customer_id = data.get("customer")
        meta_tier   = (data.get("metadata") or {}).get("tier")
        if customer_id and meta_tier in ("pro", "autopilot"):
            _set_tier_for_customer(db_conn_factory, customer_id, meta_tier,
                                   status="active")
            return f"checkout.completed → tier={meta_tier}"
        return "checkout.completed (no actionable data)"

    if etype == "invoice.payment_failed":
        customer_id = data.get("customer")
        if customer_id:
            with db_conn_factory() as conn:
                conn.execute(
                    "UPDATE users SET subscription_status = ? "
                    "WHERE stripe_customer_id = ?",
                    ("past_due", customer_id),
                )
            return "invoice.payment_failed → past_due"

    return f"ignored ({etype})"


def _apply_subscription_object(db_conn_factory, sub):
    """Apply a Stripe Subscription object to the matching user row."""
    customer_id = sub.get("customer")
    status      = sub.get("status", "")
    period_end  = sub.get("current_period_end")  # unix timestamp
    items       = (sub.get("items") or {}).get("data") or []
    price_id    = ""
    if items:
        price_id = ((items[0].get("price") or {}).get("id")) or ""

    new_tier = tier_from_price_id(price_id)
    if status in ("canceled", "incomplete_expired", "unpaid"):
        new_tier = "free"

    period_end_iso = ""
    if period_end:
        try:
            period_end_iso = datetime.utcfromtimestamp(int(period_end)).isoformat()
        except Exception:
            period_end_iso = ""

    if not customer_id:
        return "subscription event missing customer"

    with db_conn_factory() as conn:
        conn.execute(
            "UPDATE users SET tier = ?, subscription_status = ?, "
            "stripe_subscription_id = ?, current_period_end = ? "
            "WHERE stripe_customer_id = ?",
            (new_tier, status, sub.get("id", ""), period_end_iso, customer_id),
        )
    return f"subscription → tier={new_tier} status={status}"


def _downgrade_subscription(db_conn_factory, sub):
    customer_id = sub.get("customer")
    if not customer_id:
        return "delete event missing customer"
    with db_conn_factory() as conn:
        conn.execute(
            "UPDATE users SET tier = 'free', subscription_status = 'canceled', "
            "stripe_subscription_id = NULL WHERE stripe_customer_id = ?",
            (customer_id,),
        )
    return "subscription deleted → tier=free"


def _set_tier_for_customer(db_conn_factory, customer_id, tier, status="active"):
    with db_conn_factory() as conn:
        conn.execute(
            "UPDATE users SET tier = ?, subscription_status = ? "
            "WHERE stripe_customer_id = ?",
            (tier, status, customer_id),
        )
