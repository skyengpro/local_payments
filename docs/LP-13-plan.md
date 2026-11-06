# LP-13 — Plan d'implémentation : échanger les payins et les statuts avec l'API Nokash

Statut : plan à valider, rien n'est encore implémenté.
Workstream : Core. Estimation du ticket : non renseignée. Dépendance : EP-01 (socle).
Branche : `lp-13-exchange-payins-and-statuses-with-the-nokash-api`, à jour d'`origin/develop` (`a184856`).
Document de travail : comme les plans LP-11 et LP-12, il n'est pas destiné à être mergé dans `develop`.

## 1. Contexte et objectif

Nokash est un agrégateur : au Cameroun, une seule intégration donne accès à MTN MoMo et à Orange Money.
LP-13 l'ajoute à `local_payments` sous les mêmes règles que la passerelle MTN directe : envoyer un
payin, lire le statut d'une transaction, et traduire chaque réponse de Nokash dans le modèle de
résultat que le reste de l'application utilise déjà. La référence est
`docs/openAPI/nokash-payin-spec.json`.

Critères d'acceptation, numérotés pour les tests. CA1 à CA9 viennent du ticket, CA10 à CA12 des
décisions ci-dessous.

| #    | Critère |
| ---- | ------- |
| CA1  | Un payin pour un numéro, un opérateur et un montant arrive chez Nokash avec sa signature HMAC, qui correspond au vecteur de test de la spec. |
| CA2  | Chaque payin porte l'`attempt_id` comme `order_id`. |
| CA3  | L'id Nokash renvoyé par le payin est enregistré sur la tentative. La réponse au payin ne décide jamais du résultat, seul un contrôle de statut le fait. |
| CA4  | Un payin répondu `REQUEST_BAD_INFOS` est un refus : rien n'est créé et le payeur peut réessayer. |
| CA5  | Un payin sans réponse, en timeout ou en erreur serveur a un résultat inconnu, jamais un échec. La tentative reste ouverte jusqu'à son échéance locale, puis elle est signalée au support. |
| CA6  | Les statuts sont traduits selon la table du ticket. `statusReason` est conservé pour tout résultat autre qu'un succès. |
| CA7  | Un contrôle de statut répondu autrement que `REQUEST_OK`, ou illisible, laisse la tentative inchangée et remonte une erreur : c'est le contrôle qui a échoué, pas le paiement. |
| CA8  | Les clés Nokash et la signature n'apparaissent dans rien de ce qui est journalisé ou renvoyé. |
| CA9  | L'échange est testé sur réponses enregistrées pour chaque cas de la spec. |
| CA10 | Sur une passerelle qui propose les deux opérateurs, le payeur choisit le sien sur la page. Un opérateur inconnu ou non proposé est refusé avant tout enregistrement et tout appel. |
| CA11 | Une tentative sans id Nokash déclenche une alerte aux managers dès son échéance, une seule fois, même si la session est `Paid`. Elle n'est plus interrogée ensuite. |
| CA12 | Une passerelle Nokash n'accepte que le XAF. La devise n'est pas configurable. |

Décisions prises pendant l'analyse (réponses du 2026-11-06) :

| Sujet | Décision |
| ----- | -------- |
| Périmètre | Client pur, doctype `Nokash Settings`, branchement sur `start_attempt` et `reconcile`. |
| Callback | Prévu seulement pour la production : Nokash ne l'envoie pas en sandbox. On réserve son emplacement sans écrire l'endpoint (étape 3 et étape 9). |
| Choix de l'opérateur | Par le payeur, sur la page. Choisir un opérateur à l'intérieur d'une passerelle n'est pas le choix entre passerelles exclu par ARCHITECTURE. Sa table de périmètre doit le dire. |
| Tentative sans id à l'échéance | `Unresolved` et alerte immédiate, même si la session est `Paid`, sans polling inutile. |
| Écarts aux invariants 2 et 5 | ADR 0003. `lifecycle.py` reste strict. L'authentification du statut devient un prérequis de production. |
| Devise | Fixée à XAF pour le contexte camerounais. Aucun champ à renseigner. |

Choix que j'ai faits dans ce plan, à contester à la relecture :

| Sujet | Choix | Pourquoi |
| ----- | ----- | -------- |
| Préfixe et longueur du numéro | Fixés aussi (`237`, 9 chiffres), comme la devise | Même raison : la spec ne couvre que `CM`. Un champ de moins à mal saisir. |
| Montant minimum | Pas de contrôle local. Nokash refuse, et CA4 s'applique | Le minimum peut changer : sans valeur locale, il n'y a rien à mettre à jour. Coût : voir risque R8. |
| `provider_status` | `FAILED: Insufficient customer balance`, via `describe()` | Même forme que MTN. Le `statusReason` y reste mot pour mot, et le statut brut reste lisible. |
| `user_name`, `user_email` | Jamais envoyés | Optionnels, et ce sont des données personnelles dont Nokash n'a pas besoin. |
| Environnement | Pas de champ `environment` | La spec ne donne qu'une URL de base, et `api_base_url` reste configurable si la production en a une autre. |

## 2. Ce que l'analyse a établi

Vérifié dans le code d'`origin/develop` et dans les sources installées : frappe `2871914b69`,
payments `cca07d9`.

1. **L'id Nokash n'a aucun chemin jusqu'à la tentative.** `Initiated` ne porte que `status` et
   `integration_request` (`gateway.py:41`), et `_record_start` n'écrit que ces deux informations
   (`api.py:206`). `_record` n'écrit `provider_transaction_id` que sur un succès (`reconcile.py:276`).
   MTN s'en passe : l'`attempt_id` lui sert de clé de statut. Pour Nokash, l'id est la seule clé.
2. **`_record` écrase l'id sur un succès** (`reconcile.py:279`). `check_status` doit donc toujours
   renvoyer l'id interrogé dans `transaction_id`, même si `data.id` manque.
3. **Le 404 ne se copie pas de MTN.** Chez MTN, un 404 sur le statut donne `Failed`
   (`mtn_momo.py:209`). Chez Nokash, c'est « check failed, attempt unchanged » (CA7).
4. **Sans devise dans la réponse, tout succès finit en `amount_mismatch`.** `currencies_match` refuse
   une valeur absente (`lifecycle.py:141`). Le client doit renseigner `currency` avec la devise fixée.
5. **Les clés partent dans le corps, pas dans les en-têtes.** `log_exchanges` de MTN journalise
   `request_body` tel quel dans l'Integration Request (`mtn_momo_settings.py:161`). Copié, ce code
   écrirait `i_space_key` et `app_space_key` en base. Le client Nokash doit remettre un `Exchange`
   déjà nettoyé.
6. **`requests` suit les redirections par défaut.** Un 307 ou un 308 renverrait le corps, donc les
   clés, vers une autre adresse. Le payin doit passer `allow_redirects=False`. À confirmer via
   Context7 à l'étape 0.
7. **Le montant de session est un float.** La signature exige `1000`, jamais `1000.0`. Le corps et la
   chaîne signée doivent partir de la même valeur entière.
8. **`payer_msisdn` renvoie `237…`**, alors que Nokash attend le numéro national. La tentative garde
   le numéro complet, et seul le client retire le préfixe.
9. **Une tentative sans id n'a rien à interroger, mais elle doit quand même atteindre son échéance.**
   `resolve` ne passe à `Unresolved` que sur une réponse `PENDING` (`lifecycle.py:163`). Si
   `check_status` levait une erreur, la tentative resterait `Initiated` indéfiniment. Il doit donc
   répondre `PENDING` sans appel réseau, avec une marque qui dit que rien n'a pu être vérifié.
10. **Une date vide se lit comme échue.** Frappe compare les dates de filtre via `ifnull()`
    (`scheduler.py:44`). Mettre `next_check_on` à vide sur un `Unresolved` le ferait sélectionner à
    chaque heure, sauf à ajouter `["next_check_on", "is", "set"]` comme le fait déjà
    `retry_authorizations` (`scheduler.py:87`).
11. **L'alerte des 72 h ne couvre pas le cas qui compte.** `_alert_unresolved` se tait si la session
    est `Paid` (`scheduler.py:141-144`), c'est-à-dire justement en cas de double débit.
12. **Correction de ce que j'ai écrit dans la question 3.** `Unresolved` n'empêche pas une nouvelle
    tentative côté serveur (`lifecycle.py:46`), mais la page ne la propose pas : `waiting` inclut
    `Unresolved` et masque le formulaire (`local_payment_checkout.py:37`). Le double débit reste
    possible par un appel direct à l'API, pas depuis la page. La conséquence pour le payeur est dans
    le risque R3.
13. **Les devises et le numéro se lisent sur l'objet, pas en base.** Le mixin lit `self.currency`,
    `self.msisdn_prefix` et `self.msisdn_national_length`. Rien dans `erpnext.py`, `payments` ou
    ERPNext ne les lit comme colonnes du Settings. Des attributs de classe suffisent.
14. **`erpnext.py` ne reconnaît que `SETTINGS_DOCTYPES`** (`gateway.py:27`). Sans `Nokash Settings`
    dans ce tuple, une Payment Request payée par Nokash ne se finalise pas, sans aucune erreur.
15. **Le vecteur HMAC de la spec se recalcule à l'identique**
    (`2c464aaa…168ba3`). Il a été produit par la même formule et n'est pas officiel : il prouve la
    cohérence du code avec la spec, pas avec Nokash.

Skills vérifiés pour cette session :

| Usage | Skill | Où |
| ----- | ----- | -- |
| Ajout d'une passerelle | `lp-add-provider` | suivi dans le repo |
| Revue des invariants | `lp-review-invariants` | suivi dans le repo |
| ADR 0003 | `lp-write-adr` | suivi dans le repo |
| Doctype, page, tests Frappe | `frappe-syntax-doctypes`, `frappe-syntax-whitelisted`, `frappe-testing-unit`, etc. | installés localement, ignorés par git |
| Revues et ton | `code-review`, `security-review`, `simplify`, `humanizer:humanizer` | session |

`frappe-core-cache` n'est pas installé et n'est pas nécessaire : Nokash n'a pas de jeton à mettre en
cache. Deux skills demandent un arbitrage :

- `lp-add-provider` interdit d'écrire le client tant qu'il reste des éléments à obtenir. Les
  `open_points` de la spec sont donc rangés en « points à confirmer avant production », et le
  développement avance sur la spec, comme le demande le ticket.
- `lp-write-adr` et CLAUDE.md parlent de documents en français, alors qu'ARCHITECTURE, les ADR et
  `gateways/` sont en anglais. Je suis les fichiers réels : ADR 0003 et `nokash.md` seront en anglais.

## 3. Corps du plan

### Étape 0 — Préparation

- Committer `docs/openAPI/nokash-payin-spec.json`, qui n'est pas suivi.
- Recréer un site de test : les sites `lp-*` ont été supprimés le 2026-10-12. Il en faut un avec
  ERPNext et un sans.
- Lancer les tests avant toute modification, pour avoir un point de départ.
- Context7 sur `requests` : timeouts, `allow_redirects`, encodage de `params`. Sur `hmac` et `hashlib`
  si Context7 les couvre, sinon la documentation Python. Frappe se vérifie dans les sources, pas dans
  Context7 (CLAUDE.md).

### Étape 1 — Décision et contrat, avant le code

- **`docs/decisions/0003-nokash-aggregator.md`**, statut `Proposed`, via `lp-write-adr`. Il couvre :
  - Nokash comme agrégateur : une passerelle, plusieurs opérateurs ;
  - l'invariant 5 : devise non confirmée par Nokash, fixée côté application ;
  - l'invariant 2 et D3 : statut non authentifié en sandbox. La réponse reste une réponse à un appel
    que le site émet en TLS vers une adresse configurée, donc un tiers ne peut pas la forger. Reste
    que quiconque connaît l'id peut la lire. L'authentification est un prérequis de production ;
  - l'id perdu : ce qui reste non résolu automatiquement, et ce que le callback permettra plus tard.
- **`docs/gateways/nokash.md`**, mêmes titres que `mtn-momo.md` : configuration, opérations,
  correspondance des statuts, erreurs connues, points à confirmer, références avec leur portée
  (Cameroun, sandbox).

Ta validation de l'ADR est requise avant le merge. Le code peut avancer en parallèle.

### Étape 2 — Mettre en commun le code pur

Nouveau `local_payments/providers/common.py`, déplacé depuis `mtn_momo.py` sans changer son
comportement : `InvalidRequest`, `Exchange`, `DEFAULT_TIMEOUT`, `RESPONSE_LOG_MAX_LENGTH`,
`PROVIDER_STATUS_MAX_LENGTH`, `describe()`, `as_text()`, et `whole_amount()` qui renvoie un entier.
`format_amount()` de MTN devient `str(whole_amount(amount))`.

Les tests MTN doivent rester verts sans changement. Si le déplacement en demande davantage, c'est le
signe d'un changement de comportement à reprendre.

### Étape 3 — Client pur `local_payments/providers/nokash.py`

Constantes : `CURRENCY = "XAF"`, `COUNTRY = "CM"`, `PAYMENT_TYPE = "CM_MOBILEMONEY"`,
`MSISDN_PREFIX = "237"`, `NATIONAL_LENGTH = 9`, `OPERATORS`, `STATUSES`.

Types :

- `NokashConfig(api_base_url, i_space_key, app_space_key)`, les deux clés en `field(repr=False)` ;
- `NokashData(operator)`, le contenu de `provider_data` (D8) ;
- `Initiation(outcome, transaction_id, http_status, code)`, avec `InitiationOutcome` à trois valeurs :
  `ACCEPTED`, `REJECTED`, `UNKNOWN` ;
- `NokashError(kind, http_status, code)`, pour un contrôle de statut sans réponse exploitable.

`sign(order_id, amount, user_phone, app_space_key, i_space_key)` : fonction pure, HMAC-SHA256,
hexadécimal minuscule.

`NokashClient.payin(attempt_id, amount, msisdn, operator, callback_url=None) -> Initiation` :

| Réponse | Résultat |
| ------- | -------- |
| Exception réseau, timeout | `UNKNOWN` |
| HTTP 5xx, quel que soit le corps | `UNKNOWN` |
| Corps illisible, ou redirection | `UNKNOWN` |
| `REQUEST_OK` avec `data.id` | `ACCEPTED` et id. `data.status` est ignoré, même final |
| `REQUEST_OK` sans `data.id` | `UNKNOWN` |
| `REQUEST_BAD_INFOS` | `REJECTED`, quel que soit le code HTTP sous 500 |
| Toute autre valeur | `UNKNOWN` |

Montant ou numéro invalide : `InvalidRequest` avant tout appel, comme MTN. Aucune reprise automatique,
car l'effet d'un `order_id` répété n'est pas connu. `callback_url` est l'emplacement réservé au
callback : la clé n'est envoyée que si elle est fournie, et LP-13 ne la fournit pas.

`NokashClient.check_status(transaction_id) -> ProviderResult` : `POST` avec
`params={"transaction_id": …}`, sans corps.

| Réponse | Résultat |
| ------- | -------- |
| `REQUEST_OK`, statut connu | `ProviderResult` selon la table du ticket |
| `REQUEST_OK`, statut inconnu ou `data` illisible | `NokashError` |
| `REQUEST_BAD_INFOS`, y compris le 404 | `NokashError` |
| Autre valeur, 5xx, réseau, corps illisible | `NokashError` |

`ProviderResult` renvoie `amount` tel que lu, `currency=CURRENCY`, `transaction_id` égal à l'id
interrogé, et `provider_status=describe(status, statusReason)`.

Secrets (CA8) : l'`Exchange` reçoit un corps sans les deux clés et des en-têtes sans `hmac-signature`.
Le texte de réponse et `message` passent par une fonction qui remplace les deux clés si Nokash les
renvoie en écho. L'en-tête se construit dans l'appel, sans variable locale, et la clé HMAC locale
porte un nom contenant `secret`, que Frappe masque dans les traces (`frappe/utils/__init__.py:344`).
Seul le payin est rapporté à `on_exchange` : ARCHITECTURE ne journalise pas les contrôles de statut.

Tests : `local_payments/tests/test_nokash.py`, sans site, sur `tests/fixtures/nokash.json`. Détail à
la section 4.

### Étape 4 — Tentative sans id : `lifecycle.py`, `reconcile.py`, `scheduler.py`

- `ProviderResult` reçoit `no_reference: bool = False` : aucune question n'a pu être posée au
  fournisseur. `resolve()` ne change pas.
- `reconcile._record`, sur un résultat `no_reference` dont la tentative est `Unresolved` :
  `next_check_on = None`, et si `alerted` vaut 0, le passer à 1 et lever l'alerte `no_reference`.
- `alert_managers` reçoit le sujet : « Attempt {1} on {0} got no answer from the provider and cannot
  be checked. The payer may have been charged. Ask the provider. »
- `scheduler.check_unresolved_attempts` : filtre `["next_check_on", "is", "set"]`.

La condition couvre tout passage dans `_record`, pas seulement le premier : le polling de la page
continue d'appeler `reconcile()` et ne doit ni remettre une date, ni alerter deux fois. Les alertes de
`reconcile()` partent déjà une fois sans reprise, celle-ci suit la même règle. `row.alerted` empêche
aussi l'alerte des 72 h de doubler.

### Étape 5 — Contrat commun : `gateway.py`, `api.py`

`gateway.py` :

- `SETTINGS_DOCTYPES` reçoit `"Nokash Settings"` ;
- `Initiated` reçoit `transaction_id: str | None = None` ;
- `initiate(attempt_id, session, msisdn, provider_data)`, symétrique de `check_status` ;
- `payer_operators() -> list[dict]`, vide par défaut, et `attempt_data(operator) -> dict`, `{}` par
  défaut ;
- montent de `MTNMoMoSettings` vers le mixin, sans changement de comportement :
  `validate_gateway_name_unchanged`, `set_pending_timeout`, `log_refusal`, `log_exchanges`,
  `warn_if_no_alert_manager`. Le test de la condition `Production` reste dans MTN, qui appelle le
  mixin seulement dans ce cas ;
- la docstring du mixin parle d'attributs, plus de champs.

`api.py` :

- `start_attempt(token, msisdn, operator=None)` ;
- `_start` appelle `gateway.attempt_data(operator)` juste après `payer_msisdn`, donc avant
  `_open_attempt`, et passe le résultat à `_open_attempt` et à `initiate` ;
- `_open_attempt` écrit `provider_data`, pour que l'opérateur soit en base avant l'appel ;
- `_record_start` écrit `provider_transaction_id` s'il est fourni, en dehors du
  `if row.status == lc.INITIATED` : un contrôle a pu faire avancer la tentative entre-temps.

`MTNMoMoSettings.initiate` ignore le nouveau paramètre. Les faux de `test_start_attempt.py` prennent
un argument de plus.

### Étape 6 — Doctype `Nokash Settings`

Non Single, nommé par `gateway_name`, réservé à System Manager, comme D5 le demande. Le contrôleur
hérite de `LocalPaymentGateway` avec `provider_name = "Nokash"`.

| Champ | Type | Rôle |
| ----- | ---- | ---- |
| `gateway_name` | Data, requis | Nom du contrat. Figé après création. |
| `enabled` | Check | Décoché par défaut. |
| `api_base_url` | Data, requis | Par défaut `https://api.nokash.app`. `https://` obligatoire : les clés y sont envoyées. |
| `i_space_key` | Password, requis | Clé intégrateur, aussi clé HMAC. |
| `app_space_key` | Password, requis | Clé d'application. |
| `accept_mtn_momo` | Check | Coché par défaut. |
| `accept_orange_money` | Check | Coché par défaut. Au moins un des deux est requis. |
| `pending_timeout_minutes` | Int | Délai avant `Unresolved`, 15 par défaut. |

`currency`, `msisdn_prefix` et `msisdn_national_length` sont des attributs de classe tirés des
constantes de `providers/nokash.py`.

Méthodes :

- `initiate` : appelle le payin et renvoie `Initiated` avec l'id. `ACCEPTED` et `UNKNOWN` donnent
  `lc.INITIATED`, `REJECTED` et `InvalidRequest` donnent `lc.ERROR` avec `log_refusal`. L'Integration
  Request passe `Completed` sur `ACCEPTED`, `Failed` sinon ;
- `check_status` : lit `provider_transaction_id` sur la tentative. Sans id, renvoie
  `ProviderResult(lc.PENDING, provider_status="No Nokash transaction id", no_reference=True)` sans
  appel réseau. La signature de `check_status` ne change pas, et rien n'est dupliqué dans
  `provider_data` ;
- `payer_operators` et `attempt_data` : les opérateurs cochés, et la validation du choix. Un seul
  opérateur coché : il est choisi d'office. Choix absent ou non proposé : `frappe.throw`, avec un
  message affiché sous le champ.

### Étape 7 — Page de paiement

- `get_context` passe `operators`, lus via le contrôleur. La page reste sans effet de bord, et ce
  chargement ne déchiffre aucun mot de passe.
- Avec deux opérateurs ou plus, le formulaire affiche un groupe de boutons radio, sans valeur
  présélectionnée pour éviter un push vers le mauvais opérateur. Avec un seul ou aucun, la page reste
  identique à celle de MTN.
- Le JS envoie `operator` et refuse un envoi sans choix avant l'appel, comme pour le numéro vide : un
  appel consommerait le budget de `rate_limit`.
- Les libellés viennent de la spec : « MTN Mobile Money » et « Orange Money ».

### Étape 8 — Traductions et garde-fous

- `locale/main.pot` et `locale/fr.po` : opérateurs, nouveaux messages, alerte, libellés du doctype.
- `.gitleaks.toml` : règles pour `i_space_key` et `app_space_key`. Le vecteur de test vit dans la
  fixture, déjà en liste blanche.

### Étape 9 — Documentation

- `ARCHITECTURE.md` :
  - périmètre : la ligne exclue devient « Choice between gateways by the payer on the payment page ».
    Une phrase précise qu'une passerelle peut proposer plusieurs opérateurs ;
  - composants, D1, D3, modèle de données, passage sur `order_id` et `attempt_id`, cycle de vie ;
  - tables `Security` (la page montre aussi le choix d'opérateur) et `Entry points` : `start_attempt`,
    et une ligne `nokash_callback` marquée prévue, pour la production ;
  - `Known weaknesses` : id perdu, devise non confirmée, statut non authentifié ;
  - `Scheduled tasks` : l'alerte `no_reference` à l'échéance, et les documents liés.
- `CLAUDE.md` : doctypes, ligne `providers/nokash.py`, renvoi à l'ADR sur les invariants 2 et 5.
- `.claude/rules/guest-surface.md` : « The page shows only… » inclut le choix d'opérateur.
- `gateways/orange-money.md` : Orange via Nokash ne lève pas les prérequis de l'intégration directe.
- `README.md` : configuration de la passerelle.

### Étape 10 — Contrat qualité (méthodologie à appliquer)

1. **Bonnes pratiques.** Chaque usage de `requests`, `hmac` et `hashlib` est vérifié via Context7 pour
   la sécurité et l'idiomatisme, avant d'écrire le code concerné. Frappe se vérifie dans les sources
   v16 installées, en citant le fichier.
2. **Code minimal, KISS.** Pas d'abstraction de plus que ce plan. Chaque helper est d'abord cherché
   dans `frappe`, `erpnext` et `payments`. Aucune nouvelle dépendance. Une passe `simplify` clôt
   l'implémentation.
3. **Tests unitaires sur la logique pure.** Tout le client est testé sans site, sur réponses
   enregistrées. Seuls les tests utiles : pour chaque test, nommer celui qui couvre déjà le chemin ; si
   l'un le couvre, l'étendre ou s'abstenir. Tables de cas plutôt que tests quasi identiques.
4. **Première review avant tout fix.** Le code terminé, une review complète (`code-review`) liste tous
   les constats sans rien toucher. Les fixes s'appliquent ensuite, tous, avant de continuer.
5. **Review dédiée par domaine**, chacune fixée avant la suivante :

   | Domaine | Fichiers | Outil |
   | ------- | -------- | ----- |
   | Cœur pur | `providers/`, `lifecycle.py` | `lp-review-invariants`, invariants 5, 6, 7 |
   | Réconciliation | `reconcile.py`, `scheduler.py` | `lp-review-invariants`, invariants 2 à 5 |
   | Surface invité | `api.py`, page | `lp-review-invariants`, invariants 1, 2, 7, 8 |
   | Secrets et sécurité | tout le diff | `security-review`, greps du skill, CA8 |
   | Doctype et contrat | `Nokash Settings`, `gateway.py` | `lp-review-invariants`, skills `frappe-*` |
   | Documentation | ADR, `nokash.md`, ARCHITECTURE | relecture contre le code livré |

6. **Passe `humanizer:humanizer`** sur les commentaires et docstrings : ton humain, neutre, concis et
   fidèle au code. Une ligne par commentaire, sans référence à un ticket, un ADR ou ARCHITECTURE.
7. **Terminé** signifie : tests purs et tests de site verts, avec et sans ERPNext, garde
   `scripts/check_pure_core.py` verte, documents à jour, ADR 0003 accepté.

## 4. Points d'intégration avec l'existant

| Fichier | Changement | Effet sur MTN |
| ------- | ---------- | ------------- |
| `providers/common.py` | Nouveau, code déplacé | Aucun |
| `providers/mtn_momo.py` | Importe depuis `common.py` | Aucun |
| `providers/nokash.py` | Nouveau | Aucun |
| `lifecycle.py` | `ProviderResult.no_reference` | Aucun, valeur par défaut |
| `reconcile.py` | `_record`, sujet d'alerte | Aucun, branche inactive |
| `scheduler.py` | Filtre `is set` | Aucun : MTN garde toujours une date |
| `gateway.py` | Voir étape 5 | Déplacement de méthodes |
| `api.py` | `operator`, `provider_data`, id | Paramètre optionnel, `{}` par défaut |
| `mtn_momo_settings.py` | Méthodes montées dans le mixin, `initiate` | Comportement identique |
| `nokash_settings/` | Nouveau doctype | Aucun |
| Page de paiement | Groupe opérateurs | Aucun : liste vide |
| `erpnext.py` | Aucun, suit `SETTINGS_DOCTYPES` | Aucun |

Réutilisés sans changement : `resolve()`, `_claim`, `authorize`, `normalize_msisdn`, `Integration
Request`, le cycle de vie et le mécanisme `alerted`.

Aucun patch n'est nécessaire : aucune colonne n'est ajoutée à une table existante.

Tests prévus, tous rattachés à un critère :

| Fichier | Ce qu'il vérifie | CA |
| ------- | ---------------- | -- |
| `tests/test_nokash.py` | Signature contre le vecteur, corps envoyé (entier, numéro national, `order_id`) | 1, 2 |
| | Table des réponses au payin, y compris un `data.status` final ignoré | 3, 4, 5 |
| | Table des statuts et des `statusReason` | 6 |
| | Table des contrôles en échec, 404 compris | 7 |
| | Aucun secret dans `Exchange`, ni sur écho de Nokash ; pas de redirection | 8 |
| `tests/test_reconcile.py` | Sans id : `Pending`, puis `Unresolved` et une seule alerte, session `Paid` comprise, sans nouvelle date | 5, 11 |
| | Un succès Nokash marque la session `Paid` et garde l'id | 3, 6 |
| `tests/test_scheduler.py` | Un `Unresolved` sans date n'est pas sélectionné | 11 |
| `tests/test_start_attempt.py` | Id enregistré, y compris si la tentative a déjà avancé ; opérateur refusé avant écriture ; `provider_data` en base avant l'appel | 3, 10 |
| `nokash_settings/test_nokash_settings.py` | `initiate` et `check_status` avec et sans id ; Integration Request sans secret ; devise autre que XAF refusée ; au moins un opérateur ; URL en `https://` | 3, 5, 8, 10, 12 |
| `tests/test_checkout.py` | Radios à deux opérateurs, aucun à un seul ou sur MTN | 10 |

`test_mtn_momo_settings.py:206` patche `get_users_with_role` sur le contrôleur MTN : la cible du patch
suit la méthode dans le mixin. C'est le seul test MTN à modifier, avec les faux d'`initiate`.

Les réponses enregistrées sont reconstruites depuis la spec, comme pour MTN : il n'y a pas de capture
réelle. `PENDING`, `CANCELED` et `TIMEOUT` sur le statut sont dérivés des exemples `SUCCESS` et
`FAILED` et de `status_reasons_observed`. Le champ `_source` de la fixture le dit.

## 5. Risques et inconnues

### Risques

| # | Risque | Effet | Parade dans ce plan |
| - | ------ | ----- | ------------------- |
| R1 | Statut non authentifié en sandbox, peut-être en production | Quiconque connaît l'id peut lire le statut. Écart à l'invariant 2 | ADR 0003, prérequis de production |
| R2 | Devise non confirmée par Nokash | `confirmed_currency` porte une valeur que Nokash n'a pas confirmée | ADR 0003, et le contrat ne couvre que XAF |
| R3 | Id perdu alors que le payeur a validé | L'argent est pris et rien ne le confirme. Le payeur reste sur « waiting », sans formulaire. La session ne se résout pas seule | Alerte immédiate. Le support traite avec Nokash, et le consommateur émet un nouveau lien. Aucun moyen dans l'application de saisir l'id à la main (hors périmètre) |
| R4 | Crash ou erreur de `_record_start` après un payin accepté | Même effet que R3 : l'Integration Request disparaît avec le rollback, donc l'id aussi | Même parade. Fenêtre très courte, et le `order_id` reste chez Nokash |
| R5 | Un contrôle de statut échoue en continu, par exemple un id inconnu de Nokash | CA7 laisse la tentative inchangée : elle reste en vol sans échéance, bloque la session, écrit une Error Log à chaque passage, et sa `next_check_on` ne bouge pas, donc elle reste en tête de lot. MTN a la même limite sur un 401 durable, mais son 404 clôt la tentative | Conforme au ticket, donc non traité ici. Je propose un ticket séparé pour appliquer l'échéance après N échecs |
| R6 | Clés invalides ou expirées | Réponse probablement hors `REQUEST_BAD_INFOS` : résultat inconnu, puis une alerte par tentative | Les alertes rendent le problème visible. Forme de la réponse à confirmer |
| R7 | Opérateur choisi différent de celui du numéro | Réponse de Nokash inconnue, sans doute `FAILED` | Le payeur réessaie. Pas de contrôle de préfixe local |
| R8 | Session sous 200 XAF | Le lien ne peut jamais aboutir, et le payeur voit le message générique | Accepté pour LP-13. `validate_minimum_transaction_amount()` si le cas se présente |
| R9 | Client testé uniquement sur la spec | L'API réelle peut différer d'une sandbox simulée | Validation réelle dès que des clés appelables existent, comme LP-10 pour MTN |

### Points à confirmer avec Nokash avant production

Ils ne bloquent pas le développement sur réponses enregistrées. Ils iront dans `gateways/nokash.md`.

| Point | Hypothèse | Effet si elle est fausse |
| ----- | --------- | ------------------------ |
| Authentification du statut en production | Aucune, comme en sandbox | Ajouter les en-têtes dans `check_status` |
| Signature du callback | Inconnue | À trancher avec le ticket du callback |
| Recherche d'une transaction par `order_id` | Non disponible | Si elle existe, R3 se résout automatiquement |
| `order_id` répété | Inconnu, donc aucune reprise | Aucun effet tant qu'il n'y a pas de reprise |
| Numéro avec indicatif | Non, numéro national | Une ligne dans le client |
| Vecteur HMAC officiel | Vecteur calculé par nous | Remplacer la fixture, revoir la formule s'il diffère |
| Minimum réel | 200 XAF, non codé localement | Aucun |
| Délai de `TIMEOUT` chez Nokash | 15 minutes en local | Ajuster `pending_timeout_minutes` |
| Identifiants sandbox appelables et numéros de test | Non disponibles | Bloque la validation réelle (R9) |

### Hors périmètre

- Endpoint `nokash_callback`. Il est prévu pour la production, et il réduira R3 : son corps porte `id`
  et `orderId`, de quoi retrouver un id perdu. Le corps reste un déclencheur, et la preuve reste le
  contrôle de statut qui suit.
- Remboursements et décaissements.
- Saisie manuelle d'un id par le support.
- Index sur `provider_transaction_id` de la tentative : inutile au volume annoncé.
