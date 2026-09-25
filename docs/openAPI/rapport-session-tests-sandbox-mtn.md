# Rapport de session : tests sandbox MTN MoMo (EP-02)

Date : 2026-09-25. Site de test : `acad.localhost` (frappe, payments, lms, local_payments).
Portée du ticket : faire passer les parcours payeur réels dans la sandbox MTN et vérifier ce que voit le navigateur et ce que le site enregistre.

## 1. Résumé

| Sujet | État |
| --- | --- |
| Plan de test (tutoriel A à F) | Rédigé |
| Tunnel public pour le callback | En place (ngrok) |
| API User et API Key sandbox | Générés, gateway configuré |
| Scénario D1 (paiement réussi) et traduction de devise | Exécutés, conformes côté MTN et session |
| Autorisation côté consommateur LMS | **Échec** : incompatibilité LMS, hors adaptateur |
| Scénarios D2 à D11 | Non exécutés |
| Fixtures et `mtn-momo.md` | Non modifiés |

Aucun fichier de code du repo n'a été modifié pendant la session. Seuls la configuration du site et ce rapport ont changé.

## 2. Ce qui a été fait

### 2.1 Plan
Un plan en tutoriel a été écrit (`Faire`, puis `Attendu`), en six parties : environnement, génération des clés, configuration du site, onze scénarios (D1 à D11), livrables du repo, nettoyage.

### 2.2 Tunnel public
- ngrok lancé avec réécriture du header Host, pour que Frappe route vers le bon site :
  `ngrok http 8000 --host-header=acad.localhost`
- `host_name` du site fixé sur l'URL publique ngrok.
- Vérification par `GET /api/method/ping`.

### 2.3 API User et API Key
Créés par l'API de provisioning de la sandbox :
1. `X-Reference-Id` = UUID v4 généré localement, c'est l'**API User**.
2. `POST /v1_0/apiuser` avec `providerCallbackHost` = host du tunnel.
3. `POST /v1_0/apiuser/{REF}/apikey` : renvoie l'**API Key** (affichée une seule fois).
4. Contrôle par `POST /collection/token/`.

Ne pas confondre avec le `X-Reference-Id` de `RequestToPay`, qui porte l'`attempt_id` et est généré par le code.

### 2.4 Configuration du site
- Document **MTN MoMo Settings** créé : Sandbox, XAF, `send_callback` coché, `pending_timeout_minutes` 15. Le `gateway_name` réel est `MTN`, donc le gateway s'appelle `MTN MoMo-MTN` (et pas `mtn-sandbox` comme dans le plan).
- LMS branché sur ce gateway comme moyen de paiement.

## 3. Problèmes rencontrés et corrections

| # | Symptôme | Cause | Correction |
| --- | --- | --- | --- |
| 1 | 404 répétés sur `/socket.io/` via ngrok | Le port 8000 ne sert pas le temps réel, qui est sur le port 9000 | Ignorer. Sans effet sur le paiement, qui n'utilise que du HTTP |
| 2 | « This payment method only accepts XAF, not USD » | Le cours était facturé en USD, le gateway n'accepte que sa devise | Passer le cours en XAF, prix entier |
| 3 | XAF absent du menu de devise de LMS | La devise était désactivée dans la table `Currency` | Cocher `Enabled` sur `Currency XAF` |
| 4 | Élève sans cours, `KeyError: 'rates'` dans `get_order_summary` | « Show USD Equivalent » de LMS Settings convertit via `api.frankfurter.app`, qui ne connaît pas XAF | Décocher `show_usd_equivalent` (sinon LMS enverrait aussi un montant USD au gateway) |
| 5 | Spinner infini sur « Proceed to Payment » | `get_url()` ajoute le port web en mode développeur : redirection vers `https://<tunnel>:8001` | `bench --site acad.localhost set-config http_port 443` |
| 6 | Pas de prompt USSD | La sandbox ne contacte aucun téléphone, l'issue dépend du numéro | Comportement normal |
| 7 | `authorization = Failed` après un paiement réussi | Voir section 5 | Voir section 5 |

## 4. Résultat du scénario D1 (session `LPAY-2026-00003`)

| Attendu par le ticket | Constaté |
| --- | --- |
| Attempt `Succeeded`, `provider_status` `SUCCESSFUL` | Conforme |
| `financialTransactionId` présent | `provider_transaction_id` renseigné |
| `confirmed_amount` et `confirmed_currency` égaux à la session | 10000 / XAF, conforme |
| Traduction de devise, ni `amount_mismatch` ni `duplicate` | Les deux à 0, conforme : c'est le comportement que aucune réponse enregistrée ne pouvait prouver |
| Session `Paid`, `paid_on` posé | Conforme |
| Un Integration Request nommé sur l'attempt | Conforme |
| Page qui poll et redirige seule | Redirigée vers la page du cours, conforme |
| `authorization = Done`, `authorization_tries = 0` | **`Failed`, tries = 1** |

Le corps EUR envoyé à MTN n'a pas encore été relu dans l'Integration Request : à faire pour clore D2.

## 5. Dernier problème : l'autorisation échoue avec LMS comme consommateur

### 5.1 Diagnostic
`authorization_error` : « You need to complete the payment for this course before enrolling. »

Chaîne d'appel :
1. `local_payments` appelle `LMS Course.on_payment_authorized`.
2. Celui-ci appelle `update_payment_record` (`lms/lms/utils.py:2439`).
3. Cette fonction cherche la clé `payment` (le nom de la ligne `LMS Payment` à créditer) dans le payload du callback (`frappe.flags.data`), ou à défaut dans la dernière `Integration Request` du cours.
4. L'Integration Request de `local_payments` contient le corps du `RequestToPay`, sans cette clé. `update_payment_record` sort sans rien faire.
5. Aucun `LMS Payment` n'est marqué `payment_received`, et la création de l'inscription est refusée par `lms_enrollment.py`.

La session reste `Paid` (invariant D4 : la trace du paiement n'est jamais perdue), et l'autorisation part en `Failed`, ce qui la met dans le circuit de `retry_authorizations` et des alertes. C'est le comportement prévu quand un consommateur échoue.

### 5.2 Nature du problème
Ce n'est pas un défaut de l'adaptateur MTN : le paiement est confirmé et enregistré correctement. LMS suppose le format d'Integration Request des gateways de `frappe/payments` (Razorpay, Stripe…). Le ticket précise que le consommateur est volontairement quelconque et que LP-11 (ERPNext) n'est pas fusionné : LMS n'est pas un consommateur prévu.

### 5.2b Effet sur ce paiement de test
L'élève avait déjà une inscription à ce cours (créée le 2026-09-14). Le résultat visible côté cours ne prouve donc rien.

### 5.3 Comment le gérer

**Voie recommandée pour ce ticket : consommateur neutre.**
- Utiliser un `ToDo` comme document de référence (`reference_doctype="ToDo"`). Il n'implémente pas `on_payment_authorized` : `run_method` est sans effet et l'autorisation finit `Done`, ce que le ticket déclare valide.
- Refaire D1 avec ce consommateur pour obtenir `authorization = Done` et `tries = 0`.
- Exécuter ensuite D2 à D11 sur des sessions `ToDo`, sans dépendre de LMS. Les sessions sont aussi plus faciles à recréer (LMS réutilise une session Open identique).
- Consigner dans `mtn-momo.md` que LMS n'est pas un consommateur compatible en l'état.

**Voie hors ticket : rendre LMS compatible** (ticket d'intégration séparé). Pistes, non validées :
- Côté `local_payments` : publier dans l'Integration Request ou dans `frappe.flags.data` un payload contenant `payment`, repris de `request_data` de la session (LMS le transmet déjà dans `get_payment_link`). Cela touche au contrat des consommateurs : il faudra vérifier ARCHITECTURE D6 et l'ADR 0002, et probablement écrire un nouvel ADR (skill `lp-write-adr`).
- Côté LMS : un handler qui retrouve l'`LMS Payment` à partir de la session `Local Payment` sans passer par l'Integration Request.

Aucune de ces pistes n'est à traiter dans EP-02.

**À faire si l'on veut rejouer l'autorisation de la session déjà `Paid`.** `retry_authorizations` la reprendra chaque heure, mais elle échouera tant que LMS n'est pas compatible. Il vaut mieux l'ignorer et la supprimer avec les données de test.

## 6. Modifications de configuration effectuées (site de dev uniquement)

| Où | Changement |
| --- | --- |
| `site_config.json` | `host_name` = URL ngrok, `http_port` = 443 |
| Table `Currency` | `XAF` activée |
| LMS Settings | `show_usd_equivalent` décoché |
| LMS Course | Devise XAF, prix entier |
| MTN MoMo Settings | Document `MTN` créé (Sandbox, XAF) |

À restaurer en fin de campagne : `pending_timeout_minutes` à 15, la vraie `subscription_key`, et supprimer les sessions et documents de test. À l'arrêt du tunnel, l'URL gratuite ngrok change : refaire `set-config host_name` et créer un nouvel API User avec le nouveau `providerCallbackHost`.

## 7. Suite

1. Créer une session `ToDo` fraîche sur le gateway `MTN MoMo-MTN` et refaire D1 (attendu : `Done`, tries 0). Relire le corps EUR de l'Integration Request pour clore D2.
2. Relever sur le portail MTN les numéros de test refus, échec, pending.
3. Exécuter D3 à D11 (D10, l'expiration MTN, en parallèle car c'est le plus long).
4. Remplacer `tests/fixtures/mtn_momo.json` par les captures nettoyées, vérifier que les tests unitaires existants passent inchangés.
5. Mettre à jour `docs/gateways/mtn-momo.md` : lignes confirmées retirées, inconnues laissées en « still unknown », numéros de test, délai d'expiration MTN, verbe et nombre de callbacks.
6. Passer la revue `lp-review-invariants` sur le diff.
