
# Orange Money Gateway

Orange Money is not yet a `local_payments` gateway. This document gathers
what is established about Orange Money collection in Cameroon, what remains
to be obtained, and the points of the common model the integration will
rely on. The rules common to all gateways (session, attempt, reconciliation,
consumer callback) are in [`../ARCHITECTURE.md`](../ARCHITECTURE.md).

## Status

Implementation is waiting on two things: the merchant contract with Orange
Cameroon, and the technical documentation delivered when that contract is
activated. The endpoints, schemas, and error codes published by third
parties are not enough to write a client whose behavior can be guaranteed
in production, and Orange Money in Cameroon has recently forced a change of
credentials and endpoints on its integrators.

## Collection modes

Orange offers two families of API for merchant collection.

**Local/USSD.** The merchant initiates the request via API with the
payer's number. The payer receives an Orange Money notification on their
phone and approves it with their PIN. Neither a browser nor an app is
needed. This is the mode chosen first: it has the same shape as MTN
RequestToPay (initiation with a number, waiting for an approval on the
phone, confirmation by status polling), so the payment page, the state
machine, and the reconciliation already written for MTN cover it without
duplicating them.

**Web Payment, "Group" offer.** The merchant initiates via API and receives
a payment URL. The payer is redirected to a page hosted by Orange,
generates a temporary code through the Orange Money USSD service, then
enters it on that page. This mode requires a payer in front of a browser
and has no equivalent at MTN. It is deferred; it remains open as a second
mode once Local/USSD is delivered.

## Merchant prerequisites

- An Orange Money contract with Orange Cameroon. The request is made to
  the sales department (`Partenariat.om@orange.com`), with the documents
  listed on Orange Cameroon's partner page.
- An application on the Orange Developer portal, subscribed to the chosen
  collection API. It provides `client_id` and `client_secret`.
- A merchant key (`merchant_key`), delivered when the contract is
  activated.

The contracting process is long and independent of development. Starting
it early avoids a dead period once the rest is ready.

## What must be obtained before implementing

| Point                                                                           | What depends on it                                                                           |
| ------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| Local/USSD API endpoints and schemas                                            | All of`providers/orange_money.py`.                                                         |
| Accepted format for the transmitted transaction identifier (length, characters) | The`attempt_id` generator for this gateway.                                                |
| Vocabulary of statuses returned by the status query                             | The mapping to attempt states.                                                               |
| Fields present in the status response, amount and currency included             | The amount compliance check, which currently rejects a mismatch.                             |
| Expiry delay of a pending request                                               | The local delay before`Unresolved`.                                                        |
| Behavior after the payer's rejection or a wrong PIN                             | The distinction between`Failed` and `Unresolved`.                                        |
| Currency code used in sandbox                                                   | Normalization in the client.                                                                 |
| Minimum and maximum amounts per transaction                                     | `validate_minimum_transaction_amount()` and a maximum rejection.                           |
| Existence and schema of an incoming notification                                | An additional trigger for`reconcile()`, with no proof value on its own (ARCHITECTURE, D3). |

## What the integration will reuse as-is

- The `Local Payment` doctype and its attempts table, whose typed columns
  cover a push-request flow: payer's number, transaction identifier,
  confirmed amount and currency, raw provider status.
- `lifecycle.py`, `reconcile.py`, and the scheduler, which know about no
  provider.
- The `local_payment_checkout` page in its "number then wait" variant.

The work specific to Orange Money therefore covers three elements: an
`Orange Money Settings` doctype inheriting from `gateway.py`, an HTTP
client in `providers/orange_money.py`, and the structure that this client
writes into `provider_data` for the fields only it reads (ARCHITECTURE,
D8).

One point still needs checking once the documentation is received: if
Local/USSD imposes a step that MTN does not have, for example a prior check
of the payer's account before sending the request, it would add a state to
`lifecycle.py`'s state machine rather than columns to the schema.

## References

| Source                                                                                                               | Nature                             | Content used                                                                                                    |
| -------------------------------------------------------------------------------------------------------------------- | ---------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| Orange Developer, "Orange Money Web Payment / M Payment": overview and FAQ —`developer.orange.com/apis/om-webpay` | Official                           | Payer journey, covered countries, access terms.                                                                 |
| Orange Cameroon, "Webpayment Partner" —`orange.cm/fr/om-partenaires/partenaire-webpayment.html`                   | Official                           | Contracting process.                                                                                            |
| Y-Note, "How to deploy the Orange Money Group / Web API?" —`y-note.cm/comment-deployer-lapi-orange-money/`        | Orange Cameroon partner integrator | Requests and URL for the Web Payment mode. To be confirmed against the documentation delivered to the merchant. |
