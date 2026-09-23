---
paths:
  - "local_payments/api.py"
  - "local_payments/templates/pages/**"
  - "local_payments/www/**"
---

# Guest-facing surface (checkout page and endpoints)

- Endpoints: `@frappe.whitelist(allow_guest=True)` plus `rate_limit`
  (`frappe.rate_limiter.rate_limit`). Session access by `token` only; never return or accept the
  sequential `LPAY-...` name.
- GET has no side effect. `start_attempt` (POST) validates the MSISDN against the settings prefix
  and length **before** any outbound call, and refuses a session that is `Paid` or `Void`, or that
  already has an `Initiated`/`Pending` attempt.
- `get_status` triggers `reconcile()` only if the minimum interval per attempt has elapsed,
  whatever the number of open tabs.
- The page shows only title, amount, currency and provider. Errors shown to the payer are generic;
  detail goes to the error log without secrets or the full MSISDN.
- Every payer-facing string is translatable and translated in French and English.
- The callback endpoint answers fast, enqueues `reconcile()`, and ignores an unknown attempt
  without any outbound call.
