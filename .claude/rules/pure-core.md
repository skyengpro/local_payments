---
paths:
  - "local_payments/providers/**/*.py"
  - "local_payments/lifecycle.py"
---

# Pure core: `providers/` and `lifecycle.py`

- No `import frappe` or `from frappe...`, direct or transitive. Standard library, typed
  dataclasses and the HTTP client already used in `providers/` only. No new dependency without an ADR.
- Provider clients return a normalised `ProviderResult`. They never raise raw HTTP errors upward:
  map timeouts, 4xx, 5xx and unknown statuses to the states in the gateway doc
  ("Correspondance des statuts", "Erreurs connues").
- Provider-specific fields go in a dataclass serialised to `provider_data` (ARCHITECTURE D8).
  Never a secret in it.
- `lifecycle.py` decides transitions and amount/currency conformity, nothing else. Pass `now` in
  as a parameter so transitions are testable without a clock.
- No transition leaves a final state (`Succeeded`, `Failed`, `Expired`, `Error`).
- Every branch has a test using recorded HTTP responses. No live call, no real MSISDN.
