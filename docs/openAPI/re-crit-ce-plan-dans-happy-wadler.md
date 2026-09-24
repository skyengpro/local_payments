# Plan LP-08 : client MTN MoMo dans le cœur pur

## Contexte

`providers/mtn_momo.py` est le seul module qui parle à l'API MTN MoMo Collection : jeton d'accès,
RequestToPay, requête de statut. Il traduit les réponses brutes de MTN en `ProviderResult`
(`lifecycle.py`) pour `reconcile()`, n'importe rien de `frappe` (D7) et ne garde rien dans
`provider_data` (MTN n'en a pas besoin). La cible est le Cameroun, sur Frappe v16.

LP-07 (décision D-1) a reporté ici le branchement côté Frappe : `MTN MoMo Settings.check_status()`,
le cache du jeton et la lecture des mots de passe.

## Sources et leur portée

| Source | Portée | Utilisé pour |
| --- | --- | --- |
| `docs/openAPI/collection.json` (export du portail, fourni par l'utilisateur) | Officielle, tous pays | Opérations, en-têtes, schémas, codes HTTP, `status` ∈ {PENDING, SUCCESSFUL, FAILED}, `ErrorReason` (17 codes) |
| Page « testing » de momodeveloper.mtn.com (via Context7) | Officielle, sandbox commune à tous les pays | Sandbox = EUR uniquement |
| Page « common-error » (via Context7) | Officielle, globale | 400/401 ; `COULD_NOT_PERFORM_TRANSACTION` = fenêtre d'approbation de 5 min (non confirmé pour le Cameroun) |
| Annonce communauté MTN du 30/01/2024 | Officielle, exemple ougandais | `reason` peut être une chaîne ; contredit l'OpenAPI (objet) |
| Page Cameroun (conditions d'utilisation) | Cameroun | Aucun contenu technique ; contact `MoMoCorporate.CM@mtn.com` |

Aucune doc technique publique propre au Cameroun : URL de production, `X-Target-Environment` et
codes de motif réels restent de la configuration ou des points à confirmer.

## Décisions

| # | Sujet | Décision |
| --- | --- | --- |
| D-1 | Statut brut | ✅ `ProviderResult` reçoit `provider_status: str \| None = None` ; `reconcile._record` l'écrit sur la tentative. |
| D-2 | `provider_data` | ✅ Rien pour MTN. `check_status` accepte le dict (contrat de reconcile) sans le lire. |
| D-3 | Devise sandbox | ✅ En Sandbox, envoi en EUR et, au retour, EUR remplacé par la devise des Settings. En production, aucune traduction. |
| D-4 | Client HTTP | ✅ `requests` : cité par le ticket, dépendance directe de Frappe v16 (`requests~=2.33`), donc pas de nouvelle dépendance ni d'ADR. Injecté dans le client pour les tests. |
| D-5 | Format de `reason` | ✅ Accepte chaîne ou objet `{code, message}` (les deux sources officielles divergent). Le code n'est que recopié, jamais interprété. |
| D-6 | Caractères refusés | ✅ Liste blanche prudente, la liste exacte de MTN n'étant pas publiée : accents repliés en ASCII (NFKD), on garde `A-Z a-z 0-9 espace . , - _ : /`, espaces fusionnés, tronqué à 160. À confirmer, noté dans la doc. |

## Conception de `local_payments/providers/mtn_momo.py`

Aussi `local_payments/providers/__init__.py` (vide).

- `MtnMomoConfig` (dataclass figée) : `api_base_url`, `target_environment`, `subscription_key`,
  `api_user`, `api_key`, `currency`, `msisdn_prefix`, `msisdn_national_length`, `sandbox: bool`.
- `TokenStore` (Protocol) : `get(key)`, `set(key, token, ttl_seconds)`, `delete(key)`.
- `MtnMomoClient(config, token_store, cache_key, http=None, timeout=(5, 15))`.

**Jeton.** `POST {base}/collection/token/`, avec la Basic auth `api_user:api_key` et la clé
d'abonnement. Il est mis en cache pour `expires_in - 60` s (rien n'est stocké si ce délai est ≤ 0).
Sur un 401 d'une opération métier : suppression du cache, un nouveau jeton, **une seule** nouvelle
tentative. Un second 401 veut dire « non autorisé ».

**Contrôles avant tout appel HTTP** (`InvalidRequest(ValueError)`) :
- MSISDN = `msisdn_prefix` + exactement `msisdn_national_length` chiffres ASCII ;
- montant entier strictement positif, envoyé en chaîne (`"5000"`), jamais arrondi.

**`request_to_pay(attempt_id, amount, msisdn, external_id, payer_message, payee_note, callback_url=None) -> Initiation`**

L'en-tête `X-Reference-Id` reçoit `attempt_id` tel que fourni (le client ne génère jamais
d'identifiant). `X-Callback-Url` n'est envoyé que si `callback_url` est fourni. La devise est celle
des Settings, ou EUR en sandbox (D-3). Issues distinctes (`InitiationOutcome`), traduites par
l'appelant :

| Réponse MTN | Issue |
| --- | --- |
| 202 | `ACCEPTED` |
| 409 | `DUPLICATE_REFERENCE` |
| 400, ou autre 4xx sauf 401 | `REJECTED` (avec code HTTP et code MTN) |
| 401 après renouvellement | `UNAUTHORIZED` |
| 5xx, timeout, erreur réseau, autre statut | `UNKNOWN` (sans danger : la référence reste interrogeable) |
| Jeton impossible à obtenir (5xx, timeout) | `NOT_SENT` (RequestToPay n'est jamais parti) |

**`check_status(attempt_id) -> ProviderResult`**

| Réponse MTN | Résultat |
| --- | --- |
| 200 `PENDING` / `SUCCESSFUL` / `FAILED` | `Pending` / `Succeeded` / `Failed` ; `amount`, `currency` (traduite en sandbox) et `financialTransactionId` passés en `str` ; `provider_status` = `"FAILED: NOT_ENOUGH_FUNDS"` (tronqué à 140) |
| 404 | `Failed`, `provider_status` = `"404 RESOURCE_NOT_FOUND"` |
| 200 avec statut inconnu ou JSON illisible | `MtnMomoError(UNEXPECTED)` |
| 400 | `MtnMomoError(REJECTED)` |
| 401 après renouvellement | `MtnMomoError(UNAUTHORIZED)` |
| 5xx, timeout, erreur réseau | `MtnMomoError(UNAVAILABLE)` |
| Tout autre statut | `MtnMomoError(UNEXPECTED)` |

`Expired` n'est jamais produit. `Initiated`, `Unresolved` et `Error` sont décidés ailleurs.
Aucune exception `requests` ne sort du module. Les messages d'erreur ne contiennent que
l'opération, le code HTTP et le code MTN : jamais de jeton, de clé ni d'en-tête. L'écart de
montant reste l'affaire de `lifecycle.py`.

Une `MtnMomoError` levée par `check_status` suit le chemin existant : `get_status` annule la
transaction et écrit dans l'Error Log, le scheduler journalise l'erreur et passe au suivant. La
tentative ne change pas et sera réinterrogée plus tard.

## Côté Frappe

- `lifecycle.py` : champ `provider_status` optionnel sur `ProviderResult` (D-1).
- `reconcile.py` `_record` : `row.provider_status = result.provider_status` quand il est fourni.
- `mtn_momo_settings.py` :
  - `mtn_client()` construit `MtnMomoConfig` avec `get_password("subscription_key")` et
    `get_password("api_key")`, `sandbox = environment == "Sandbox"` ;
  - `check_status(attempt_id, provider_data)` renvoie `self.mtn_client().check_status(attempt_id)` ;
  - `on_update()` appelle `super().on_update()` puis vide le jeton en cache (des identifiants
    modifiés ne réutilisent pas l'ancien jeton) ;
  - un petit `TokenStore` Frappe sur `frappe.cache` (`set_value(..., expires_in_sec=ttl)`,
    `get_value`, `delete_value`), clé `local_payments:mtn_momo_token:<name>`. API vérifiée dans
    `frappe/utils/redis_wrapper.py` (v16).

## Tests

**`local_payments/tests/test_mtn_momo.py`** (pur, `unittest.TestCase`, sans site). Un faux
transport rejoue les réponses de `local_payments/tests/fixtures/mtn_momo.json`, construites à partir
des exemples de l'OpenAPI officiel avec les types corrigés, et marquées comme telles. Un
`TokenStore` en mémoire. Numéros factices uniquement. Des tableaux avec `subTest` gardent les tests
courts :

- en-têtes de chaque opération (clé d'abonnement, cible, `X-Reference-Id` = `attempt_id`, rappel
  seulement si fourni, Basic auth pour le jeton) ;
- jeton en cache avec un TTL de `expires_in - 60` ; 401 → un renouvellement et une nouvelle
  tentative ; deux 401 → non autorisé ;
- chaque ligne des deux tableaux d'issues ci-dessus ;
- `reason` en chaîne et en objet ;
- MSISDN invalide et montant non entier refusés **sans aucun appel HTTP** ;
- nettoyage et troncature des textes ;
- sandbox : EUR envoyé puis traduit ; production : devise renvoyée telle quelle.

**Site** (une assertion chacun) :
- `test_mtn_momo_settings.py` : `check_status` passe par le client avec les mots de passe
  déchiffrés et renvoie le `ProviderResult` (transport simulé) ;
- `test_reconcile.py` : `provider_status` est écrit sur la tentative.

## Doc

- `docs/gateways/mtn-momo.md` :
  - Opérations : clé d'abonnement sur toutes les opérations, lien vers `docs/openAPI/collection.json` ;
  - Correspondance des statuts : statut inconnu → erreur, format de `reason`, `provider_status` ;
  - Erreurs connues : issues de l'initiation, erreurs du statut sans changement d'état ;
  - Points à confirmer : format réel de `reason` au Cameroun, fenêtre de 5 min, caractères refusés,
    traduction EUR en sandbox ;
  - Références : remplacer le lien rwandais mort, indiquer le pays de chaque source ;
  - Ajouter que MTN n'écrit rien dans `provider_data`.
- `docs/openAPI/*.json` ajoutés au dépôt comme références (aucun secret, vérifié).

## Vérification

1. `bench --site lp-test.localhost run-tests --app local_payments` (tests purs et tests du site).
2. `python scripts/check_pure_core.py`.
3. `pre-commit run` sur les fichiers touchés (ruff, détection de secrets).
4. Revue : `lp-review-invariants`, `frappe-core-permissions` / `frappe-syntax-controllers` au
   besoin, Context7 pour `requests` (timeouts, exceptions), `/humanizer` sur la doc et les
   commentaires.

## Hors périmètre

- `start_attempt` (appel de `request_to_pay`, traduction des issues en statuts de tentative,
  Integration Request) et le point d'entrée du callback : tickets suivants.
- Normalisation de la saisie du payeur (espaces, `+237`) : relève de `start_attempt`.
