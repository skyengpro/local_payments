# ADR-0003: Integrate Nokash as an aggregator gateway

**Status:** Proposed
**Date:** 2026-11-06

## Context

Nokash is a payment aggregator. In Cameroon, one integration reaches both MTN MoMo and Orange Money,
without a merchant contract with each operator. It works the same way as MTN RequestToPay: a number,
an approval on the phone, a status query. The contract read here is the Nokash sandbox, published on
its website and transcribed in
[`../openAPI/nokash-payin-spec.json`](../openAPI/nokash-payin-spec.json).

Nokash differs from the direct MTN gateway on four points, and two of them touch rules the
application holds as invariants:

| Point | Direct MTN | Nokash | Rule concerned |
| --- | --- | --- | --- |
| Operator | One per gateway | Chosen per payin (`payment_method`) | ARCHITECTURE, scope |
| Status answer | Amount and currency | Amount only | Amount and currency are compared with the session |
| Status authentication | Bearer token and subscription key | None shown in the sandbox | D3, a merchant-authenticated query is the proof |
| Status key | `attempt_id`, chosen by the site | Nokash's own id, returned by the payin | None, but an id lost with the payin answer cannot be recovered |

`lifecycle.resolve()` compares currencies strictly: a missing confirmed currency never matches, so a
Nokash success would always end as `amount_mismatch`.

## Decision

Nokash is a `frappe/payments` gateway like the others: a `Nokash Settings` doctype, a pure client in
`providers/nokash.py`, the shared page, `reconcile()` and scheduler.

- **Operator.** One gateway offers one or both operators, and the payer picks theirs on the page.
  This is not the provider choice ARCHITECTURE excludes, which is a choice between gateways: the
  session stays tied to one gateway.
- **Currency.** Fixed to XAF, the only currency of the Nokash Cameroon offer, with no setting. The
  client reports it as the confirmed currency. `lifecycle.py` stays strict for every provider.
- **Status authentication.** The sandbox call is accepted as it is. The site still calls a configured
  address over TLS, so a third party cannot forge the answer; anyone holding the id can read it.
  Knowing how the call is authenticated in production is a prerequisite for going live.
- **Lost id.** An attempt with no Nokash id cannot be queried. It stays open until its local deadline,
  moves to `Unresolved`, and the managers are alerted at once, even if the session has been paid since.
  No query goes out for it.

## Options considered

### Option A: one gateway, operator on the page, currency fixed in the client (chosen)

| Dimension | Assessment |
| --- | --- |
| Complexity | Medium: one doctype, one client, one choice on the page |
| Effect on `lifecycle.py` | One neutral field on `ProviderResult` |
| Merchant setup | One gateway for both operators |
| Honesty of the data | `confirmed_currency` holds a value Nokash did not state |

**Advantages:** the amount check stays the same for every provider. The consumer offers one payment
method and the payer keeps their choice of operator.

**Drawbacks:** the page gains a provider-specific field. `confirmed_currency` is implied, not
confirmed.

### Option B: a "currency not confirmed" flag in `lifecycle.py`

| Dimension | Assessment |
| --- | --- |
| Complexity | Low in the client, higher in the pure core |
| Effect on `lifecycle.py` | A new branch in the rule that decides a payment is secured |
| Honesty of the data | `confirmed_currency` stays empty |

**Advantages:** the record says exactly what Nokash answered.

**Drawbacks:** it loosens, for all providers, the check that protects against a payment in the wrong
currency, to describe a case with a single possible currency.

### Option C: one gateway per operator

**Advantages:** the page does not change. The session says which operator was used.

**Drawbacks:** every consumer has to offer two gateways for one merchant contract, and the merchant
sets up the same keys twice.

### Option D: stay on direct contracts only

**Advantages:** no aggregator between the site and the operators. D3 holds as it is.

**Drawbacks:** Orange Money stays blocked by its merchant contract, and each new operator needs its
own contract and client.

## Trade-off analysis

The amount check is what stops a wrong payment from being recorded, so it should not bend for one
provider. With a single possible currency, an implied value changes nothing to that protection, and
it can be removed the day Nokash returns a currency. The status query keeps its role as the only proof
of outcome: what is lost in the sandbox is confidentiality of the status, not its authenticity.

## Consequences

- **Simpler:** Orange Money becomes reachable before a contract with Orange, and MTN MoMo needs no
  separate contract.
- **Harder:** the site depends on one more party for the payment's outcome. An attempt whose id was
  lost is settled by a person, with Nokash.
- **Payment Requests:** `Nokash Settings` counts among the application's gateways in step 1 of
  ADR 0002, through the `SETTINGS_DOCTYPES` tuple.
- **To revisit:**
  - when Nokash states how the status call is authenticated in production;
  - when a second currency or country is in scope, since the currency, country and number format are
    constants of `providers/nokash.py`;
  - when the callback is added: its body carries `id` and `orderId`, enough to find a lost id, as long
    as the status query that follows stays the proof. Nokash sends it with production keys only;
  - if Nokash confirms a search by `order_id`, which would resolve lost ids on its own.

## Actions

1. [ ] `providers/nokash.py` and its recorded responses.
2. [ ] `Nokash Settings`, with the operator choice on the page.
3. [ ] Alert at the deadline for an attempt with no id.
4. [ ] Confirm the points listed in [`../gateways/nokash.md`](../gateways/nokash.md) with Nokash
       before production.
5. [ ] Validate the exchange against a real Nokash environment once server-callable keys exist.
