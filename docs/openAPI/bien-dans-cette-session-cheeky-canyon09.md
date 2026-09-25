# Plan LP-09 : start_attempt, RequestToPay et callback MTN

## Contexte

La page de paiement affiche la session et interroge `get_status`, mais son champ numéro est
désactivé et rien n'appelle MTN. LP-09 ouvre le chemin du payeur : saisie du numéro,
`start_attempt` crée la tentative puis envoie RequestToPay, la page passe en attente, et le
callback MTN (envoyé une seule fois) sert de déclencheur supplémentaire à `reconcile()`. La
confirmation vient toujours de la requête de statut (D3). Le corps du callback n'est jamais lu.

Déjà en place (LP-06 à LP-08) : `api.find_session`, `_session_or_404`, `payer_status`,
`current_attempt` ; `lc.ensure_can_start_attempt` ; `MtnMomoClient.request_to_pay` → `Initiation`
(`InitiationOutcome`) ; `MTNMoMoSettings.mtn_client()` ; `rc.MIN_CHECK_INTERVAL`,
`rc.CHECKABLE_STATES`, `rc._provider_for` ; le polling de la page ; la colonne
`integration_request` de la tentative.

## Décisions (validées)

| #   | Sujet                   | Décision                                                                                                                                                                                                                                          |
| --- | ----------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| D-1 | Integration Request     | Une IR par tentative, pour l'initiation seulement : RequestToPay et sa relance après 401. Ni l'appel de jeton, ni`Authorization`, ni la clé d'abonnement n'y figurent.                                                                         |
| D-2 | Commit                  | Un seul`frappe.db.commit()` explicite dans `api._open_attempt`, avec `# nosemgrep` justifié. CLAUDE.md « Frappe rules » est mis à jour. Le reste est committé par Frappe en fin de POST.                                                |
| D-3 | Rejet d'initiation      | `Error` + `frappe.log_error` (opération, code HTTP, code MTN, jamais le MSISDN). Pas d'alerte aux managers.                                                                                                                                   |
| D-4 | Saisie MSISDN           | Tolérante : on retire espaces,`.`, `-`, `(`, `)`, puis un `+` ou un `00` en tête. Si seuls les N chiffres nationaux restent, on ajoute le préfixe. On valide ensuite `prefix + N chiffres`.                                       |
| D-5 | `Initiated → Error`  | Ajouté à`ATTEMPT_TRANSITIONS`. La ligne est écrite `Initiated` avant l'appel (AC), donc un rejet part de `Initiated`. `resolve()` ne produit jamais `Error` (absent de `PROVIDER_STATUSES`). Le diagramme ARCHITECTURE est aligné. |
| D-6 | Passerelle désactivée | `start_attempt` refuse, comme `get_payment_url` (« A disabled gateway refuses new payments »).                                                                                                                                               |
| D-7 | Callback                | Mis en file uniquement pour une tentative connue et encore vérifiable (`CHECKABLE_STATES`). `job_id` par tentative + `deduplicate=True`, pour que des callbacks répétés n'empilent pas de jobs.                                          |

Vérifié dans les sources v16 (16.32.0) :

- `frappe/rate_limiter.py:104` : avec `key` et `ip_based=True`, le compteur porte sur `ip:key`. Pour avoir un compteur par token ou par tentative quelle que soit l'adresse, il faut `ip_based=False`.
- `frappe/integrations/utils.py:create_request_log` fait un commit, donc on ne l'utilise pas : l'IR est insérée directement.
- `frappe/utils/background_jobs.py:enqueue` : `job_id` + `deduplicate`.
- `frappe/public/js/frappe/request.js` : avec `silent: true`, le message n'ouvre pas de fenêtre. Le callback `error(r)` reçoit `r._server_messages` sur un 417.
- `frappe/auth.py:validate_csrf_token` : aucun contrôle sans jeton CSRF en session, ce qui couvre le callback de MTN.

## Changements

### Cœur pur

**`lifecycle.py`** : `INITIATED` accepte aussi `ERROR`. Un commentaire précise « initiation
refusée, décidée localement ».

**`providers/mtn_momo.py`**

- `Exchange` (dataclass figée) : `method`, `url`, `request_headers`, `request_body`, `status_code`,
  `response_body` (tronqué à `RESPONSE_LOG_MAX_LENGTH = 2000`), `error` (nom de l'exception quand
  aucune réponse n'est revenue).
- `MtnMomoClient(..., on_exchange: Callable[[Exchange], None] | None = None)`. `_call` le notifie
  à chaque envoi, relance après 401 comprise, et aussi sur `RequestException` avant de la relancer.
  Les en-têtes journalisés sont seulement `X-Target-Environment` et ceux de l'opération
  (`X-Reference-Id`, `X-Callback-Url`). `_token()` n'est jamais notifié.

### Adaptateurs Frappe

**`gateway.py`**

- Docstring du contrat : les Settings ont aussi `msisdn_prefix`, `msisdn_national_length` et
  `pending_timeout_minutes`, et implémentent `initiate`.
- `@dataclass(frozen=True) class Initiated: status: str; integration_request: str | None = None`.
- `LocalPaymentGateway.payer_msisdn(raw) -> str` : normalisation D-4 puis validation. Sinon
  `frappe.throw(_("Enter your mobile money number: {0} digits, with or without +{1}."))`.
- `LocalPaymentGateway.initiate(attempt_id, session, msisdn) -> Initiated` : `NotImplementedError`.

**`mtn_momo_settings.py`**

- `INITIATION_STATUSES` : `ACCEPTED`, `DUPLICATE_REFERENCE` et `UNKNOWN` donnent `Initiated` ;
  `REJECTED`, `UNAUTHORIZED` et `NOT_SENT` donnent `Error`.
- `mtn_client(on_exchange=None)` transmet le hook.
- `initiate()` appelle `request_to_pay(attempt_id, session.amount, msisdn, external_id=session.name, payer_message, payee_note, callback_url)`. `callback_url` n'est fourni que si `send_callback`
  est coché : `get_url("/api/method/local_payments.api.mtn_momo_callback?" + urlencode({"attempt": attempt_id}))`,
  jamais le token. `InvalidRequest` donne `Error` sans appel HTTP. Sur `Error`, `frappe.log_error`
  (sans MSISDN). Renvoie `Initiated(status, self._log_exchanges(...))`.
- `_log_exchanges(attempt_id, session_name, exchanges, failed)` : aucune IR si aucun échange
  (`NOT_SENT`). Sinon, insertion directe d'une `Integration Request` (`ignore_permissions`) :
  service `MTN MoMo`, `request_id = attempt_id`, référence `Local Payment`/session, `url`,
  `request_headers`, `data` = corps, `output` = liste `[{status_code, body | error}]` (une entrée
  par envoi), `status` = `Completed` pour un 202 ou un 409, `Failed` sinon.

**`api.py`**

- Constantes de rate limit, avec un commentaire sur le choix des valeurs :
  - `start_attempt` : `rate_limit(key="token", limit=5, seconds=600, ip_based=False)` borne les
    push sur le téléphone d'un payeur, `rate_limit(limit=30, seconds=600)` borne une adresse.
  - callback : `rate_limit(key="attempt", limit=10, seconds=60, ip_based=False)` et
    `rate_limit(limit=600, seconds=60)`, large parce que MTN appelle depuis peu d'adresses.
- `start_attempt(token, msisdn)`, `@frappe.whitelist(allow_guest=True, methods=["POST"])` :
  1. `_session_or_404(token)` : même refus que `get_status`.
  2. `gateway = rc._provider_for(session.payment_gateway)`. Refus si désactivée (D-6).
  3. `msisdn = gateway.payer_msisdn(msisdn)`, avant toute écriture et tout appel.
  4. `attempt_id = _open_attempt(session.name, msisdn, gateway.pending_timeout_minutes)` :
     `get_doc(..., for_update=True)`, refus si pas `Open`, `lc.ensure_can_start_attempt` traduit
     `AttemptInProgress` en message payeur, puis ajout d'une ligne : `attempt_id = str(uuid.uuid4())`,
     `status` `Initiated`, `payer_msisdn`, `started_on`, `expires_on = started_on + timeout`,
     `next_check_on = started_on + rc.MIN_CHECK_INTERVAL` (le scheduler la reprend si la page est
     fermée). `save` puis `commit` (D-2) : identifiant durable, verrou relâché avant l'appel HTTP.
  5. `started = gateway.initiate(attempt_id, session, msisdn)`.
  6. `_record_start(...)` : nouveau verrou, `integration_request` écrit. `Error` n'est appliqué que
     si la ligne est encore `Initiated` (un callback ou le scheduler a pu passer entre-temps) :
     `lc.check_attempt_transition`, puis `next_check_on = None`.
  7. Renvoie `payer_status` relu. Même forme que `get_status`, sans rechargement de la page.
- `mtn_momo_callback(attempt=None)`, `methods=["PUT", "POST"]`, invité : vérifie la forme UUID
  (regex), lit le statut par `frappe.db.get_value`. Si la tentative est connue et vérifiable :
  `frappe.enqueue("local_payments.reconcile.reconcile", attempt_id=..., job_id=f"local_payments:reconcile:{attempt}", deduplicate=True)`.
  Renvoie toujours `None`, quelle que soit la tentative. Aucun appel sortant.
- Docstring du module : ajouter les deux endpoints d'écriture.

**Page `local_payment_checkout.html`**

- Champ et bouton activés, `required`, `aria-describedby`. Retrait de « Paying from this page is
  not open yet. ». Un `<div class="invalid-feedback" id="lp-msisdn-error">`. Le formulaire est
  masqué quand `waiting`.
- Message générique d'échec rendu côté serveur dans un `data-*` (traduit par Jinja).
- JS : le polling devient `startPolling()`, appelé au chargement si `waiting`. Au submit :
  `preventDefault`, bouton désactivé, `frappe.call({method: "local_payments.api.start_attempt", type: "POST", silent: true, ...})`.
  Si le statut est en cours : formulaire masqué, attente affichée, `startPolling()`. Si `Error` :
  message générique sous le champ. En erreur serveur : premier message de `_server_messages`
  sous le champ (`is-invalid`), sinon le message générique. Bouton réactivé.

**Doctype `Local Payments Test Settings`** (réservé aux tests) : ajout de `msisdn_prefix` (237),
`msisdn_national_length` (9) et `pending_timeout_minutes` (15), pour tester `api.py` sur le
contrat générique.

**Traductions** : `bench generate-pot-file --app local_payments` puis
`bench update-po-files --app local_payments`, avec les `msgstr` français remplis à la main.

## Tests

- `tests/test_lifecycle.py` : arête `(INITIATED, ERROR)` dans `ATTEMPT_EDGES`.
- `tests/test_mtn_momo.py` (pur) : `on_exchange` appelé une fois par envoi (deux après un 401),
  jamais pour le jeton ; ni `Authorization` ni la clé d'abonnement dans l'échange ; un timeout
  produit un échange avec `error` ; corps tronqué.
- `test_mtn_momo_settings.py` (transport simulé) : chaque `InitiationOutcome` donne le bon statut
  (`subTest`) ; `X-Callback-Url` présent seulement si `send_callback`, avec l'`attempt_id` et sans
  le token ; l'IR ne contient ni clé ni `api_key` ; `NOT_SENT` ne crée pas d'IR ; un montant
  fractionnaire donne `Error` sans appel HTTP.
- `tests/test_start_attempt.py` (site, même nettoyage par commit que `CheckoutTestCase`, qu'on
  réutilise ; `initiate` patché sur les Test Settings) :
  - token inconnu ou mal formé, session `Paid`/`Void`, tentative `Initiated`/`Pending` en cours,
    passerelle désactivée : refus, aucune ligne, `initiate` jamais appelé ;
  - numéro invalide : `ValidationError`, aucune ligne, aucun appel ; normalisation (`subTest`)
    des saisies acceptées ;
  - numéro valide : une seule ligne avec un UUID v4, `payer_msisdn` normalisé, `started_on`,
    `expires_on` à +15 min, `next_check_on` renseigné, `Initiated` ; déjà présente en base au
    moment où `initiate` est appelé ;
  - rejet : `Error`, `next_check_on` vide, nom de l'IR enregistré, nouvelle tentative possible ;
  - une ligne passée à `Pending` entre-temps n'est pas écrasée par un rejet tardif.
  - Callback : tentative connue → `frappe.enqueue` appelé (patché) avec `reconcile` et le
    `job_id` ; inconnue, mal formée ou réglée → même réponse, aucun enqueue, aucun appel fournisseur.

## Documentation

- `docs/ARCHITECTURE.md` : diagramme des tentatives (`[*] → Initiated` enregistrée avant l'appel, `Initiated → Error` initiation refusée) ; « Entry points » (réponse de `start_attempt`, callback
  ignoré si inconnu ou réglé) ; paragraphe Integration Request (initiation seule, en-têtes secrets exclus).
- `docs/gateways/mtn-momo.md` : correspondance des statuts (400, 401 et jeton indisponible → `Error`, 409 → `Initiated`) ; « Known errors » (Error Log, pas d'alerte) ; règles callback (dédoublonnage, rate limits) ; saisies MSISDN acceptées ; contenu de l'IR.
- `CLAUDE.md` : la ligne sur les commits autorise aussi `api.start_attempt` (un commit avant l'appel fournisseur).

## Vérification

1. `bench --site lp-test.localhost run-tests --app local_payments` (tests purs et tests du site).
2. `python scripts/check_pure_core.py`.
3. `pre-commit run --files <fichiers touchés>` (ruff, semgrep, gitleaks).
4. Essai manuel : ouvrir la page d'une session Sandbox, saisir un numéro invalide (erreur sous le champ, aucune ligne), puis un numéro valide de test (la page passe en attente sans rechargement). Contrôler la tentative et l'IR (aucun secret), puis appeler le callback avec
   `curl -X PUT` sur un `attempt_id` connu et un inconnu.
5. Revue : `lp-review-invariants`, puis `/code-review` et `/simplify` (KISS, lisibilité).
   Context7 pour `requests` (exceptions). `/humanizer` sur les commentaires et la doc.

## Hors périmètre

- Journaliser les requêtes de statut dans l'IR (D-1).
- Alerter les managers sur une initiation refusée (D-3).
- Le compteur `get_status` porte sur `ip:token` et non sur le seul token (constat sur
  `rate_limiter.py`, existant depuis LP-06). À signaler, pas à corriger ici.
