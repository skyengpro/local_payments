# ADR-0002: Finalize Payment Requests via an `on_payment_authorized` hook

**Status:** Accepted
**Date:** 2026-09-11
**Updated:** 2026-10-02 (user context, cancellation, guards)

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
the missing handling without modifying ERPNext. Each handler is called as
`handler(doc, method, *args)`, and a `commit()` inside it is ignored
(frappe 16.31.0, `model/document.py` `Document.hook`).

`set_as_paid()` checks the current user's rights along the way. In erpnext
16.32.3, `get_party_account()` refuses an account the user cannot read
(`accounts/party.py`, `account_perm_check`), and `get_reference_details()`
checks read access to the referenced document
(`payment_entry/payment_entry.py`). The authorization step runs as Guest
when the payer's page polls the status, and as the user who queued the job
for the MTN callback. An integration test confirmed it: as Guest,
`set_as_paid()` fails with "User don't have permissions to select/read this
account."; as Administrator, it books the Payment Entry.

When a Payment Request is cancelled, ERPNext's own `on_cancel` runs first;
our handler runs after it, in the same transaction, so an error from it
undoes the whole cancellation.

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
   MoMo Settings`. Other gateways' requests stay unlocked;
2. does nothing if `status` is not `Authorized` or `Completed`, or if no
   `Local Payment` session of this request is `Paid`: only a payment the
   provider confirmed to the merchant settles it, whoever calls the method;
3. re-reads the Payment Request's `docstatus` and `status` from the
   database, with a lock (`for_update`), and does nothing if it is already
   `Paid`. A request that is not submitted (`docstatus != 1`) raises an
   error: the authorization moves to `Failed` and the managers are told
   through the usual retry and alert path;
4. calls `set_as_paid()` on the request loaded again after the lock, not on
   the `doc` it received, which may be stale. The call runs as
   Administrator. The caller's user and request state (session, form data,
   caches, and the `ignore_account_permission` flag that ERPNext sets and
   never resets) are put back as they were afterwards, even on error.

The hook has no `try/except` and no `commit`. Errors go up to
`reconcile.authorize()`, which owns the transaction, the retry counter and
the alert. The payer's status endpoint drops every message raised while it
checks the payment, the hook's included, so ERPNext's text never reaches the
payer's page.

`void_open_sessions`, on the request's `on_cancel`, locks the sessions that
reference it, by name and without waiting. The cancel already holds the
request's row and an authorization locks its session before the request, so
waiting could deadlock. Then it:

- refuses the cancellation at once if a session is locked by a running
  authorization ("try again in a moment");
- refuses the cancellation if one of them is `Paid` with an authorization
  other than `Done`. The message names the session to retry first;
- moves the `Open` ones to `Void`. Their checkout page no longer accepts a
  payment.

A payment the provider confirms later on a `Void` session leaves it `Void`
and alerts the managers (`void_paid`).

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

### User context for `set_as_paid()`

| Option | Assessment |
| --- | --- |
| Run it as Administrator, after the guards (chosen) | The request is settled on the first trigger, from the page or the callback |
| Leave it to the scheduler, which runs as Administrator | No elevation, but every payment from the page first ends `Failed`, waits about an hour, uses one try and writes an Error Log |

**Advantages of elevating:** the payer sees a settled request right away, and
the managers are only alerted for real failures. The elevated code is the
same one the scheduler already runs. The payer only supplies a token, and
the hook is reached only after the provider has confirmed the payment to the
merchant's credentials (ARCHITECTURE, D3).

**Drawbacks of elevating:** other applications' handlers on Payment Entry or
Sales Invoice also run as Administrator, as they do from the scheduler. The
caller's state must be put back exactly as it was: `frappe.set_user()` also
replaces the session, the form data and the caches.

### Cancelling a request with money in play

| Option | Assessment |
| --- | --- |
| Refuse the cancel while a session is `Paid` and not `Done` (chosen) | No payment ends up on a cancelled request without an entry |
| Allow it and alert | The money is received, the request is cancelled, nothing in the books |

**Advantages of refusing:** the merchant books the payment before cancelling,
and the message names the session to retry.

**Drawbacks of refusing:** until the "Retry authorization" button exists on
the `Local Payment` form, a merchant blocked by repeated failures waits for
the scheduler's retry or uses the console.

## Trade-off analysis

Option A delegates the accounting entry to ERPNext and limits the side
effect to the application's gateways. Option B fixes a broader problem, but
goes beyond scope and creates a risk of conflict between applications.
Options C and D shift the cost onto each site or onto long-term
maintenance. Elevating to Administrator, limited to `set_as_paid()` and
reached only after a payment the merchant's own query confirmed, costs less
than turning every payment from the page into a failure and a delayed retry.

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
- **User context:** the Payment Entry is created by Administrator, whoever
  triggered the authorization.
- **Cancellation:** a request whose payment is received but not booked
  cannot be cancelled. A payment confirmed after the cancellation leaves the
  session `Void` and alerts the managers, who handle it by hand.
- **To revisit:** once frappe/payments#204 is closed, keep the guard, then
  remove the hook if the upstream handling covers the same cases. If ERPNext
  stops checking the user's rights in `set_as_paid()`, drop the elevation.

## Actions

1. [x] `local_payments/erpnext.py`: `on_payment_authorized` and
       `void_open_sessions`.
2. [x] Integration test: Payment Request paid, Payment Entry submitted,
       status `Paid`, as Guest and as Administrator.
3. [x] Integration test: second trigger without a second Payment Entry.
4. [x] Integration test: simulated upstream handling, no double entry.
5. [x] Integration test: Payment Request cancelled, session moved to
       `Void`, new attempt refused.
6. [x] Integration test: cancellation refused while a session is `Paid`
       and not `Done`.
7. [ ] "Retry authorization" button on the `Local Payment` form, so a
       merchant blocked by the cancellation guard does not need the console.
