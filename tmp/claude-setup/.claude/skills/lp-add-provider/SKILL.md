---
name: lp-add-provider
description: >
  Add or extend a mobile-money provider gateway in local_payments (Orange Money Local/USSD next,
  or a later provider): Settings doctype, pure HTTP client in providers/, status mapping, tests,
  gateway doc. Use whenever the user asks to implement, start, scaffold or continue Orange Money,
  MTN MoMo changes, or "a new gateway/provider", even if they only say "let's do Orange".
---

# Add a provider gateway

## Stop condition (check first)

Open `docs/gateways/<provider>.md`. If it has a table of items still to obtain (as
`orange-money.md` does) and any row is unresolved, **do not write the client**. List the missing
rows to the user and stop. Never fill a gap from third-party blog posts or memory: the docs state
why (Orange Cameroon changed credentials and endpoints recently).

Doing only what does not depend on the missing rows is allowed: the Settings doctype skeleton
without API fields, and test scaffolding.

## Steps (all four layers, in this order)

1. **Doc first.** Update `docs/gateways/<provider>.md`: configuration table, operations,
   status mapping, known errors, points to confirm. Same headings as `mtn-momo.md`.
2. **Pure client** `local_payments/providers/<provider>.py`: token, initiate, status. Returns
   `ProviderResult`. Provider-only fields in a dataclass for `provider_data`. No `frappe` import.
   Initiation errors after the identifier is known keep the attempt `Initiated` (status stays
   queryable) unless the gateway doc says otherwise.
3. **Tests before adapters**: recorded HTTP responses for success, pending, failed, refused
   initiation, timeout, 401 then renewed token, unknown status, amount/currency mismatch.
   No live call. Fake MSISDN only.
4. **Adapters**: `<Provider> Settings` doctype inheriting `gateway.py`, one document per merchant
   contract (not Single), `Password` fields for secrets, callback endpoint in `api.py` if the
   provider has one (trigger only, ignores unknown attempts). Reuse `reconcile.py`,
   `lifecycle.py`, the scheduler and the checkout page as they are. If the provider needs a state
   or step MTN lacks, extend the state machine in `lifecycle.py` (ARCHITECTURE D8), not the schema.

## Then

- Update the ARCHITECTURE tables (scope, modules, data model, entry points, install steps).
- Run `lp-review-invariants` on the diff.
- If a decision differs from ADR 0001/0002 or D1-D8, write an ADR (`lp-write-adr`) before merging.
