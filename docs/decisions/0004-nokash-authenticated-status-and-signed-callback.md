# ADR-0004: Nokash status through Check Status 611, callback only a signed trigger

**Status:** Proposed
**Date:** 2026-11-17
**Supersedes:** the "Status authentication" decision of [ADR 0003](0003-nokash-aggregator.md)

## Context

ADR 0003 was written from the Nokash sandbox's published contract. It states that the status call
(`/trans/310/status-request`) carries no authentication, and makes how it is authenticated in
production a prerequisite for going live. Nokash has since answered, in three documents and in writing:

| Point | What Nokash says | Source |
| ----- | ---------------- | ------ |
| Status call | `POST /trans/611/status-request`, authenticated by `i_space_key`, as query parameters or `application/x-www-form-urlencoded`. A 403 `Unauthorized` answers a key it does not know and a transaction outside the key's space alike, whether or not the transaction exists. | Check Status 611, Nov. 2026 |
| Old status call | `/trans/310/status-request` answered 403 to every call we made, with or without the key and the integration header. | Our tests, 2026-11-13 |
| Rate | At most one call every 30 seconds per transaction, and none once the status is final. | Check Status 611 |
| Callback | Signed: `hmac-signature: t=<s>,s=<hex>`, HMAC-SHA256 keyed by `i_space_key` over `id:amount:orderId:status:phone`. `t` is informative. Only final statuses are sent. HTTP 200 expected, 401 for a bad signature. | Callback signature, Nov. 2026 |
| Their advice | "Never deliver an order on a callback whose signature you did not verify." For large amounts, confirm with 611. | Callback signature |
| Integration header | `X-NK-Integration` is meant for the **production** keys: any IP accepted, 1,000 XAF cap per transaction, only the integration routes open. Then Nokash declares the server addresses and the same calls go without the header. | Nokash, 2026-11-16 |
| Sandbox | Payins are simulated and complete by themselves. No callback is sent. | Nokash, 2026-11-16 |

Observed on 2026-11-13, before Nokash fixed it on their side: 611 answered 403 for a transaction made
seconds earlier with the same key, and answered the same 403 for a wrong key. A missing key gave a 400
naming the parameter. The answers are JSON (`REQUEST_ERROR`); only a missing header gives a plain-text 403.

Two facts constrain how the key is sent. A status call made with `i_space_key` in the query string would
put the key in the URL of any failed call: the message of a `requests` transport error holds the URL, and
`log_error` writes the traceback, chained exceptions included. And the same key is now the payin credential,
the payin signature key, the callback signature key and the status credential.

## Decision

The status query stays the only proof of a payment (ARCHITECTURE D3), now authenticated.

- **Status.** `check_status` calls 611 with `i_space_key` and `transaction_id` in the form body, never in the
  URL, and drops the transport error's context. The answer must name the transaction (`id`) and the attempt
  (`orderId`) and both must match: an answer that names neither proves nothing. A 403 is a refusal of the key,
  not an outage: the attempt is left as it is, as for any failed check, and the managers are alerted once per
  attempt.
- **Rate.** Each gateway can set `min_check_interval`. Nokash sets 30 seconds. It applies to page polling,
  callbacks and the scheduler alike, since all go through `reconcile()`.
- **Callback.** `nokash_callback` verifies the signature with the key of the attempt's own gateway, and that
  `orderId` and `id` are the attempt's. A call that cannot be verified, an unknown attempt included, gets 401
  and queues nothing. A verified call with a final status and a running attempt queues one `reconcile()`. The
  body decides nothing: 611 does.

## Options considered

### Option A: 611 as the proof, signed callback as a trigger (chosen)

| Dimension | Assessment |
| --------- | ---------- |
| Security | The proof is a merchant-authenticated call. A forged callback cannot queue a check. |
| Cost | One 611 call per verified callback, which Nokash recommends for large amounts anyway. |
| Complexity | One more method on the gateway, one interval attribute, one endpoint check. |

**Advantages:** D3 holds unchanged. A leaked `i_space_key` lets an attacker forge triggers, never a success.
The callback is optional for the outcome: polling and the scheduler still settle it.

**Drawbacks:** the callback can only be tested end to end with production keys and the integration header.

### Option B: trust a verified callback as the proof

**Advantages:** no status call for a delivered callback.

**Drawbacks:** the signature key is also the credential that starts payins and reads statuses, so one leak
would let anyone mark a payment as received. The callback is sent once and Nokash tells us not to rely on it
alone. It breaks D3 and invariant 2.

### Option C: keep 310 unauthenticated

**Drawbacks:** it answers 403 to every call, and ADR 0003 already made it a blocker for production.

### Option D: 611 with the key in the query string

**Advantages:** the shape of Nokash's own example.

**Drawbacks:** the key in the URL of every failed call, and so in the Error Log.

## Trade-off analysis

What decides is that the signature protects who may trigger a check, not what the payment is: the key behind
it is shared by too many operations to be a proof. 611 keeps the property D3 relies on. The form body costs
nothing, since Nokash accepts it, and removes the one new way this change could write a key into a log.

## Consequences

- **Simpler:** no more "status authentication unknown" in the production checklist. The Nokash gateway meets
  D3 as MTN does.
- **Harder:** the callback code can be exercised against Nokash only once there are production keys with the
  integration header, so it is covered by tests built from the specification's rule. A verification bug fails
  safe: the call is refused, and polling and the scheduler still settle the payment. Rejections are the only
  sign of it.
- **Alerts:** a refused key is a configuration error that no retry fixes, so it alerts once per attempt, as a
  refused payin does.
- **To revisit:**
  - a callback could recover a lost transaction id, as it is signed and carries `id` and `orderId`. 611 would
    still confirm it. That is a new decision;
  - a verified callback could skip the interval, since it announces a final status and costs one call;
  - what Nokash answers when the rate is exceeded (not documented), and whether it retries a callback after a
    401 or a 5xx;
  - the integration key can go once Nokash has the server addresses. The settings field then stays empty.

## Actions

1. [x] `check_status` on 611, key in the form body, `id` and `orderId` required, 403 told apart.
2. [x] Signature verification in `nokash_callback`, with tests from the specification's rule.
3. [x] `min_check_interval` per gateway, 30 seconds for Nokash.
4. [ ] Run Check Status 611 against the integration environment: the call was fixed by Nokash on 2026-11-16,
   not yet re-run by us because `api.nokash.app` was unreachable on 2026-11-17.
5. [ ] With production keys and `X-NK-Integration`, make a payment of at most 1,000 XAF through the site, and
   check that the signed callback is accepted and the payment settles through 611.
6. [ ] Ask Nokash: the answer to a call above the rate, the callback retry policy, and the addresses callbacks
   come from.
7. [ ] Send Nokash the server addresses, then remove the integration key from the settings.
