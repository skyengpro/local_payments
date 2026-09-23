---
name: lp-review-invariants
description: >
  Review a diff, branch or PR of the local_payments app against its non-negotiable payment
  invariants (ARCHITECTURE D2-D4, ADR 0001/0002, guest-surface security, secrets, amount checks).
  Use whenever the user asks to review, check, audit or validate local_payments code, says "before
  merge" or "is this safe", or after you finish editing gateway.py, api.py, reconcile.py,
  lifecycle.py, scheduler.py, erpnext.py, providers/* or the checkout page, even if they do not ask
  for a review explicitly.
---

# local_payments invariant review

Goal: find violations of the invariants in `CLAUDE.md`, not style issues. Generic Frappe checks
(hooks, controllers, DocType JSON) come from the `frappe-*` skills, but on any conflict
`docs/ARCHITECTURE.md` wins.

## Procedure

1. `git diff <base>...HEAD --stat`, then read every touched file in full.
2. Map each file to the invariants that apply (table below) and check only those.
3. Run the greps. A hit is a lead, not a verdict: read the context.
4. For each Frappe API used in a way that could differ between v15 and v16, confirm the signature
   in the installed source (`../frappe-bench/apps/frappe`) and cite the file.
5. Report.

| Touched | Invariants |
| --- | --- |
| `gateway.py` | 1 (no provider call in `get_payment_url`), 5 (currency check), disabled gateway refuses |
| `api.py`, checkout page | 1, 2, 7, 8 |
| `reconcile.py`, `scheduler.py` | 2, 3, 4, 5 |
| `erpnext.py` | 3, 4, 9 and ADR 0002 order of steps |
| `providers/`, `lifecycle.py` | 5, 6, 7 |

## Greps

```bash
grep -rnE "^\s*(import frappe|from frappe)" local_payments/providers local_payments/lifecycle.py
grep -rn  "frappe.db.commit" local_payments
grep -rn  "allow_guest" local_payments          # each hit must also have rate_limit
grep -rniE "(api_key|subscription_key|password|secret|token).*(log|print|msgprint|throw)" local_payments
grep -rniE "requests\.|httpx\.|urllib" local_payments --include=*.py | grep -v providers/
grep -rn  "\.name" local_payments/api.py local_payments/templates   # sequential name leaked to guests?
```

## Report format

```
## Blocking (breaks an invariant)
- [inv N] path:line — what, why it matters, minimal fix
## Should fix
## Notes / questions
## Verified against source
- api or behaviour — file checked (branch)
```

If nothing is blocking, say so in one line and list what was verified. Do not pad.
