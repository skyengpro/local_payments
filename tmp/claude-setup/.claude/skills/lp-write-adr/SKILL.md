---
name: lp-write-adr
description: >
  Write or update an Architecture Decision Record in docs/decisions/ of the local_payments repo,
  in the repo's French ADR format (same as 0001 and 0002). Use whenever a change contradicts or
  extends ARCHITECTURE.md decisions D1-D8, an existing ADR, or the frappe/payments contract, or
  when the user says ADR, decision record, "document this decision" or "why did we choose".
  Not for the company-wide ADR registry (ADR-001..009, English): that is a separate register.
---

# Repo ADR (French, `docs/decisions/NNNN-slug.md`)

1. Number: next free `NNNN` after the highest file in `docs/decisions/`. Slug in English kebab-case
   like `0001-native-payments-contract.md`. Read 0001 and 0002 first and mirror their tone.
2. Header: `# ADR-NNNN : <title>`, `**Statut :** Proposé | Accepté | Remplacé par ADR-XXXX`,
   `**Date :** YYYY-MM-DD` (use the real date). New ADRs start as `Proposé`; only the user accepts.
3. Sections, in this order:
   - `## Contexte`: facts and constraints, with a table when consumers or providers are compared.
     Verify each claim about Frappe/ERPNext/frappe/payments in source and cite version or branch.
   - `## Décision`: one short paragraph, then code or list only if it clarifies.
   - `## Options considérées`: `### Option A : ... (retenue)`, then B, C, D. Each has a
     Dimension/Évaluation table, **Avantages**, **Inconvénients**. Include the option "do nothing"
     if plausible.
   - `## Analyse des compromis`: what decides between options, in a few sentences.
   - `## Conséquences`: **Plus simple**, **Plus difficile**, **À revoir** (with the trigger event).
   - `## Actions`: `- [ ]` checkboxes, each verifiable.
4. Link it: add it to "Documents liés" in `docs/ARCHITECTURE.md`, and to the relevant `D#` section
   ("Options écartées et conséquences : ADR NNNN"). If it supersedes another ADR, update that
   ADR's status line only.
5. Do not invent facts. Anything unknown goes under "À revoir" or "Actions" as a question.
