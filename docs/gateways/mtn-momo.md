# MTN MoMo Gateway

This document describes the integration of MTN MoMo into `local_payments`:
configuration, operations called, status mapping, and rules specific to this
provider. Rules common to both gateways (session, attempt, reconciliation,
consumer callback) are in [`../ARCHITECTURE.md`](../ARCHITECTURE.md).

## Product

MTN MoMo Open API, Collection product, RequestToPay operation. The site sends
a payment request to the payer's MoMo account, identified by their MSISDN
(Mobile Station International Subscriber Directory Number). The payer
approves the request on their phone. The request stays pending until it is
approved, rejected, or expired by MTN.

MTN does not host a payment page. Entering the number and waiting happen on
the local `local_payment_checkout` page, shared by both gateways. The
contract seen by consumers is therefore identical to Orange Money's.

## Merchant prerequisites

- An account on the MTN MoMo developer portal, subscribed to the Collection
  product, for sandbox.
- Going to production with MTN Mobile Money Cameroon. The terms of use
  specific to Cameroon are published on the portal; the contact is
  `MoMoCorporate.CM@mtn.com`.
- After going to production, access to the MTN merchant portal. Production
  API keys are created there and the site's callback host is registered
  there. HTTPS is mandatory in production.

## Configuration

Doctype `MTN MoMo Settings`, not Single, named by `gateway_name`. Only
System Manager can read or edit it.

| Field | Type | Required | Role |
| --- | --- | --- | --- |
| `gateway_name` | Data | yes | Contract name. Gives its name to the document and to the `MTN MoMo-<gateway_name>` gateway. A change after the document is created is refused. |
| `enabled` | Check | | A disabled gateway refuses `get_payment_url()`. Unchecked by default. |
| `environment` | Select: Sandbox, Production | yes | Defaults to Sandbox. |
| `api_base_url` | Data | yes | Sandbox: `https://sandbox.momodeveloper.mtn.com` (the default). Production: URL provided by MTN. Must start with `https://`. With Sandbox, only the sandbox host is accepted, since EUR answers are then read as the contract's currency. |
| `target_environment` | Data | yes | Value of the `X-Target-Environment` header: `sandbox` (the default), or the production value provided by MTN. |
| `currency` | Link: Currency | yes | Accepted currency: `XAF` in Cameroon (the default). |
| `subscription_key` | Password | yes | Subscription key for the Collection product (`Ocp-Apim-Subscription-Key`). |
| `api_user` | Data | yes | API user identifier. |
| `api_key` | Password | yes | API user key. |
| `msisdn_prefix` | Data | yes | Country code without `+`: `237` for Cameroon. Digits only. |
| `msisdn_national_length` | Int | yes | Length of the national number: 9 in Cameroon. Greater than zero. |
| `pending_timeout_minutes` | Int | | Local delay before `Unresolved`. Defaults to 15, also when left empty or set to 0. Cannot be negative. |
| `payer_message` | Data | | Message shown to the payer. At most 160 characters. |
| `payee_note` | Data | | Merchant-side note. At most 160 characters. |
| `send_callback` | Check | | Sends `X-Callback-Url`. Checked by default. |

Saving the document creates the `MTN MoMo-<gateway_name>` gateway if it does
not exist yet, and announces it with `payment_gateway_enabled` when `enabled`
is checked. `create_payment_gateway` never renames an existing gateway, so
`gateway_name` is fixed once the document exists: renaming it would leave
the old gateway, and the sessions that point to it, without settings.

## Operations

| Operation | Request | Useful response |
| --- | --- | --- |
| Access token | `POST {api_base_url}/collection/token/`, headers `Authorization: Basic base64(api_user:api_key)` and `Ocp-Apim-Subscription-Key` | `access_token`, `expires_in` |
| RequestToPay | `POST {api_base_url}/collection/v1_0/requesttopay`. Headers: Bearer token, `X-Reference-Id` (the `attempt_id`, UUID v4), `X-Target-Environment`, `X-Callback-Url` (optional), `Ocp-Apim-Subscription-Key`. Body: `amount`, `currency`, `externalId`, `payer` (`partyIdType: MSISDN`, `partyId`), `payerMessage`, `payeeNote` | `202 Accepted`, no body |
| Status | `GET {api_base_url}/collection/v1_0/requesttopay/{attempt_id}`, same headers without `X-Callback-Url` | `amount`, `currency`, `externalId`, `payer`, `financialTransactionId`, `status`, `reason` |
| Incoming callback | `PUT` or `POST` on `X-Callback-Url`, sent once | Serves only as a trigger |

Every call, the token included, carries `Ocp-Apim-Subscription-Key`. The
full contract is MTN's Collection OpenAPI, kept in
[`../openAPI/collection.json`](../openAPI/collection.json). The client is
`local_payments/providers/mtn_momo.py`. It reports what MTN answered and
leaves the attempt status to its caller.

## Status mapping

| MTN response | Attempt status | Note |
| --- | --- | --- |
| `202` on RequestToPay | `Initiated` | Request accepted, queued at MTN. |
| Timeout or `5xx` error on RequestToPay | `Initiated` | The identifier is known before the call: the status remains queryable. |
| No token could be obtained before RequestToPay | `Error` | The request never left. The payer can try again. |
| `PENDING` | `Pending` | Waiting for the payer's approval. |
| `SUCCESSFUL` | `Succeeded` | Amount and currency compared to the session. |
| `FAILED` | `Failed` | Whatever the reason. `provider_status` keeps it, e.g. `FAILED: NOT_ENOUGH_FUNDS`. |
| `404` on status | `Failed` | The request was not created. `provider_status` is `404: RESOURCE_NOT_FOUND`. |
| `PENDING` after `pending_timeout_minutes` | `Unresolved` | Queried thereafter at decreasing frequency. |
| Any other `status` value, or a body that is not JSON | No change | Reported as an error, logged, queried again later. |

`reason` is read both as a plain code (`"NOT_ENOUGH_FUNDS"`) and as an
object (`{"code": ..., "message": ...}`): MTN documents both. The code is
only copied into `provider_status`, never used to decide anything.

MTN writes nothing in the attempt's `provider_data`: the `attempt_id` is the
only reference it needs.

## Flow

```mermaid
sequenceDiagram
    autonumber
    participant C as Consumer
    participant S as local_payments
    participant P as Payer - browser
    participant T as Payer - phone
    participant MTN as MTN MoMo API

    C->>S: get_payment_url
    S->>S: creates Local Payment, status Open
    S-->>C: local_payment_checkout URL
    P->>S: GET local_payment_checkout
    S-->>P: summary and number field
    P->>S: POST start_attempt with the number
    S->>S: number validated, attempt_id UUID v4, attempt Initiated
    S->>MTN: POST requesttopay with X-Reference-Id
    MTN-->>S: 202 Accepted
    S-->>P: waiting state
    MTN->>T: payment request
    T->>MTN: approval with PIN code
    loop every 5 seconds
        P->>S: get_status
        S->>MTN: GET requesttopay/attempt_id
        MTN-->>S: PENDING, then SUCCESSFUL
    end
    opt sent once, may be missed
        MTN->>S: callback
        S->>S: reconcile queued
    end
    S->>S: amount and currency match, session Paid, then on_payment_authorized
    S-->>P: redirect to payment-success
```

## Rules specific to MTN MoMo

- **`X-Reference-Id`.** It receives the `attempt_id`, a UUID v4 generated
  before the call. MTN refuses an identifier that has already been used
  (error 409). If initiation gets no response, a retry with the same
  identifier therefore cannot create a second request. A `409` means the
  request exists, and the attempt moves to status polling.
- **`externalId`.** The name of the `Local Payment` session.
- **Currency.** It must match the target environment. The sandbox only
  accepts `EUR`, so when `environment` is Sandbox the request goes out in
  `EUR` and an `EUR` answer is read back as the contract's currency. In
  production nothing is translated: the currency MTN returns is compared as
  is. The session's amount does not change.
- **Amount.** Sent as a string, an integer in XAF. A fraction is refused
  before any call, never rounded. The amount and currency returned by the
  status operation are compared to those of the session.
- **Texts.** `payerMessage` and `payeeNote` are limited to 160 characters.
  MTN does not publish the characters it refuses, so accents are dropped
  and anything other than letters, digits, spaces and `. , - _ : /` becomes
  a space (the apostrophe included).
- **MSISDN.** Digits only: country code, then national number, with no `+`
  or `00`. A number outside the format is rejected before any call.
- **Callback.** MTN sends it once and does not resend it if there is no
  response. It carries no secret. The URL contains the `attempt_id`. The
  callback triggers a query, under `rate_limit` and within the minimum
  interval per attempt.
- **Waiting on the page.** The page calls `get_status()` every 5 seconds. The
  server queries MTN only once per 5-second interval and per attempt,
  regardless of how many tabs are open.
- **One request at a time.** A new request is only possible after the
  previous one has moved to a final state or to `Unresolved`. A late success
  of an `Unresolved` request on an already-paid session is logged as a
  duplicate.
- **Access token.** Cached per settings document until `expires_in` minus 60
  seconds. Renewed on a 401 response, with a single retry.

## Known errors

| HTTP code | Meaning | Handling |
| --- | --- | --- |
| `400` | Non-compliant request: headers, invalid UUID, refused characters, currency different from the target environment, text over 160 characters | Attempt `Error`. Alert if the cause is configuration. |
| `401` | Invalid subscription key or token | Token renewal, then a single retry. Otherwise attempt `Error` and alert. |
| `404` on status | Unknown reference: the request did not go through | Attempt `Failed`. |
| `409` | `X-Reference-Id` already used | Move to status polling. |
| `5xx` or timeout | Unavailability | Attempt `Initiated`, query scheduled. |
| `400`, `401`, `5xx` or timeout on status | The query failed, not the payment | No change to the attempt. The error goes to the Error Log and the attempt is queried again later. |

## Points to confirm before production

| Point | Assumption made | Effect if the assumption is wrong |
| --- | --- | --- |
| Delay after which MTN expires a pending request | 15 minutes. MTN's global error page mentions a 5-minute approval window; not confirmed for Cameroon | Adjust `pending_timeout_minutes`. A value too short produces `Unresolved` states, never a lost payment. |
| Status after the payer's rejection or a wrong PIN | `FAILED` | If the request stays `PENDING`, it moves to `Unresolved` when the local delay expires. |
| Base URL and value of `X-Target-Environment` in production in Cameroon | Provided by MTN at go-live | Plain configuration values. |
| Fields of the status response | As in the Collection OpenAPI: `amount`, `currency`, `financialTransactionId`, `status`, `reason`. Its examples send some of them as numbers, so all are read as text | Adapt normalization in `providers/mtn_momo.py`. To be validated in sandbox. |
| Format of `reason` returned in Cameroon | Plain code or `{code, message}`, both accepted | None on the outcome: the reason is only displayed. |
| Characters MTN refuses in `payerMessage` and `payeeNote` | Anything outside letters, digits, spaces and `. , - _ : /` | A wider list only makes the texts less faithful. A narrower one gives a `400` on RequestToPay. |
| Test responses | Rebuilt from the OpenAPI examples (`local_payments/tests/fixtures/mtn_momo.json`) | Replace them with sandbox captures, token and numbers removed. |
| Minimum and maximum amounts per transaction | None | Add `validate_minimum_transaction_amount()` and a maximum rejection. |
| Prior verification of the payer's account | Not used | Possible UX improvement: immediate rejection of a number with no MoMo account. |

## References

No public technical documentation specific to Cameroon was found. The
scope column says which country each source covers.

| Source | Nature | Scope | Content used |
| --- | --- | --- | --- |
| Collection OpenAPI, exported from the developer portal — [`../openAPI/collection.json`](../openAPI/collection.json) | Official | All countries | Operations, headers, schemas, HTTP codes, `status` values, `ErrorReason` codes. |
| Sandbox provisioning OpenAPI — [`../openAPI/sandbox-provisioning-api.json`](../openAPI/sandbox-provisioning-api.json) | Official | Sandbox | Creating a sandbox API user and key. Not used by the client. |
| MTN MoMo Developer, "Testing" — `momodeveloper.mtn.com/api-documentation/testing` | Official | Sandbox, all countries | Sandbox target environment is `sandbox`, its only currency is `EUR`. |
| MTN MoMo Developer, "Common error" — `momodeveloper.mtn.com/api-documentation/common-error` | Official | All countries | Codes `400` and `401`. `COULD_NOT_PERFORM_TRANSACTION` after a 5-minute approval window. |
| MTN MoMo Developer, "Callback" documentation — `momodeveloper.mtn.com/api-documentation/callback` | Official | All countries | Asynchronous processing, `202` response, callback sent once, status polling as fallback, HTTPS and host registration in production. |
| MoMo Dev Community, "Reason for some FAILED API responses updated", 2024-01-30 | Official | All countries, example in UGX (Uganda) | `reason` as a plain code. |
| MTN MoMo Developer, API terms of use for Cameroon — `momodeveloper.mtn.com/Cameroon_apitermsandcondition` | Official | Cameroon | Availability in Cameroon, contact for MTN Mobile Money Cameroon. No technical content. |

The Rwandan partner portal (`momoapi.mtn.co.rw`) cited earlier no longer
answers (404 in September 2026) and is no longer used.
