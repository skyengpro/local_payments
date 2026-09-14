# ADR-0001: Integrate providers via the native `frappe/payments` contract

**Status:** Accepted
**Date:** 2026-09-11

## Context

`local_payments` must install on any Frappe site and be usable there the same
way, regardless of which document requests the payment.

Three consumers of `frappe/payments` gateways are already in service:

| Consumer | Call to the gateway | Expected callback |
| --- | --- | --- |
| `Payment Request` (ERPNext) | `get_payment_url()` in `before_submit`. With `payment_channel = "Phone"`, `request_phone_payment()` instead. | `on_payment_authorized` on the Payment Request |
| Web Form (override provided by `frappe/payments`) | `get_payment_url()` | `on_payment_authorized` on the submitted document |
| LMS | `get_payment_url()` | `on_payment_authorized` on `LMS Course` or `LMS Batch` |

The "Phone" channel only exists in ERPNext: `Payment Request` and `POS
Invoice`. The only contract common to all consumers is `get_payment_url()`
on the way out and `on_payment_authorized()` on the way back.

The two providers do not work the same way:

- **Orange Money Web Payment** redirects the payer to a page hosted by Orange.
- **MTN MoMo RequestToPay** sends a request to the payer's phone. It requires
  their number and MTN hosts no page.

In `frappe/payments`, `get_payment_url()` does not necessarily return a
provider page. Stripe, Braintree, Razorpay, Paytm, and GoCardless return a
local page (`templates/pages/<gateway>_checkout`), on which the payment takes
place.

## Decision

Both Orange Money and MTN MoMo implement the native contract.
`get_payment_url()` returns the URL of a single local page,
`local_payment_checkout`. For Orange Money, this page redirects to Orange.
For MTN MoMo, it collects the payer's number, triggers the request, and
waits for its result. No standalone API is exposed to consumers, and
ERPNext's "Phone" channel is not used.

## Options considered

### Option A: native contract and shared local page (chosen)

| Dimension | Assessment |
| --- | --- |
| Complexity | Medium: one page, two HTTP clients |
| Consumers covered | All: Payment Request, Web Form, LMS, business doctypes |
| Uniformity between providers | Total on the consumer side; only the page differs |
| Maintenance | Follows the evolution of `frappe/payments` |

**Advantages:** no consumer to adapt. A single confirmation path for both
providers. A third provider, whether redirect-based or push-based, can be
added without changing the contract.

**Drawbacks:** the MTN payment page (input, waiting, errors) has to be built
and maintained.

### Option B: standalone API and dedicated reference doctype

The application exposes its own initiation method, and `Local Payment`
becomes the `reference_doctype` of each payment.

| Dimension | Assessment |
| --- | --- |
| Complexity | Low for the first caller |
| Consumers covered | Only code written against this API |
| Uniformity between providers | Good |
| Maintenance | A second gateway registry, parallel to that of `frappe/payments` |

**Advantages:** independence from `Payment Request`'s flaws.

**Drawbacks:** consumers cannot call a gateway that is not registered in
`frappe/payments`, so neither ERPNext, nor Web Forms, nor LMS can offer
Orange Money or MTN MoMo. This contradicts the goal.

### Option C: ERPNext's "Phone" channel for MTN MoMo

MTN MoMo follows the M-Pesa pattern: `request_for_payment()` called by
`Payment Request.request_phone_payment()`.

| Dimension | Assessment |
| --- | --- |
| Complexity | Low within ERPNext |
| Consumers covered | Payment Request and POS Invoice only |
| Uniformity between providers | None: two contracts for two providers |
| Maintenance | Depends on a path specific to ERPNext |

**Advantages:** the number comes from the ERPNext document, with no page to
build.

**Drawbacks:** MTN MoMo is absent from sites without ERPNext, from Web
Forms, and from LMS. Behavior differs by provider.

### Option D: fork of `frappe/payments`

| Dimension | Assessment |
| --- | --- |
| Complexity | High over time |
| Consumers covered | All |
| Maintenance | A second repository to maintain against upstream |

**Advantages:** none.

**Drawbacks:** the necessary extension point already exists. The fork would
forfeit upstream fixes on the other gateways.

## Trade-off analysis

The uniformity goal is the deciding factor between options. Only option A
makes both providers available everywhere a gateway already is. Its cost is
a local payment page, already present in most `frappe/payments` gateways.
Options B and C are simpler for a first use case, but each excludes part of
the consumers.

## Consequences

- **Simpler:** every `frappe/payments` consumer offers both payment methods
  as soon as the gateway is configured. Integration tests are done once per
  consumer, not once per consumer-provider pair.
- **Harder:** the application carries the full state of a payment (session,
  attempts, retry), since consumers only expect a callback. The MTN page
  requires particular attention to the waiting experience.
- **To revisit:** ERPNext's `develop` branch already calls a new gateway
  interface (`payments.controllers.PaymentController`, with a `Payment
  Session Log`), guarded by a fallback to the current interface. No branch of
  `frappe/payments` ships it to date. Once it is published, `Local Payment`
  will correspond to its `Payment Session Log`, and the port will be limited
  to the adapters.

## Actions

1. [ ] `gateway.py`: `validate_transaction_currency()`, `get_payment_url()`,
       session creation and reuse.
2. [ ] `local_payment_checkout` page: Orange Money variant (redirect) and
       MTN MoMo variant (number, waiting, errors).
3. [ ] Integration tests, one per consumer: Payment Request, Web Form, LMS.
4. [ ] Watch `frappe/payments` for the publication of `PaymentController`.
