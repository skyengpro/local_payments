# ADR-0002: Finalize Payment Requests via an `on_payment_authorized` hook

**Status:** Accepted
**Date:** 2026-09-11

## Context

All `frappe/payments` gateways call `run_method("on_payment_authorized",
status)` on the reference document. ERPNext's `Payment Request` does not
implement this method, neither in v15, nor in v16, nor on the `develop`
branch (frappe/payments#204, open). A successful payment therefore leaves the
Payment Request in the `Requested` state, with no Payment Entry.

`Payment Request` does know how to settle itself, though: `set_as_paid()`
creates and submits the Payment Entry from the `Payment Gateway Account`.
For a Payment Request on the `Phone` channel, the method simply marks it
paid.

`Document.run_method()` runs the controller's method if it exists, then the
`doc_events` declared by installed applications for that event, then the "On
Payment Authorization" Server Scripts. An application can therefore supply
the missing handling without modifying ERPNext.

## Decision

`local_payments` declares in `hooks.py`:

```python
doc_events = {
    "Payment Request": {
        "on_payment_authorized": "local_payments.erpnext.on_payment_authorized",
        "on_cancel": "local_payments.erpnext.void_open_sessions",
    }
}
```

`on_payment_authorized(doc, method, status)`:

1. does nothing if `doc.payment_gateway` is not a `local_payments` gateway,
   i.e. if `gateway_settings` is neither `Orange Money Settings` nor `MTN
   MoMo Settings`;
2. does nothing if `status` is not `Authorized` or `Completed`;
3. re-reads the Payment Request's status from the database, with a lock
   (`for_update`), and does nothing if it is already `Paid`;
4. calls `doc.set_as_paid()`.

`void_open_sessions` moves `Open` sessions that reference the cancelled
Payment Request to `Void`.

The hook is inactive on a site without ERPNext, for lack of a `Payment
Request` doctype.

## Options considered

### Option A: hook limited to `local_payments` gateways (chosen)

| Dimension | Assessment |
| --- | --- |
| Complexity | Low: one function, one guard |
| Effect on the site | Only Orange Money and MTN MoMo payments |
| Accounting entries | Made by ERPNext (`set_as_paid`) |
| Risk from version upgrades | Managed by the status re-read |

**Advantages:** the Payment Entry follows ERPNext's rules (gateway account,
accounting dimensions, currency, the original document's advance status). No
effect on the site's other gateways.

**Drawbacks:** Stripe, PayPal, and the other gateways keep bug #204 on the
same site.

### Option B: hook for all gateways

| Dimension | Assessment |
| --- | --- |
| Complexity | Low |
| Effect on the site | All gateways |

**Advantages:** fixes #204 for the whole site.

**Drawbacks:** changes the behavior of gateways outside the application's
scope. May conflict with another application fixing the same bug.

### Option C: "On Payment Authorization" Server Script

**Advantages:** no application code.

**Drawbacks:** configuration specific to each site, not versioned, to be
recreated on every installation. Contrary to the uniformity goal.

### Option D: create the Payment Entry inside `local_payments`

**Advantages:** full control over the entry.

**Drawbacks:** duplicates ERPNext's logic (accounts, dimensions, exchange,
advance status) and drifts from it with every ERPNext change. The
application would become a second source of accounting entries.

### Option E: do not finalize the Payment Request

**Advantages:** no coupling with ERPNext.

**Drawbacks:** every payment received must be reconciled by hand. The main
intended use case, paying ERPNext invoices and orders, loses its point.

## Trade-off analysis

Option A delegates the accounting entry to ERPNext and limits the side
effect to the application's gateways. Option B fixes a broader problem, but
goes beyond scope and creates a risk of conflict between applications.
Options C and D shift the cost onto each site or onto long-term
maintenance.

## Consequences

- **Simpler:** a Payment Request paid via Orange Money or MTN MoMo moves to
  `Paid` with its Payment Entry, with no intervention.
- **Harder:** the hook runs within the authorization step (ARCHITECTURE,
  D4). If `set_as_paid()` fails (closed period, missing account), the
  authorization moves to `Failed` and the scheduler retries it. The payment
  stays recorded on the session.
- **Traceability:** the Payment Entry takes the Payment Request's name as
  `reference_no`, as ERPNext does. The provider's transaction id is found on
  the `Local Payment` session that references this Payment Request.
- **Version upgrade:** if ERPNext ever adds `on_payment_authorized` to
  `Payment Request`, the controller's method will run before the hook. The
  database status re-read (step 3) then prevents a second Payment Entry. An
  integration test simulates this case.
- **To revisit:** once frappe/payments#204 is closed, keep the guard, then
  remove the hook if the upstream handling covers the same cases.

## Actions

1. [ ] `local_payments/erpnext.py`: `on_payment_authorized` and
       `void_open_sessions`.
2. [ ] Integration test: Payment Request paid, Payment Entry submitted,
       status `Paid`.
3. [ ] Integration test: second trigger without a second Payment Entry.
4. [ ] Integration test: simulated upstream handling, no double entry.
5. [ ] Integration test: Payment Request cancelled, session moved to
       `Void`, new attempt refused.
