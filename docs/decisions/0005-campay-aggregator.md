# ADR-0005: Integrate CamPay as an aggregator gateway

**Status:** Proposed
**Date:** 2026-11-24

## Context

CamPay is a payment aggregator, like Nokash ([ADR 0003](0003-nokash-aggregator.md)). In Cameroon,
one CamPay application reaches MTN MoMo and Orange Money: the site sends a push to the payer's
number, the payer approves it on their phone, and the site reads the status. CamPay holds the funds.
The contract read here is the official Postman documentation plus the sandbox calls of 2026-11-24,
transcribed in [`../openAPI/campay-collect-spec.json`](../openAPI/campay-collect-spec.json).
Where they disagree, the sandbox wins.

CamPay differs from the gateways already in place on five points:

| Point                 | Nokash                                        | CamPay                                                            | Rule concerned                                  |
| --------------------- | --------------------------------------------- | ----------------------------------------------------------------- | ----------------------------------------------- |
| Operator              | Sent with each payin (`payment_method`)     | Deduced by CamPay from the number                                 | ARCHITECTURE, scope                             |
| Credentials           | Integrator and app keys in the body           | Permanent token in `Authorization: Token`, no expiry              | Secrets in `Password` fields only             |
| Status answer         | Amount, no currency                           | Amount and currency, but `0.00` on a sandbox success              | Amount and currency are compared with the session |
| Callback              | URL sent per payin, HMAC over the body        | One URL per application, set in the dashboard, GET or POST, HS256 JWT that names no transaction, sent once | D3, a merchant-authenticated query is the proof |
| Repeated reference    | Unknown                                       | 429 `DUPLICATE_ATTEMPT` while the first one is pending            | D4, record then act                             |

The sandbox has no way to show a session reaching `Paid`: it reports `amount` `0.00` for every
transaction, and caps amounts at 100 XAF.

## Decision

CamPay is a `frappe/payments` gateway like the others: a `CamPay Settings` doctype, a pure client in
`providers/campay.py`, the shared page, `reconcile()` and the scheduler. `lifecycle.py`, `reconcile.py`,
the scheduler and the ERPNext hook do not change.

- **Credentials.** The permanent access token only. The temporary token flow is not used, so there is
  no token cache. The token is a `Password` field and never leaves the `Authorization` header.
- **Operator.** As with Nokash, the payer picks among the operators the settings accept. The choice is
  not sent: it only checks the number's network before the call. The choice code is shared with Nokash
  through an `OperatorChoice` mixin in `gateway.py`.
- **Proof.** Only `GET /transaction/{reference}/`, authenticated by the token, can settle an attempt.
  Its answer must name the attempt's `reference` and `external_reference`.
- **Webhook.** Accepted in GET and POST. Its JWT is checked by hand with the standard library: HS256
  only, constant-time comparison, `nbf` and `exp` with 60 seconds of leeway, key from the attempt's
  own gateway. A valid token only queues `reconcile()`; the parameters next to it are never read for
  the outcome. An empty `webhook_key` refuses every call.
- **Reference.** `external_reference` is always the `attempt_id` (a UUID4, as CamPay requires) and a
  collect is never sent twice. A lost collect answer leaves the attempt without a reference, so it
  reaches its deadline, becomes `Unresolved`, and the managers are alerted.
- **Amount.** No sandbox exception. A sandbox success ends as `Succeeded` with `amount_mismatch`, and
  the session stays `Open`.

## Options considered

### Option A: a CamPay gateway on the shared flow, webhook as a signed trigger (chosen)

| Dimension                  | Assessment                                                            |
| -------------------------- | --------------------------------------------------------------------- |
| Complexity                 | Low: one doctype, one client, one endpoint, a small extraction        |
| Effect on the pure core    | None in `lifecycle.py`; no new dependency                           |
| Proof of payment           | The authenticated status read, as for every gateway                   |
| Sandbox testing            | No `Paid` session in the sandbox                                    |

**Advantages:** every invariant holds without a CamPay-specific branch. The webhook speeds things up
when it arrives and costs nothing when it is lost.

**Drawbacks:** the sandbox cannot validate a full payment: that needs one small real payment in
production. A JWT replayed within its hour can trigger a status check, bounded by `rate_limit` and
the job's deduplication.

### Option B: a "sandbox" exception to the amount check

| Dimension            | Assessment                                                  |
| -------------------- | ----------------------------------------------------------- |
| Complexity           | Low                                                         |
| Effect on the core   | A branch in the rule that decides a payment is secured      |
| Risk                 | A gateway left on Sandbox by mistake pays sessions for free |

**Advantages:** a sandbox run ends `Paid`, which makes demos easier.

**Drawbacks:** it weakens the one check that stops a wrong payment from being recorded, for a
configuration value a person can get wrong.

### Option C: a JWT library

**Advantages:** less code of our own around the signature.

**Drawbacks:** a new dependency in the pure core for a few dozen lines, and a library may accept
algorithms we never want unless it is configured carefully.

### Option D: trust a verified webhook as the proof

**Advantages:** one call fewer per payment.

**Drawbacks:** the JWT covers no field of the call, so the status, amount and reference beside it
could be anything. It breaks D3.

## Trade-off analysis

The deciding point is that CamPay's signature proves where a call comes from but not what it says.
That leaves the status read as the only possible proof, which is what the shared flow already does.
The sandbox's `0.00` amounts make Option B tempting, but the cost of a wrong `Sandbox` setting in
production is a paid order with no money behind it, which no demo is worth.

## Consequences

- **Simpler:** a second aggregator with no change to the core. The operator choice now lives in one
  place for Nokash and CamPay.
- **Harder:** a session cannot reach `Paid` in the sandbox, so end-to-end validation needs production.
  The webhook is sent once: the scheduler's polling must stay as frequent as it is.
- **Payment Requests:** `CamPay Settings` counts among the application's gateways in step 1 of
  ADR 0002, through the `SETTINGS_DOCTYPES` tuple.
- **To revisit:**
  - if a production `SUCCESSFUL` also reports `0.00`: no CamPay session could ever be paid, and the
    gateway must not be opened to customers;
  - when CamPay states the HTTP code of a refused token, the status rate limit, and the production
    amount limits;
  - if a real webhook delivery carries other claims than the status answer's `signature`;
  - if CamPay documents a lookup by `external_reference`, which would recover lost references.

## Actions

1. [ ] `providers/campay.py` and its recorded responses.
2. [ ] `CamPay Settings`, `campay_callback`, and the `OperatorChoice` extraction.
3. [ ] Sandbox run with the four test numbers: MTN success ends `Succeeded` with `amount_mismatch`,
       failures end `Failed`, a webhook queues `reconcile()`.
4. [ ] One small real payment in production before enabling the gateway for customers.
5. [ ] Ask CamPay the open questions listed in [`../gateways/campay.md`](../gateways/campay.md).
