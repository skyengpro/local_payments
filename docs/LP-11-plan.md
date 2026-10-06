# LP-11 — Plan d'implémentation : finaliser les Payment Requests ERPNext après un paiement local

Statut : plan validé, rien n'est encore implémenté.
Workstream : Adapter. Estimation du ticket : 1 jour. Avec les décisions prises pendant l'analyse
(blocage de l'annulation, alerte sur un succès tardif, job CI ERPNext), compter plutôt 1,5 jour.

## 1. Contexte et objectif

Sur un site ERPNext, une facture ou une commande se paie par une Payment Request (PR). Quand la
session `Local Payment` passe à `Paid`, `reconcile.authorize()` appelle
`run_method("on_payment_authorized", "Completed")` sur la PR. ERPNext v16 n'implémente pas cette
méthode (frappe/payments#204) : la PR reste `Requested` et aucune Payment Entry n'est créée. Le marchand a l'argent sur son compte MTN et rien dans ses livres.

LP-11 livre `local_payments/erpnext.py`, prévu par ARCHITECTURE (D6) et par l'ADR 0002 mais
encore absent du dépôt :

- quand une de nos passerelles a encaissé, ERPNext règle lui-même la PR (`set_as_paid()`, qui crée
  et soumet la Payment Entry) ;
- quand le marchand annule une PR, les sessions encore ouvertes contre elle sont fermées.

Critères d'acceptation, numérotés pour les tests :

| #   | Critère                                                                                                                                                                        |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| CA1 | Une PR payée via une passerelle MTN MoMo finit`Paid`, avec la Payment Entry qu'ERPNext aurait créée pour ce compte de passerelle. La session est `authorization = Done`. |
| CA2 | Une PR payée via une passerelle qui n'est pas la nôtre, sur le même site, se comporte exactement comme aujourd'hui.                                                          |
| CA3 | Un paiement réglé plusieurs fois (plusieurs déclencheurs, ou une future version d'ERPNext qui règle elle-même) ne produit jamais une seconde Payment Entry.                |
| CA4 | Si ERPNext refuse l'écriture, le paiement reste enregistré sur la session, le marchand est prévenu de l'échec, et un nouveau règlement plus tard réussit sans doublon.    |
| CA5 | Annuler une PR ferme les sessions encore ouvertes : leur page de paiement n'accepte plus de paiement.                                                                           |
| CA6 | Installer, migrer et faire tourner l'application sur un site sans ERPNext n'est pas affecté.                                                                                   |

Décisions prises pendant l'analyse (réponses du 2026-10-02) :

| Sujet                                       | Décision                                                                                                                                                                                                                 |
| ------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `set_as_paid()` exécuté en Guest        | Vérifier d'abord par un test d'intégration (étape 1), décider ensuite.                                                                                                                                                |
| Annulation d'une PR avec de l'argent en jeu | Refuser l'annulation tant qu'une session est`Paid` avec une authorization non `Done`. Les sessions `Open` passent en `Void` dans tous les cas, et un succès tardif sur une session `Void` alerte les managers. |
| Tests ERPNext en CI                         | Ajouter un job CI qui installe ERPNext. Le job actuel, sans ERPNext, couvre CA6.                                                                                                                                          |

## 2. Ce que l'analyse a établi

Constats vérifiés dans les sources installées (frappe 16.31.0, erpnext 16.32.3, payments `cca07d9`).

1. **Ordre d'exécution.** `Document.run_method()` passe par `Document.hook()`
   (`frappe/model/document.py:1612-1661`) : la méthode du contrôleur d'abord, puis les `doc_events`
   des applications. Chaque handler tourne avec `frappe.db._disable_transaction_control` incrémenté,
   donc un `commit()` dans le hook est ignoré avec un avertissement (`database.py:1176-1180`).
   Les savepoints restent utilisables, et c'est ce qu'utilise `reconcile.authorize()`.
2. **Signature.** Le handler est appelé `f(doc, method, *args)`, donc
   `on_payment_authorized(doc, method, status)`.
3. **`set_as_paid()`** (`payment_request.py:337-346`). Pour le canal `Phone`, il passe seulement le
   statut à `Paid`. Sinon il appelle `create_payment_entry()` : `get_payment_entry(...)`, insertion
   et soumission avec `ignore_permissions=True`, puis `make_invoice()` si `make_sales_invoice`.
   La soumission de la PE passe la PR en `Paid` via `update_payment_requests_as_per_pe_references`
   (`frappe.db.bulk_update`). Le `doc` en mémoire, lui, n'est pas mis à jour : d'où la relecture en
   base prévue par l'ADR 0002.
4. **Permissions.** `create_payment_entry()` appelle `set_missing_ref_details(force=True)`, qui
   appelle `get_reference_details()`, et celle-ci fait
   `frappe.has_permission(reference_doctype, "read", ..., throw=True)`
   (`payment_entry.py:2800`). Or `authorize()` tourne en Guest depuis le polling de la page
   (`api.get_status`) et depuis le job du callback MTN, qui garde l'utilisateur qui l'a mis en file.
   **Probable** : en Guest, la PE est refusée et seul le scheduler (Administrator) règle la PR, au
   retry suivant (1 h). À confirmer à l'étape 1.
5. **Fuite de message vers le payeur.** Un `frappe.throw` dans `set_as_paid()` ajoute son texte à
   `frappe.local.message_log`. `reconcile` attrape l'exception, mais `get_status` répond quand même
   avec `_server_messages` (`frappe/utils/response.py:200`), et `frappe.call` sur la page l'affiche
   en boîte de dialogue. Le payeur verrait un message comptable interne, ce qui enfreint la règle
   « erreurs génériques côté payeur ».
6. **`cancel` d'une PR** (`payment_request.py:278-281`) : `check_if_payment_entry_exists()`
   bloque déjà si la PR est `Paid` avec une PE, puis `set_as_cancelled()`. Notre handler
   `on_cancel` s'exécute ensuite, dans la même transaction. Un `frappe.throw` de notre part annule
   donc toute l'annulation.
7. **Session `Void` puis succès tardif.** `lifecycle.resolve()` renvoie `Succeeded` pour la
   tentative et laisse la session `Void`, sans drapeau. `reconcile._record()` n'alerte que sur
   `duplicate` ou `amount_mismatch`. Aujourd'hui, ce cas ne prévient donc personne.
8. **`start_attempt` refuse déjà une session `Void`** (`api._ensure_open`, vérifié deux fois, dont
   une sous verrou), et la page affiche l'issue au lieu du formulaire
   (`test_void_session_shows_its_outcome_instead_of_the_form`). CA5 ne demande donc que le passage
   à `Void`.
9. **Aucun code ne passe une session à `Void` aujourd'hui.** Pas de helper à réutiliser ; le
   bouton « Cancel » du formulaire `Local Payment` cité dans ARCHITECTURE n'existe pas encore (hors
   périmètre de LP-11).
10. **CI** : `.github/workflows/ci.yml` installe `payments` et `local_payments`, pas ERPNext.

## 3. Corps du plan

Les étapes se suivent dans cet ordre. Chacune se termine par des tests verts avant la suivante.

### Étape 0 — Préparation

- Charger les skills `frappe-syntax-hooks-events` et `frappe-testing-unit`. Lire l'ADR 0002 et la
  règle `.claude/rules/reconcile-transactions.md`.
- Context7 (`resolve-library-id` puis `query-docs`) sur Frappe et ERPNext : `doc_events`,
  `run_method`, `frappe.set_user`, `frappe.clear_messages`, `IntegrationTestCase`. CLAUDE.md fait des
  sources installées la référence pour les internes Frappe : chaque point Context7 est confirmé dans
  la source v16, avec le fichier cité.

### Étape 1 — Vérifier le contexte Guest (spike tranché par un test)

1. Écrire le test d'intégration de CA1 (étape 4) en deux variantes : `authorize()` en
   Administrator, puis en Guest (`frappe.set_user("Guest")`).
2. Lancer les deux variantes sur le site de dev avec ERPNext.
3. Résultat :
   - **Guest passe** : rien à faire, on continue.
   - **Guest échoue sur une permission**: présenter le choix au demandeur avec la
     trace réelle. Option recommandée : `erpnext.py` exécute `set_as_paid()` sous Administrator et
     restaure l'utilisateur précédent dans un `finally`. On n'arrive dans le hook qu'après une
     preuve de paiement interrogée par le site et après les gardes. Autre option : laisser le
     scheduler régler, avec 1 h de décalage et un essai consommé à chaque fois.
4. Consigner la décision dans l'ADR 0002 (étape 7).

### Étape 2 — Logique pure, testée sans site

Deux petites fonctions sans accès base, dans `erpnext.py`. Le module importe `frappe`, mais ces
fonctions ne lisent que leurs arguments.

- `settlement_action(is_ours, status, pr_docstatus, pr_status) -> "skip" | "settle" | "refuse"` :
  - `skip` si la passerelle n'est pas la nôtre, si `status` n'est ni `Authorized` ni `Completed`,
    ou si la PR est déjà `Paid` ;
  - `refuse` si la PR n'est pas soumise (`docstatus != 1`, par exemple annulée entre-temps). Le hook
    lève alors une erreur claire, `authorize()` passe à `Failed`, et le marchand est prévenu par le
    circuit existant ;
  - `settle` sinon.
- `blocks_cancel(sessions) -> bool` : vrai si une session est `Paid` avec une authorization autre
  que `Done`.

Dans `lifecycle.py`, ajouter à `Resolution` un drapeau `void_paid` (le succès d'une session `Void`),
posé dans la branche existante `session_status != OPEN`. Il suit le modèle de `duplicate` et
`amount_mismatch`.

Tests unitaires (`UnitTestCase`, sans base) : toutes les branches des deux fonctions et le nouveau
drapeau de `resolve()`.

### Étape 3 — Adaptateur `local_payments/erpnext.py`

Aucun import d'`erpnext` au niveau du module : sans ERPNext, ce fichier se charge quand même (CA6).

**`on_payment_authorized(doc, method, status)`**, dans l'ordre de l'ADR 0002 :

1. Notre passerelle ? `frappe.db.get_value("Payment Gateway", doc.payment_gateway, "gateway_settings")`
   est dans `SETTINGS_DOCTYPES`, un tuple défini dans `gateway.py` (`MTN MoMo Settings`,
   `Orange Money Settings`). Sinon on sort : CA2.
2. Statut `Authorized` ou `Completed`, sinon on sort.
3. Relecture sous verrou :
   `frappe.db.get_value("Payment Request", doc.name, ["docstatus", "status"], for_update=True)`.
   `Paid` : on sort (CA3, y compris quand une future méthode ERPNext a déjà réglé la PR avant nous).
   Non soumise : `frappe.throw` (voir `refuse` à l'étape 2).
4. `set_as_paid()` sur la PR relue après le verrou, pas sur le `doc` reçu, qui peut être périmé.
   Selon l'étape 1, exécuté sous Administrator.

Pas de `try/except`, pas de `commit`. Une erreur remonte telle quelle à `authorize()`, qui
annule au savepoint, note `Failed`, compte l'essai et alerte (CA4).

**`void_open_sessions(doc, method)`**, sur `on_cancel` :

1. Lire les sessions `Local Payment` avec `reference_doctype = "Payment Request"` et
   `reference_docname = doc.name`.
2. Si `blocks_cancel(...)` : `frappe.throw` avec un message traduit du genre « Un paiement a été
   reçu pour cette demande et n'est pas encore comptabilisé. Relancez son autorisation depuis la
   session {0} avant d'annuler. » La transaction d'annulation est alors entièrement annulée.
3. Pour chaque session `Open` : `frappe.get_doc(..., for_update=True)`, nouvelle vérification de
   `Open`, `lc.check_session_transition(OPEN, VOID)`, `status = Void`,
   `save(ignore_permissions=True)`. Pas de `commit` : l'annulation de la PR le fait. Le verrou
   empêche un `reconcile()` concurrent de passer la session à `Paid` pendant que nous la fermons.

**`hooks.py`** :

```python
doc_events = {
    "Payment Request": {
        "on_payment_authorized": "local_payments.erpnext.on_payment_authorized",
        "on_cancel": "local_payments.erpnext.void_open_sessions",
    }
}
```

### Étape 4 — Ajustements minimaux dans `reconcile.py`

`erpnext.py` ne fait que signaler l'échec (note technique du ticket). Deux retouches restent
nécessaires côté `reconcile`, parce que LP-11 est le premier consommateur qui peut lever une
erreur pendant une requête Guest, et le premier qui produit des sessions `Void` :

1. `_record_authorization_failure()` vide `frappe.local.message_log` (`frappe.clear_messages()`,
   à confirmer en v16), pour qu'aucun message ERPNext n'atteigne la page du payeur (constat 5). Le
   détail reste dans Error Log et dans `authorization_error`.
2. `_record()` alerte les managers quand `resolution.void_paid`, avec une nouvelle raison
   `void_paid` dans `alert_managers()` : « Paiement reçu sur {0} (tentative {1}) après son
   annulation. Le traiter à la main. » Chaîne traduite en français dans `locale/fr.po`.

### Étape 5 — Tests d'intégration ERPNext

Fichier `local_payments/tests/test_erpnext.py`, en `IntegrationTestCase`, sauté proprement si
`"erpnext" not in frappe.get_installed_apps()`. Le jeu de données réutilise les helpers de test
d'ERPNext quand ils existent (société, client, facture soumise) ; on vérifie d'abord ce que
`erpnext/accounts/doctype/payment_request/test_payment_request.py` propose. Il comprend une
`MTN MoMo Settings` avec sa passerelle, un `Payment Gateway Account` et une PR créée par
`make_payment_request(..., submit=True)`. `reconcile` commettant, le nettoyage suit le modèle de
`test_checkout.py`. Le fournisseur est un `FakeProvider`, sans appel réseau ni vrai MSISDN.

| Test                                                                                                                                                                                                                                | CA          |
| ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------- |
| Succès via`reconcile()` : PR `Paid`, une PE soumise, `paid_to` égal au compte du Payment Gateway Account, `reference_no` égal au nom de la PR, session `Done`                                                          | CA1         |
| Même scénario en Guest (étape 1)                                                                                                                                                                                                 | CA1         |
| PR sur une passerelle tierce :`run_method("on_payment_authorized", "Completed")` ne change ni le statut ni le nombre de PE                                                                                                        | CA2         |
| Deux`authorize()` puis un appel direct du hook : toujours une seule PE                                                                                                                                                            | CA3         |
| Règlement en amont simulé (contrôleur patché avec un`on_payment_authorized` qui appelle `set_as_paid`) : une seule PE                                                                                                       | CA3         |
| ERPNext refuse (cause réelle, par exemple compte de passerelle sans compte bancaire) : session`Paid`, authorization `Failed`, erreur en Error Log, aucune PE ; cause corrigée puis `authorize()` : `Done` et une seule PE | CA4         |
| Échec pendant`get_status` en Guest : la réponse ne porte pas `_server_messages`                                                                                                                                               | CA4, payeur |
| PR annulée : sessions`Open` passées à `Void`, `start_attempt` refusé                                                                                                                                                      | CA5         |
| Annulation refusée tant qu'une session est`Paid` non `Done`                                                                                                                                                                    | CA5         |
| Succès tardif sur une session`Void` : tentative `Succeeded`, session `Void`, alerte managers                                                                                                                                 | CA5         |

CA6 est couvert par la suite complète du job CI sans ERPNext, plus un test qui résout les deux
chemins de `doc_events` par `frappe.get_attr` sans ERPNext installé.

### Étape 6 — CI

Dans `.github/workflows/ci.yml`, une matrice `erpnext: [false, true]`. Avec `true` :
`bench get-app erpnext --branch version-16`, puis `install-app erpnext` avant `local_payments`.
Le job sans ERPNext reste tel quel. Vérifier que le garde du cœur pur (`check_pure_core.py`)
continue de passer : `lifecycle.py` ne gagne qu'un champ booléen.

### Étape 7 — Documentation

- ADR 0002, mis à jour avec le skill `lp-write-adr` : décision Guest de l'étape 1, refus
  d'annulation quand une session est payée mais pas comptabilisée, garde `docstatus`, relecture du
  document après verrou. Cocher les actions 1 à 5.
- ARCHITECTURE.md :
  - cycle de vie de session (`Open → Void` sur annulation de PR, alerte sur un succès tardif) ;
  - alertes ajoutées à « Scheduled tasks » ou à la liste des alertes ;
  - « Refus par conception » : annulation refusée ;
  - « Known weaknesses » : PR partiellement payée (voir risques).
- Docs en français, sans référence de ticket dans le code.

### Étape 8 — Contrat qualité (méthodologie à appliquer)

1. **Bonnes pratiques, sécurité et idiomatisme.** Context7 pour Frappe et ERPNext
   (`doc_events`, élévation d'utilisateur, `clear_messages`, `IntegrationTestCase`, conventions de
   traduction), chaque point confirmé dans la source v16 installée, avec le fichier cité dans la
   PR. En cas de désaccord, la source installée l'emporte (CLAUDE.md).
2. **Code minimal, KISS.** Un module d'environ 60 lignes, aucun nouveau doctype ni champ, aucune
   dépendance. Pas d'écriture comptable dans l'application : ERPNext crée la PE. Pas de
   `try/except` dans le hook. Avant tout helper, chercher l'équivalent dans frappe, erpnext et
   payments.
3. **Tests unitaires sur la logique pure.** `settlement_action`, `blocks_cancel` et le drapeau
   `void_paid` de `resolve()`, toutes branches couvertes, sans base. Les tests d'intégration
   couvrent les critères d'acceptation.
4. **Première review avant tout fix.** Une fois le code et les tests verts : une review complète
   (skill `code-review`, niveau `high`). Chaque constat est corrigé, et les tests repassent,
   avant de passer à la suite.
5. **Reviews par domaine**, après la première série de corrections, chaque constat corrigé avant la
   review suivante :
   - invariants de paiement : `lp-review-invariants` (D4, ADR 0002, idempotence, aucune écriture
     comptable dans l'application, hook limité à nos passerelles) ;
   - sécurité : `security-review` (élévation d'utilisateur, surface Guest, fuite de messages) ;
   - idiomatisme Frappe : checklists de `frappe-errors-hooks` et `frappe-syntax-hooks-events`
     (signature du handler, aucun commit dans un hook, contexte sans ERPNext) ;
   - simplicité : `simplify` sur le diff ;
   - traductions : `frappe-core-translation` pour les nouvelles chaînes et `fr.po`.
6. **Passe `humanizer:humanizer`** sur les commentaires, les docstrings et les passages modifiés
   des docs. Ton humain, neutre et concis, fidèle au code. Une ligne par commentaire, qui dit ce
   que fait le code, sans citer ARCHITECTURE, une ADR ou un ticket.

Skills vérifiés comme disponibles dans ce dépôt (`.claude/skills/`) ou dans la session :
`lp-review-invariants`, `lp-write-adr`, `frappe-syntax-hooks-events`, `frappe-syntax-hooks`,
`frappe-errors-hooks`, `frappe-testing-unit`, `frappe-testing-cicd`, `frappe-core-translation`,
`frappe-core-permissions`, `code-review`, `simplify`, `security-review`, `humanizer:humanizer`.
Serveur MCP Context7 disponible. Aucun skill propre à la comptabilité ERPNext : pour
`Payment Request` et `Payment Entry`, la seule référence est la source installée.

**Fini veut dire** : tests purs et d'intégration verts dans les deux jobs CI, garde du cœur pur
vert, ADR 0002 et ARCHITECTURE à jour, toutes les reviews closes.

## 4. Points d'intégration avec l'existant

| Élément existant                                         | Interaction LP-11                                                                                                                                                                                          |
| ---------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `reconcile.authorize()` / `_call_consumer()`           | Appelle le hook via`run_method`. Possède la transaction (savepoint `lp_authorize`), le compteur d'essais, la date de retry et l'alerte. Inchangé, sauf le vidage de `message_log` en cas d'échec. |
| `reconcile._record()` / `alert_managers()`             | Nouvelle raison d'alerte`void_paid`.                                                                                                                                                                     |
| `lifecycle.resolve()` / `Resolution`                   | Nouveau drapeau`void_paid`. `check_session_transition(OPEN, VOID)` est réutilisé tel quel.                                                                                                           |
| `scheduler.retry_authorizations` / `send_daily_alerts` | Inchangés. Ils relancent et alertent les autorisations`Failed` produites par le hook.                                                                                                                   |
| `api.start_attempt` / `_ensure_open`, page de paiement | Inchangés. Ils refusent déjà une session`Void` et affichent son issue.                                                                                                                                |
| `gateway.py`                                             | Nouveau tuple`SETTINGS_DOCTYPES`, à compléter par le skill `lp-add-provider` pour chaque nouveau fournisseur.                                                                                        |
| `hooks.py`                                               | Nouveau bloc`doc_events` sur `Payment Request`.                                                                                                                                                        |
| ERPNext`PaymentRequest.set_as_paid()`                    | Seul créateur de la Payment Entry (invariant 9).                                                                                                                                                          |
| ERPNext`PaymentRequest.on_cancel()`                      | S'exécute avant notre handler, dans la même transaction.                                                                                                                                                 |
| ERPNext`Payment Gateway Account`                         | Fournit`payment_account`, la cible de la PE. Doit exister pour la société de la PR (D5).                                                                                                               |
| `.github/workflows/ci.yml`                               | Matrice avec et sans ERPNext.                                                                                                                                                                              |
| ADR 0002, ARCHITECTURE D4, D6 et cycle de vie              | Mis à jour à l'étape 7.                                                                                                                                                                                 |

## 5. Risques et inconnues

| Risque / inconnue                                                                                                                                                                                                                                            | Traitement                                                                                                                                                                                                                  |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `set_as_paid()` échoue en Guest (constat 4).                                                                                                                                                                                                              | Étape 1. Si l'échec se confirme, la décision d'élévation revient au demandeur et est consignée dans l'ADR 0002.                                                                                                       |
| Élever à Administrator élargit ce qu'une requête Guest peut déclencher.                                                                                                                                                                                 | Élévation limitée à`set_as_paid()`, après les gardes, utilisateur restauré dans `finally`, revue `security-review`.                                                                                             |
| Callbacks`after_commit` ou jobs mis en file par une PE ensuite annulée au savepoint : `rollback(save_point=...)` ne vide pas `after_commit` (`database.py:1199`), et le commit de `Failed` pourrait les déclencher pour une PE qui n'existe pas. | Le vérifier pendant le test CA4 (Error Log, file RQ). S'il se confirme, le corriger dans`reconcile`, pas dans le hook.                                                                                                   |
| `create_payment_entry()` met `frappe.flags.ignore_account_permission = True` sans le remettre à zéro.                                                                                                                                                  | Comportement d'ERPNext, effet limité à la requête ou au job courant. Noté, non corrigé.                                                                                                                                |
| PR`Partially Paid` (une PE manuelle partielle existe déjà) : la session porte `grand_total`, mais `set_as_paid()` ne comptabilise que `outstanding_amount`.                                                                                        | Hors périmètre. On garde le comportement d'ERPNext (CA1 : « la PE qu'ERPNext aurait créée ») et on documente dans « Known weaknesses ».                                                                             |
| `make_invoice()` (commande avec `make_sales_invoice`) échoue après la PE.                                                                                                                                                                              | Le savepoint annule PE et facture ensemble, et le retry recommence de zéro. Couvert par le principe de CA4.                                                                                                                |
| Annulation refusée : un marchand bloqué par une autorisation`Failed` répétée.                                                                                                                                                                         | Le message indique la session à traiter. Le bouton « Retry authorization » du formulaire`Local Payment` n'existe pas encore (hors LP-11). En attendant, le retry du scheduler ou la console. À signaler au demandeur. |
| Coût du job CI avec ERPNext (installation longue).                                                                                                                                                                                                          | Accepté. Cache pip et yarn existants.                                                                                                                                                                                      |
| ERPNext`version-16` bouge sans version épinglée en CI.                                                                                                                                                                                                   | Un changement de`set_as_paid()` ferait échouer les tests CA1 à CA4, ce qui est le signal voulu.                                                                                                                         |
| Canal`Phone` : avec notre passerelle, la PR appellerait `request_phone_payment()`, que nous n'implémentons pas (ADR 0001).                                                                                                                              | Hors périmètre : une telle PR échoue dès sa soumission, avant tout paiement.                                                                                                                                            |
| Base du site de dev inaccessible depuis cet environnement (erreur 1045 sur`lp-test.localhost`).                                                                                                                                                            | Corriger l'accès avant l'étape 1, sinon les tests d'intégration ne peuvent pas tourner.                                                                                                                                  |
