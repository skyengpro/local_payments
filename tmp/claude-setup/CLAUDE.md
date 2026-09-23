# local_payments

Frappe app that adds Cameroon mobile-money gateways (MTN MoMo first, then Orange Money
Local/USSD) on top of `frappe/payments`. Targets Frappe v15 and v16. ERPNext is optional.
v14 is out of scope.

## Source of truth

When sources disagree, follow this order:

1. `docs/ARCHITECTURE.md`, `docs/decisions/*`, `docs/gateways/*`
2. Installed source of frappe, erpnext, payments (see "Method")
3. Community skills `.claude/skills/frappe-*` (generic, not written for this app)
4. Your own recollection

If a task would contradict an ADR, stop and say so. Propose a new ADR (skill `lp-write-adr`);
never deviate silently. Docs are French and stay French.

Read before touching:

| Area | Read first |
| --- | --- |
| `gateway.py`, Settings doctypes | ARCHITECTURE D1, D5 · `gateways/<provider>.md#configuration` |
| `reconcile.py`, `lifecycle.py` | ARCHITECTURE D3, D4, "Cycle de vie" |
| `api.py`, checkout page | ARCHITECTURE D2, "Points d'entrée", "Refus par conception" |
| `erpnext.py` (Payment Request) | ADR 0002, ARCHITECTURE D6 |
| `providers/mtn_momo.py` | `gateways/mtn-momo.md` (statuts, erreurs, points à confirmer) |
| `providers/orange_money.py` | `gateways/orange-money.md` — contract NOT received, do not implement from guesses |
| `scheduler.py` | ARCHITECTURE "Tâches planifiées" |

## Layout

Pure core (no `frappe` import, enforced by CI): `providers/`, `lifecycle.py`.
Frappe adapters: `gateway.py`, `api.py`, `reconcile.py`, `erpnext.py`, `scheduler.py`,
`templates/pages/local_payment_checkout`. Doctypes: `MTN MoMo Settings`, `Orange Money Settings`,
`Local Payment`, `Local Payment Attempt` (child table).

## Invariants (never break; details in ARCHITECTURE)

1. `get_payment_url()` creates a `Local Payment` session and returns the checkout URL. It never
   calls a provider. GET on the checkout page has no side effect; only POST `start_attempt`
   starts an attempt (D2).
2. Only a merchant-authenticated status query proves a payment. Callback, return URL, page
   polling and scheduler are triggers that all go through `reconcile()`. Never trust a callback
   body or URL parameter (D3).
3. Record, then act. Session -> `Paid` is committed in its own transaction. `on_payment_authorized`
   runs in a second transaction under a row lock (`frappe.get_doc(..., for_update=True)`). If it
   fails: `authorization = Failed`, `Paid` stays (D4).
4. Consumer callbacks can be replayed and run as Guest, user or Administrator: idempotent, no
   dependence on `frappe.session.user`.
5. Provider-confirmed amount and currency are compared with the session. Mismatch ->
   `amount_mismatch`, session stays `Open`. XAF has no sub-unit: reject non-integers, never round.
6. `providers/` and `lifecycle.py` import no `frappe`. No HTTP call outside `providers/`.
7. Secrets live in `Password` fields only. Never in logs, `provider_data`, Integration Request,
   fixtures, tests, recorded HTTP responses, or commit messages.
8. Guest surface: access by `token` only, sequential `name` never exposed, every guest endpoint has
   `rate_limit`, unknown callback attempt is ignored with no outbound call.
9. This app writes no accounting entries. ERPNext creates the Payment Entry via `set_as_paid()`;
   the `doc_events` hook applies to our own gateways only (ADR 0002).
10. No refunds, disbursements or recurring payments in v1.

## Frappe rules

- No `frappe.db.commit()` in controllers or hooks. Explicit commits only where D4 requires them
  (`reconcile.py`).
- `Local Payments Manager` and any other role ship as fixtures or patches, never hand-configured.
- Deliverables are app code. No Server Scripts, no Client Scripts created in the desk.
- Code must work on v15 and v16. When an API differs, check both branches and say which.

## Method

- **Don't reinvent.** Before adding a helper, search `frappe`, `erpnext`, `payments` for it. State
  what exists and the specific limit that blocks reuse. Installed sources are readable through
  `additionalDirectories` (`.claude/settings.json`).
- **Verify version-sensitive APIs in source, not in a skill or from memory.** Known case: the
  community skill `frappe-core-cache` teaches `frappe.lock()`, which does not exist in v15 or v16
  (use row locks or `frappe.utils.synchronization.filelock`). Cite the file you checked.
- **Context7** for third-party library docs. Not for Frappe internals: read the source.
- **Never guess provider behaviour.** Unknown Orange Money endpoints, schemas, statuses, ID format:
  stop and list what is missing (`gateways/orange-money.md`, table "Ce qu'il faut obtenir").
  MTN "points à confirmer" stay configuration, not hard-coded assumptions.
- **Small scope first**: one gateway, one consumer (Payment Request), one site, then generalise.
- Tests: pure core with recorded HTTP responses, no live provider call, no real MSISDN
  (use obviously fake numbers). Sandbox credentials come from the environment, never from the repo.

## Commands

TODO: fill with the repo's real commands, then delete this line.

```bash
bench --site <dev-site> run-tests --app local_payments   # site tests (stable Frappe CLI)
# pure tests:        TODO
# lint / format:     TODO
# CI import guard:   TODO (fails if providers/ or lifecycle.py imports frappe)
```

Never run `bench migrate`, `restore`, `reinstall` or `drop-site` against a non-dev site.

## Conventions (TODO confirm)

Code, identifiers, comments, commit messages: English. Docs in `docs/`: French, Mermaid diagrams.
Behaviour change => update the matching ARCHITECTURE / gateways table in the same PR.

## Done means

Pure tests and site tests green, CI import guard green, docs updated, and the invariants above
checked on the diff (skill `lp-review-invariants`).
