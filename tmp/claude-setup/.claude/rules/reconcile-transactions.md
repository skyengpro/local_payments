---
paths:
  - "local_payments/reconcile.py"
  - "local_payments/scheduler.py"
  - "local_payments/erpnext.py"
---

# Reconciliation, scheduler, ERPNext hook

- Transaction order (ARCHITECTURE D4): commit session `Paid` first; run
  `on_payment_authorized` second, under a row lock on the session
  (`frappe.get_doc("Local Payment", name, for_update=True)`); a second trigger must find
  `authorization = Done` and do nothing.
- On authorization failure: roll back the consumer's effects only, set `authorization = Failed`
  with the error, let the scheduler retry with spacing. Never revert `Paid`.
- `reconcile()` records only what the authenticated status response contains. A trigger's payload
  is never a source of truth.
- Scheduler queries are bounded (batch size) and use indexed columns (`next_check_on`).
  `Unresolved` attempts are polled at decreasing frequency for 72 h, then alert
  `Local Payments Manager`; a late success is still applied.
- `erpnext.py` hook (ADR 0002), in this order: skip if the gateway is not ours; skip unless status
  is `Authorized` or `Completed`; re-read the Payment Request with `for_update` and skip if already
  `Paid`; then `doc.set_as_paid()`. Inactive when ERPNext is not installed.
- Check `set_as_paid()` and `Payment Request` behaviour in the installed erpnext source for both v15
  and v16 before changing any of this.
