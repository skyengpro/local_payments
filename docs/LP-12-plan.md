# LP-12 — Plan d'implémentation : refuser les comptes de paiement qui ne peuvent pas recevoir d'écriture

Statut : plan à valider, rien n'est encore implémenté.
Workstream : Adapter. Estimation du ticket : 0,5 jour. Dépendance : LP-11 (fusionné, PR #24).
Il faut aussi reprendre les fixtures de `test_erpnext.py` (voir constat 8) : compter plutôt 1 jour.

## 1. Contexte et objectif

Le 2026-10-05, le Payment Gateway Account `MTN MoMo-MTN - XAF - SEP` pointait vers un compte de
groupe. Deux sessions sont passées à `Paid` : le payeur avait payé, mais `set_as_paid()` a échoué à la
soumission de la Payment Entry (« is a Group Account… »). Les Payment Requests sont restées
`Requested` et rien n'a été comptabilisé (security-review, LP-SEC-01 et LP-IMP-03).

LP-12 vérifie le compte quand il est configuré, puis une seconde fois avant que le payeur paie.
L'échec ne peut ainsi plus survenir une fois l'argent encaissé. Si un règlement échoue malgré tout,
la cause est lisible dans `authorization_error`.

Critères d'acceptation, numérotés pour les tests :

| #   | Critère |
| --- | ------- |
| CA1 | L'enregistrement d'un Payment Gateway Account rattaché à une de nos passerelles est refusé si son compte est un groupe, est désactivé, n'est pas de type `Bank` ou `Cash`, appartient à une autre société, ou a une autre devise que le Settings de la passerelle. Les comptes des autres passerelles ne sont pas concernés. |
| CA2 | La soumission d'une Payment Request dont le compte de paiement échoue au même contrôle est refusée avec une erreur lisible. Aucune session n'est créée. |
| CA3 | Le démarrage d'une tentative sur une session `Open` dont la Payment Request a un tel compte est refusé avant tout appel à MTN. |
| CA4 | Si le règlement échoue quand même sur un problème de compte, `authorization_error` commence par une cause lisible (« Payment account is not postable »), suivie du texte d'ERPNext. |
| CA5 | Sur un site sans ERPNext, aucun de ces contrôles ne s'exécute et rien n'échoue. |

Décisions prises pendant l'analyse (réponses du 2026-10-06) :

| Sujet | Décision |
| ----- | -------- |
| Où contrôler la Payment Request à la soumission | `doc_event` `validate` sur `Payment Request`, actif seulement à la soumission. Pas dans `get_payment_url()`, contrairement à la recommandation 2 de LP-SEC-01 (voir constat 4). |
| PGA créé automatiquement par ERPNext et refusé par notre contrôle | Le Settings s'enregistre quand même. On affiche un message invitant à créer le compte à la main, comme ERPNext le fait déjà quand il ne trouve pas de compte. |
| Payeur refusé dans `start_attempt` | Le payeur voit le message générique existant. La cause va dans l'Error Log, rattachée à la session. Aucun nouveau champ. |
| Compte gelé (`freeze_account = Yes`) | Hors périmètre. On le note dans les risques. |

## 2. Ce que l'analyse a établi

Vérifié dans les sources installées : frappe 16.31.0, erpnext 16.32.3, payments `cca07d9`.

1. **Payment Gateway Account** (`payment_gateway_account.py`). Son `validate()` ne fait que copier
   `account_currency` dans `currency`, puis gérer `is_default`. Il ne contrôle rien d'autre. Comme
   notre `doc_event` `validate` s'exécute après la méthode du contrôleur, `doc.currency` est déjà
   rempli quand il tourne. Champs : `payment_gateway`, `payment_account`, `company`, `currency`.
2. **Account** (`account.json`) : `is_group`, `disabled`, `account_type` (dont `Bank` et `Cash`),
   `company`, `account_currency`, `freeze_account`. À la soumission d'une écriture, ERPNext refuse un
   groupe et une autre société (`gl_entry.py:230-254`), un compte désactivé
   (`general_ledger.py:136`) et un compte gelé (`gl_entry.py:420`). Le type de compte, lui, n'est
   jamais contrôlé : un compte de charge passe sans erreur (incident 6011 de LP-SEC-01).
3. **Ordre à la soumission d'une PR** (`frappe/model/document.py:1408-1413`) : `validate`, puis
   `before_submit`. Les deux passent par `run_method` : la méthode du contrôleur d'abord, puis les
   `doc_events`. C'est le `before_submit` d'ERPNext (`payment_request.py:204-238`) qui appelle
   `set_payment_request_url()`, puis `get_payment_url()`, et c'est là qu'il crée notre session.
   Un hook `validate` refuse donc la PR avant qu'aucune session n'existe.
4. **Pourquoi ne pas contrôler dans `get_payment_url()`.** Lancé depuis `before_submit`, le contrôle
   devrait relire la PR en base. Or une PR insérée directement avec `docstatus = 1` n'y est pas
   encore (`insert` : `run_before_save_methods` passe avant `db_insert`). Un hook `validate`, lui,
   lit le document en mémoire. Il évite aussi que `gateway.py`, qui doit rester indépendant
   d'ERPNext, importe `erpnext.py`, lequel importe déjà `gateway.py`.
   `make_payment_request` fait `insert()` puis `submit()` (`payment_request.py:762-766`) : le hook
   couvre ce chemin aussi.
5. **`payment_account` d'une PR** est en lecture seule, avec `fetch_from` qui pointe vers
   `payment_gateway_account.payment_account`, sans `allow_on_submit`. Une PR soumise garde donc
   son ancien compte même après correction du PGA (LP-IMP-03). Les PR antérieures à LP-12 ne sont
   rattrapées que par le contrôle de `start_attempt` (CA3), et le règlement de leurs sessions
   déjà `Paid` ne l'est que par le préfixe de CA4.
6. **Création automatique du PGA.** `LocalPaymentGateway.on_update()` émet
   `payment_gateway_enabled` quand le Settings est activé. ERPNext réagit avec
   `create_payment_gateway_account()` (`accounts/utils.py:1473-1528`) : il cherche ou crée un compte
   feuille de type `Bank`, nommé comme la passerelle, dans la devise de la société par défaut, puis
   insère le PGA. Il n'attrape que `DuplicateEntryError`. Sans précaution, notre `validate` ferait
   échouer l'enregistrement du Settings sur toute société par défaut dont la devise n'est pas celle
   du Settings, ou si un compte du même nom est un groupe.
7. **Message côté payeur.** `start_attempt` affiche déjà « This payment method is not available at
   the moment. » quand la passerelle est désactivée (`api.py:133-134`). C'est le message générique
   à réutiliser, conformément à `.claude/rules/guest-surface.md`.
8. **Fixtures de test existantes.** `TestSettlePaymentRequest` crée un PGA en INR
   (`_Test Bank - _TC`) pour la passerelle `MTN MoMo-lp-erpnext`, dont le Settings est en XAF
   (valeur par défaut de `make_settings`). Avec CA1, ce PGA serait refusé. Il faut créer le
   Settings de test avec `currency="INR"`. `TestHookPaths` compare aussi la liste exacte des
   `doc_events` de `Payment Request`, et cette liste change.
9. **Le hook ne contient aujourd'hui aucun `try/except`** (ADR 0002 : « The hook has no
   try/except and no commit »). CA4 en impose un autour de `set_as_paid()`, qui relance l'erreur
   après l'avoir enrichie. L'ADR 0002 doit être mise à jour.
10. **Sans ERPNext**, les doctypes `Payment Gateway Account`, `Payment Request` et `Account`
    n'existent pas. Les `doc_events` ne se déclenchent jamais, et une session ne peut pas avoir
    `reference_doctype = "Payment Request"`. Le contrôle de `start_attempt` se règle donc sur ce
    seul test, sans lecture en base.

## 3. Corps du plan

Les étapes se suivent dans cet ordre. Chacune se termine par des tests verts avant de passer à la
suivante.

### Étape 0 — Préparation

- Charger les skills `frappe-syntax-hooks-events`, `frappe-errors-hooks` et `frappe-testing-unit`.
  Lire l'ADR 0002, `.claude/rules/reconcile-transactions.md`, `.claude/rules/guest-surface.md` et
  LP-SEC-01 / LP-IMP-03 dans `docs/security-review-2026-10.md`.
- Context7 (`resolve-library-id`, puis `query-docs`) sur Frappe et ERPNext : `doc_events` sur
  `validate`, `Document._action`, `frappe.db.savepoint` / `rollback(save_point=...)`,
  `raise ... from`, chaînes traduites. CLAUDE.md donne la priorité aux sources installées : chaque
  point Context7 est confirmé dans la source v16, et le fichier est cité.
- Vérifier que le site de dev avec ERPNext (`lp-erpnext`) répond. En cas d'erreur 1045, appliquer le
  correctif connu.

### Étape 1 — Logique pure, testée sans site

Dans `erpnext.py`, une fonction qui ne lit que ses arguments, sur le modèle de
`settlement_action` :

```python
def account_problem(account, company: str, currency: str) -> str | None:
    """Why this account can't take the gateway's payments, or None if it can."""
```

`account` est la ligne `Account` lue par l'adaptateur (ou `None` si le compte n'existe pas). La
fonction renvoie un code, dans cet ordre : `missing`, `group`, `disabled`, `type` (ni `Bank` ni
`Cash`), `company`, `currency`. Elle ne renvoie pas de texte : les messages traduits restent dans
l'adaptateur.

Tests unitaires dans `tests/test_erpnext_rules.py` (`unittest.TestCase`, `SimpleNamespace`, sans
base) : un test par code, un compte valide `Bank` et un compte valide `Cash`, et un test qui
vérifie que le premier problème trouvé l'emporte.

### Étape 2 — Adaptateur `erpnext.py`

Toujours aucun import d'`erpnext` au niveau du module.

- **`payment_account_problem(gateway, account_name, company) -> str | None`.** Renvoie `None` si
  `_is_ours()` est faux. Sinon, lit la devise du Settings
  (`Payment Gateway.gateway_settings` / `gateway_controller`, puis `currency`) et les champs du
  compte par `frappe.db.get_value`. Appelle `account_problem()` et renvoie le message traduit
  correspondant (par exemple « Account {0} is a group account. Choose a Bank or Cash ledger
  account. »). `_is_ours()` accepte tout objet qui a un `payment_gateway`, que ce soit un PGA, une
  PR ou une session. On le réutilise tel quel.
- **`validate_gateway_account(doc, method)`**, `validate` sur `Payment Gateway Account` (CA1) :
  `frappe.throw` si `payment_account_problem(doc.payment_gateway, doc.payment_account, doc.company)`
  renvoie un message.
- **`validate_request_account(doc, method)`**, `validate` sur `Payment Request` (CA2) : ne fait rien
  si `doc._action != "submit"` (le comportement de `_action` en v16 est à confirmer à l'étape 0),
  sinon même contrôle sur `doc.payment_account` et `doc.company`. Un brouillon s'enregistre
  toujours.
- **`request_account_problem(session) -> str | None`**, utilisé par `start_attempt` (CA3) : renvoie
  `None` si `session.reference_doctype != "Payment Request"` (CA5). Sinon, lit `payment_account`
  et `company` de la PR, puis appelle `payment_account_problem`.
- **`on_payment_authorized`** (CA4) : autour de `request.set_as_paid()` uniquement,

  ```python
  try:
      request.set_as_paid()
  except Exception as exc:
      if cause := payment_account_problem(request.payment_gateway, request.payment_account, request.company):
          raise frappe.ValidationError(f"{_('Payment account is not postable')}: {cause} {exc}") from exc
      raise
  ```

  Pas de `commit` et pas de `rollback`, qui restent le travail de `reconcile.authorize()`. La cause
  est recalculée sur le compte de la PR, et le texte d'ERPNext est seulement ajouté à la suite (note
  technique du ticket). `reconcile._record_authorization_failure()` écrit déjà `str(exc)` dans
  `authorization_error`, sans modification.

**`hooks.py`** :

```python
doc_events = {
    "Payment Gateway Account": {
        "validate": "local_payments.erpnext.validate_gateway_account",
    },
    "Payment Request": {
        "validate": "local_payments.erpnext.validate_request_account",
        "on_payment_authorized": "local_payments.erpnext.on_payment_authorized",
        "on_cancel": "local_payments.erpnext.void_open_sessions",
    },
}
```

### Étape 3 — `api.start_attempt` (CA3)

Juste après le contrôle `gateway.enabled`, avant `payer_msisdn()` et `_open_attempt()`, donc
avant toute écriture et tout appel réseau :

```python
if problem := ep.request_account_problem(session):
    frappe.log_error(title="Local Payment account not postable", message=problem,
                     reference_doctype="Local Payment", reference_name=session.name)
    frappe.throw(_("This payment method is not available at the moment."))
```

Points à vérifier : `SESSION_FIELDS` contient déjà `reference_doctype`, `reference_docname` et
`payment_gateway`. L'Error Log ne contient ni numéro de téléphone ni secret. `log_error` doit
survivre au `throw` d'une requête POST : comme pour `_check_with_provider`, on décide entre
`defer_insert=True` et une insertion directe en lisant `frappe/utils/error.py`. Le volume reste
borné par les `rate_limit` existants (10 par token, 30 par adresse, sur 10 min).

### Étape 4 — Création automatique du PGA (`gateway.py`)

Dans `LocalPaymentGateway.on_update()`, autour de `call_hook_method("payment_gateway_enabled", ...)` :
on pose un savepoint et on attrape `frappe.ValidationError` uniquement. Dans ce cas, on revient au
savepoint (ce qui annule aussi le compte que `create_bank_account` aurait pu créer), et
`frappe.msgprint` affiche « Payment Gateway Account not created for {gateway}: {cause}. Create one
manually. ». Toute autre exception remonte. `gateway.py` n'importe toujours rien d'ERPNext, et le
comportement sans ERPNext ne change pas : le hook n'a alors aucun abonné.

Test dans `test_gateway.py` : un hook `payment_gateway_enabled` simulé qui lève une
`ValidationError` laisse le Settings enregistré et produit un message. Un test ERPNext (étape 5)
couvre le cas réel.

### Étape 5 — Tests d'intégration ERPNext

Dans `tests/test_erpnext.py`, sauté sans ERPNext comme aujourd'hui.

Reprise des fixtures (constat 8) : le Settings `lp-erpnext` est créé en `INR` (recréé s'il existe
en XAF), et `TestHookPaths` attend les nouveaux événements. Les comptes de test sont créés dans
`_Test Company` à partir des helpers d'ERPNext, s'il en existe (à vérifier dans
`erpnext/accounts/doctype/account/test_account.py`), et sont nettoyés comme le reste.

| Test | CA |
| ---- | -- |
| PGA refusé pour chacune des cinq formes (groupe, désactivé, type `Expense`, autre société, devise ≠ Settings), chaque fois avec un message qui nomme le compte | CA1 |
| PGA valide `Bank`, puis valide `Cash` : accepté | CA1 |
| PGA d'une passerelle tierce (`_Test Gateway`) sur un compte de groupe : accepté | CA1 |
| Settings activé alors que le PGA automatique serait refusé : Settings enregistré, message affiché, pas de PGA | décision |
| PR soumise sur un PGA dont le compte est devenu invalide (désactivé après coup) : soumission refusée, message lisible, aucune `Local Payment` pour cette PR | CA2 |
| La même PR enregistrée en brouillon : acceptée | CA2 |
| Session `Open` sur une PR dont le compte est devenu invalide : `start_attempt` refusé, `initiate` jamais appelé (`FakeInitiate`), aucune tentative, un Error Log rattaché à la session | CA3 |
| Session `Paid` sur une PR au compte devenu invalide : `authorize()` → `Failed`, `authorization_error` commence par « Payment account is not postable », contient le texte d'ERPNext | CA4 |
| Échec sans problème de compte (période close, test existant) : `authorization_error` sans préfixe | CA4 |

Pour rendre un compte invalide après la soumission de la PR, on écrit directement en base
(`frappe.db.set_value("Account", ..., "disabled", 1)`), puis on vide le cache du document. C'est le
scénario réel : un compte modifié après coup, ou une PR antérieure à LP-12.

CA5 : le job CI sans ERPNext fait tourner toute la suite, plus un test dans `test_start_attempt.py`
qui vérifie que `request_account_problem` ne lit rien en base pour une session dont la référence
n'est pas une PR. Sans ERPNext, `TestHookPaths` vérifie aussi que les nouveaux chemins se résolvent.

### Étape 6 — Documentation

- **ADR 0002**, via le skill `lp-write-adr` : le hook a désormais un `try/except` limité à
  `set_as_paid()`, qui relance toujours l'erreur, et le contrôle de compte s'ajoute à la
  soumission et à la configuration. Il s'agit d'une mise à jour de l'ADR existante, pas d'une
  nouvelle ADR, à moins que le skill n'en juge autrement.
- **ARCHITECTURE.md** :
  - « Rejected by design » : PGA, PR et tentative refusés sur un compte non comptabilisable ;
  - D5 et « Installation and configuration », étape 4 : le compte doit être une feuille `Bank` ou
    `Cash` de la société, dans la devise du Settings. Le PGA automatique peut ne pas être créé ;
  - procédure manuelle de LP-IMP-03 pour les PR antérieures (corriger le PGA, puis
    `payment_account` en console, puis relancer l'autorisation), dans « Installation and
    configuration » ou dans un runbook voisin.
- **`docs/security-review-2026-10.md`** : statut de LP-SEC-01 (recommandations 1 et 3 traitées,
  recommandation 2 traitée par un hook `validate` plutôt que dans `get_payment_url()`, avec la
  raison donnée au constat 4).
- **`locale/fr.po`** : nouvelles chaînes traduites (skill `frappe-core-translation`).

### Étape 7 — Contrat qualité (méthodologie à appliquer)

1. **Bonnes pratiques, sécurité, idiomatisme.** Context7 pour Frappe et ERPNext (`doc_events`,
   `_action`, savepoints, `log_error`, traduction), avec confirmation de chaque point dans la source
   v16 installée et le fichier cité dans la PR. En cas de désaccord, la source installée l'emporte
   (CLAUDE.md).
2. **Code minimal, KISS.** Une fonction pure, quatre petites fonctions d'adaptateur, un bloc
   `try/except` dans le hook, quelques lignes dans `api.py` et dans `gateway.py`. Aucun nouveau
   doctype, champ ou dépendance, aucune écriture dans un document ERPNext. `_is_ours()` est
   réutilisé. Avant d'ajouter un helper, on cherche son équivalent dans frappe, erpnext et payments.
   Par exemple, `erpnext.accounts.utils` n'expose pas de contrôle « compte comptabilisable »
   réutilisable : les contrôles de `gl_entry.py` sont des méthodes de `GLEntry` qui lèvent avec le
   contexte d'une écriture.
3. **Tests unitaires sur la logique pure.** `account_problem()`, toutes branches, sans base. Les
   tests d'intégration couvrent CA1 à CA5.
4. **Première review avant tout fix.** Une fois le code écrit et les tests verts, une review complète
   (skill `code-review`, niveau `high`). Chaque constat est corrigé, et les tests repassent, avant
   d'aller plus loin.
5. **Reviews par domaine**, après la première série de corrections. Chaque constat est corrigé avant
   la review suivante :
   - invariants de paiement, avec `lp-review-invariants` : D2 (aucun appel fournisseur avant le
     contrôle), D4 (le hook ne fait ni commit ni rollback), ADR 0002, invariant 9 (aucune écriture
     comptable), hook limité à nos passerelles ;
   - sécurité, avec `security-review` : surface Guest (message générique, Error Log sans donnée
     personnelle, `rate_limit` inchangés), aucune fuite de texte ERPNext vers le payeur ;
   - idiomatisme Frappe, avec les checklists de `frappe-errors-hooks` et
     `frappe-syntax-hooks-events` : signature `(doc, method)`, `validate` après le contrôleur, pas de
     commit dans un hook, chargement sans ERPNext ;
   - simplicité, avec `simplify` sur le diff ;
   - traductions, avec `frappe-core-translation` pour les nouvelles chaînes et `fr.po`.
6. **Passe `humanizer:humanizer`** sur les commentaires, les docstrings et les passages modifiés des
   docs, pour un ton humain, neutre, concis et fidèle au code. Un commentaire court dit ce que fait le
   code, sans citer ARCHITECTURE, une ADR ou un ticket.

Skills vérifiés comme disponibles, dans ce dépôt (`.claude/skills/`) ou dans la session :
`lp-review-invariants`, `lp-write-adr`, `frappe-syntax-hooks-events`, `frappe-syntax-hooks`,
`frappe-errors-hooks`, `frappe-testing-unit`, `frappe-core-translation`, `code-review`,
`simplify`, `security-review`, `humanizer:humanizer`. Le serveur MCP Context7 est disponible.
Aucun skill ne porte sur la comptabilité ERPNext (`Account`, `GL Entry`) : la seule référence est
la source installée.

**Fini veut dire** : tests purs et tests d'intégration verts dans les deux jobs CI (avec et sans
ERPNext), garde du cœur pur vert, ADR 0002, ARCHITECTURE et security-review à jour, toutes les
reviews closes.

## 4. Points d'intégration avec l'existant

| Élément existant | Interaction LP-12 |
| ---------------- | ----------------- |
| `erpnext._is_ours()` | Réutilisé tel quel pour borner les trois contrôles à nos passerelles. |
| `erpnext.on_payment_authorized()` / `as_administrator()` | `try/except` autour de `set_as_paid()` seulement. L'erreur est toujours relancée, enrichie de la cause. |
| `reconcile.authorize()` / `_record_authorization_failure()` | Inchangés. Ils écrivent déjà `str(exc)` dans `authorization_error`, reviennent au savepoint, comptent l'essai et alertent. |
| `api.start_attempt()` | Un contrôle de plus, après `gateway.enabled`, avant toute écriture ou tout appel. Le message générique existant est réutilisé. |
| `api._check_with_provider()` | Inchangé. Il efface déjà les messages destinés au payeur. |
| `gateway.LocalPaymentGateway.on_update()` | Savepoint et capture de `ValidationError` autour de `payment_gateway_enabled`. |
| `gateway.get_payment_url()` | Inchangé (constat 4). |
| `hooks.py` | Deux nouveaux `doc_events` `validate` (sur `Payment Gateway Account` et `Payment Request`). |
| ERPNext `PaymentGatewayAccount.validate()` | S'exécute avant notre hook et remplit `currency`. |
| ERPNext `PaymentRequest.before_submit()` → `get_payment_url()` | N'est plus atteint si notre `validate` refuse : aucune session n'est créée. |
| ERPNext `create_payment_gateway_account()` | Son insertion peut être refusée par notre hook. Le refus est absorbé par `on_update`. |
| `tests/test_erpnext.py` | Settings de test en INR, `TestHookPaths` mis à jour, nouveaux tests. |
| `tests/test_erpnext_rules.py`, `test_start_attempt.py`, `test_gateway.py` | Nouveaux tests. |
| ADR 0002, ARCHITECTURE, security-review, `fr.po` | Mis à jour à l'étape 6. |

## 5. Risques et inconnues

| Risque / inconnue | Traitement |
| ----------------- | ---------- |
| `Document._action` : nom ou comportement à confirmer en v16, notamment pour une PR insérée directement avec `docstatus = 1`. | À vérifier à l'étape 0 dans `frappe/model/document.py`. Si `_action` n'est pas fiable, se rabattre sur `doc.docstatus == 1`, qui vaut déjà 1 au moment du `validate` d'une soumission. |
| PR soumises avant LP-12 avec un mauvais compte. Leur `payment_account` est figé (constat 5). | `start_attempt` les bloque (CA3), et les sessions déjà `Paid` affichent la cause (CA4). La réparation reste manuelle (procédure LP-IMP-03, documentée à l'étape 6). Pas de réparation automatique (hors ADR 0002). |
| PGA existants déjà invalides : notre hook ne s'exécute qu'à l'enregistrement. | Couverts en aval par CA2 et CA3. Facultatif : lister ces PGA dans la PR, avec une requête en console, pour les signaler au marchand. |
| Compte gelé (`freeze_account = Yes`), hors périmètre. | Le règlement échoue avec le seul texte d'ERPNext, sans préfixe. Noté dans « Known weaknesses ». |
| Le contrôle tourne sans connaître les droits de l'utilisateur : il lit `Account` par `frappe.db`. | Voulu : c'est un contrôle de configuration, et il ne renvoie que des informations que l'utilisateur vient de saisir (nom du compte). |
| Le savepoint de `on_update` absorbe toute `ValidationError` des abonnés de `payment_gateway_enabled`, pas seulement la nôtre. | Accepté. Le message affiché porte la cause réelle, et ERPNext se comporte déjà ainsi quand il ne trouve pas de compte (`msgprint`). Les autres exceptions remontent. |
| Le préfixe de CA4 est traduit : en français, `authorization_error` ne commencera pas par la chaîne anglaise. | Les tests tournent en anglais. Le critère reste vrai dans la langue de l'utilisateur qui a déclenché l'autorisation. À signaler si un outil externe cherche la chaîne anglaise. |
| Le contrôle « type `Bank` ou `Cash` » peut refuser un compte que certains marchands utilisent (par exemple un compte de transit de type vide). | Conforme au ticket et à LP-SEC-01. Le message dit quoi choisir. |
| ERPNext `version-16` bouge sans épinglage en CI. | Un changement du `validate` du PGA ou de l'ordre `validate` → `before_submit` ferait échouer les tests CA1 et CA2, ce qui est le signal voulu. |
