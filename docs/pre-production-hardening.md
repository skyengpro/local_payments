# Durcissement avant la production — `local_payments`

|             |                                                                                                                                                              |
| ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Date        | 2026-10-06                                                                                                                                                   |
| Source      | [`security-review-2026-10.md`](security-review-2026-10.md) (revue du 2026-10-05, commit `a5a3b8e`)                                                        |
| Périmètre | Le code de`local_payments` uniquement. Pas de changement d'ERPNext, de `frappe` ou de `payments`, et pas de nouvelle règle sur les documents ERPNext. |
| État       | Point 1 (LP-SEC-16) implémenté le 2026-10-06. Les autres ne le sont pas. Le code cité ci-dessous a été vérifié sur`main` (`fd3373d`).             |

Ce document liste ce qu'il faut traiter dans l'application avant le premier paiement en production,
et dans quel ordre. Chaque point renvoie à sa section du rapport de sécurité pour le détail. Les
points sont traités **un par un** : un point est terminé (code, tests, reviews, docs) avant de
commencer le suivant.

## Ordre de traitement

| # | ID                                                                               | Sujet                                                   | Gravité      | Fichiers principaux                                                                        |
| - | -------------------------------------------------------------------------------- | ------------------------------------------------------- | ------------- | ------------------------------------------------------------------------------------------ |
| 1 | [LP-SEC-16](#1-lp-sec-16-les-alertes-se-perdent-quand-personne-na-le-rôle)       | Les alertes se perdent quand personne n'a le rôle      | Haute         | `reconcile.py`, `scheduler.py`, `mtn_momo_settings.py`                               |
| 2 | [LP-IMP-02](#2-lp-imp-02-alerter-dès-le-premier-échec-qui-demande-une-personne) | Alerter dès le premier échec qui demande une personne | Amélioration | `reconcile.py`, `local_payment.json`                                                   |
| 3 | [LP-IMP-04](#3-lp-imp-04-permissions-du-rôle-local-payments-manager)             | Permissions du rôle Local Payments Manager             | Amélioration | `local_payment.json`                                                                     |
| 4 | [LP-IMP-01](#4-lp-imp-01-bouton-retry-authorization)                              | Bouton « Retry authorization »                        | Amélioration | `api.py`, `local_payment.js` (nouveau)                                                 |
| 5 | [LP-SEC-15](#5-lp-sec-15-champs-de-session-modifiables-après-création)          | Champs de session modifiables après création          | Moyenne       | `local_payment.json`, `local_payment.py`, `reconcile.py`, `api.py`, `erpnext.py` |
| 6 | [LP-SEC-03](#6-lp-sec-03-numéro-du-payeur-dans-lerror-log)                       | Numéro du payeur dans l'Error Log                      | Moyenne       | `api.py`, `gateway.py`, page de paiement                                               |
| 7 | [LP-SEC-07](#7-lp-sec-07-partie-app-url-de-callback)                              | URL de callback (partie app)                            | Moyenne       | `mtn_momo_settings.py`                                                                   |
| 8 | [LP-SEC-10](#8-lp-sec-10-hôte-de-production-non-contrôlé)                      | Hôte de production non contrôlé                      | Basse         | `mtn_momo_settings.py`                                                                   |

Pourquoi cet ordre :

- **1 et 2** forment le circuit des alertes. Tant qu'un paiement reçu mais non comptabilisé ne
  prévient personne, aucun autre problème ne sera vu à temps. C'est aussi le seul point de gravité
  haute du périmètre.
- **3 puis 4** : le bouton de relance s'appuie sur les droits du rôle. Avec 1 et 2, il donne au
  marchand un moyen d'agir sur une alerte sans passer par la console.
- **5** vient après 4, parce qu'il pose une garde sur tous les endroits qui écrivent l'état d'une
  session, y compris la relance ajoutée en 4.
- **6, 7 et 8** sont indépendants et plus petits. 6 répond à la loi camerounaise 2024/017, en vigueur
  depuis le 23 juin 2026. 7 évite des callbacks perdus en production. 8 protège les identifiants
  marchand.

## Méthode, pour chaque point

1. **Plan court** du point : rappel du constat, fichiers, tests, décisions ouvertes. Les décisions
   sont posées au demandeur avant d'écrire du code.
2. **Vérification** des API Frappe utilisées dans la source v16 installée, avec le fichier cité.
   Context7 sert pour les bibliothèques tierces (`requests`, `certifi`) et pour les bonnes
   pratiques. En cas de désaccord, la source installée l'emporte (CLAUDE.md).
3. **Code minimal, KISS.** Avant tout helper, chercher l'équivalent dans `frappe` ou `payments`.
4. **Tests unitaires** sur la logique pure (sans base), **tests de site** pour le comportement
   Frappe.
5. **Première review** (`code-review`, niveau `high`) avant toute correction. Chaque constat est
   corrigé, et les tests repassent, avant de continuer.
6. **Reviews par domaine** : `lp-review-invariants` (invariants de paiement), `security-review`,
   `simplify`, `frappe-core-translation` pour les nouvelles chaînes. Puis une passe
   `humanizer:humanizer` sur les commentaires et les docs : ton humain, neutre, concis, fidèle au
   code, sans référence à un ticket, à ARCHITECTURE ou à une ADR dans le code.
7. **Docs** : ARCHITECTURE (et ADR si une décision change), statut du point dans le rapport de
   sécurité, `locale/fr.po`.

**Un point est fini quand** les tests purs et les tests de site sont verts dans les deux jobs CI
(avec et sans ERPNext), le garde du cœur pur est vert, les reviews sont closes et les docs sont à
jour.

---

## 1. LP-SEC-16. Les alertes se perdent quand personne n'a le rôle

Détail : [rapport, LP-SEC-16](security-review-2026-10.md#lp-sec-16-alerts-are-lost-when-nobody-holds-local-payments-manager).

**Constat (vérifié).**

- `alert_managers()` ([`reconcile.py:325`](../local_payments/reconcile.py#L325)) n'envoie qu'aux
  détenteurs de `Local Payments Manager` par `get_users_with_role()`, qui exclut Administrator et les
  utilisateurs désactivés. Sans détenteur, la boucle est vide : aucune notification, aucune erreur.
- `_alert_unresolved` et `_alert_authorization`
  ([`scheduler.py:137-148`](../local_payments/scheduler.py#L137-L148)) posent `alerted` ou
  `authorization_alerted` **avant** l'envoi. Une alerte jamais reçue est donc considérée comme
  envoyée, et n'est jamais retentée.
- Les alertes sont des `Notification Log` de type `Alert` : Frappe ne les envoie jamais par e-mail.

**À faire.**

1. `alert_managers()` renvoie le nombre de notifications créées. Sans détenteur actif du rôle, elle
   notifie les System Managers actifs. S'il n'y en a pas non plus, elle écrit un Error Log
   (« Local Payment alert with no recipient ») qui contient le texte de l'alerte.
2. Le scheduler ne pose `alerted` / `authorization_alerted` qu'après au moins une notification
   créée. Sinon, le job quotidien réessaie le lendemain.
3. Enregistrer un `MTN MoMo Settings` activé en Production sans détenteur actif du rôle affiche un
   avertissement (`msgprint`), sans bloquer l'enregistrement.
4. ARCHITECTURE, « Installation and configuration » : ajouter l'étape « assigner Local Payments
   Manager à au moins une personne ».

**Tests.** Destinataire normal, repli vers System Manager, aucun destinataire (Error Log), drapeau
non posé quand rien n'est envoyé puis posé au passage suivant, avertissement du Settings.

**Décisions prises (2026-10-06).**

- L'envoi par e-mail est reporté.
- Les alertes immédiates de `reconcile` ne sont pas rattrapées : sans destinataire, l'Error Log garde
  leur texte.
- Le scheduler pose le drapeau avant l'appel. `alert_managers()` le commit avec les notifications, ou
  l'annule (rollback) quand personne n'a été notifié. Le scheduler ne fait aucun commit.

## 2. LP-IMP-02. Alerter dès le premier échec qui demande une personne

Détail : [rapport, LP-IMP-02](security-review-2026-10.md#lp-imp-02-alert-on-the-first-non-transient-authorization-failure).

**Constat (vérifié).** Les managers ne sont alertés qu'après `MAX_AUTHORIZATION_TRIES` (5) essais,
espacés de 1 h, 2 h, 4 h et 8 h ([`reconcile.py:66-69`](../local_payments/reconcile.py#L66-L69)),
puis au passage du job quotidien. La première alerte arrive donc 15 à 39 h après le paiement. Une
erreur de configuration ne se corrige jamais seule : ces essais ne font que retarder l'alerte.

**À faire.**

1. Une fonction pure qui classe une exception : à traiter par une personne (`frappe.ValidationError`
   et ses sous-classes, erreurs de permission) ou transitoire (verrou, deadlock, connexion).
2. Dans `_record_authorization_failure()`, pour une erreur à traiter par une personne, une alerte
   immédiate (`authorization_failed`), envoyée une seule fois par session grâce à un nouveau champ
   drapeau. Elle passe par le circuit du point 1 (destinataire de repli, drapeau posé après envoi).
   Le backoff des relances ne change pas.
3. Traduction de la nouvelle raison dans `fr.po`.

**Tests.** Unitaires sur le classement. Tests de site : un premier échec de validation alerte une
fois, un second échec n'alerte pas de nouveau, un échec transitoire n'alerte pas.

**Décisions à prendre.** Liste exacte des exceptions transitoires. Faut-il remettre le drapeau à zéro
quand l'autorisation finit `Done` ?

## 3. LP-IMP-04. Permissions du rôle Local Payments Manager

Détail : [rapport, LP-IMP-04](security-review-2026-10.md#lp-imp-04-permissions-of-local-payments-manager).

**Constat (vérifié).** `Local Payment` donne `read` et `report` à Local Payments Manager et à System
Manager, sans `export`. Le rôle ne donne accès à rien d'autre, ce qui est voulu.

**À faire.** Ajouter `export` sur `Local Payment` pour les deux rôles, dans `local_payment.json`.
Garder sans permission d'écriture, de création ni de suppression. Ne rien ouvrir sur
`Integration Request`, `Error Log` ni sur les Settings. Le droit de relance (point 4) se vérifie dans
la méthode elle-même. Documenter la matrice des permissions dans ARCHITECTURE, « Security and
permissions ».

**Tests.** Un manager peut exporter, et ne peut ni écrire ni lire les Settings.

## 4. LP-IMP-01. Bouton « Retry authorization »

Détail : [rapport, LP-IMP-01](security-review-2026-10.md#lp-imp-01-retry-authorization-button-on-each-session).

**Constat (vérifié).** Le docstring du scheduler et le message de refus d'annulation
([`erpnext.py:100`](../local_payments/erpnext.py#L100)) renvoient à une relance manuelle qui n'existe
pas. Le dossier du doctype ne contient pas de `local_payment.js`. Aujourd'hui, seule la console
permet de relancer.

**À faire.**

1. `local_payments.api.retry_authorization(session_name)` : en POST seulement, pas en Guest.
   Elle vérifie `frappe.only_for(["Local Payments Manager", "System Manager"])` et la permission
   de lecture, verrouille la session sans attendre (`wait=False`, avec le même message que
   `void_open_sessions` si elle est déjà verrouillée), appelle `reconcile.authorize()`, puis renvoie
   le nouvel état `authorization`.
2. `local_payment.js` dans le dossier du doctype : le bouton s'affiche quand `status = Paid` et
   `authorization = Failed`, ou `Pending` depuis plus de `PENDING_AUTHORIZATION_GRACE`. La page se
   recharge ensuite.
3. Le compteur `authorization_tries` n'est pas remis à zéro.

**Tests.** Refus pour un utilisateur sans le rôle, `Failed` vers `Done`, aucun effet sur une session
`Done`, réponse immédiate quand la session est verrouillée.

**Décision à prendre.** Le verrou sans attente demande un petit changement de `authorize()` (un
paramètre), ou un essai de verrou dans la méthode avant l'appel. Le choix se fait au plan du point.

## 5. LP-SEC-15. Champs de session modifiables après création

Détail : [rapport, LP-SEC-15](security-review-2026-10.md#lp-sec-15-session-record-fields-can-be-edited-after-creation).

**Constat (vérifié dans `local_payment.json`).** Seul `token` est `set_only_once`. `payment_gateway`,
`reference_doctype`, `reference_docname`, `amount`, `currency`, `title`, `description`,
`payer_name`, `payer_email` et `redirect_to` ne sont même pas `read_only`. `read_only` ne lie que le
formulaire : par l'API REST, Administrator peut modifier `status` ou `paid_amount`. `track_changes`
est désactivé, donc ces modifications ne laissent aucune trace.

**À faire.**

1. Champs fixés à la création (`set_only_once` + `read_only`) : `token`, `payment_gateway`,
   `reference_doctype`, `reference_docname`, `amount`, `currency`, `title`, `description`,
   `payer_name`, `payer_email`, `redirect_to`, `request_data`.
2. Champs d'état (`status`, `paid_amount`, `paid_on`, `provider_transaction_id`, `authorization*`,
   `success_redirect`, lignes de `attempts`) : `LocalPayment.validate()` refuse une modification
   que n'accompagne pas un drapeau posé par le code de l'application. Les endroits qui écrivent ces
   champs, à couvrir tous : `reconcile.py` (`_claim`, `_record`, `authorize`,
   `_record_authorization_failure`), `api.py` (`_open_attempt`, `_record_start`),
   `erpnext.void_open_sessions` et la relance du point 4. Les écritures par `frappe.db.set_value` du
   scheduler ne passent pas par `validate()` : on le vérifie et on le documente.
3. Activer `track_changes`.

**Tests.** Une modification d'un champ fixé par Administrator est refusée. Un `PUT` REST sur `status`
est refusé. Chaque chemin de l'application qui écrit l'état passe encore (les suites existantes le
couvrent).

**À regrouper éventuellement.** LP-SEC-17 (masquer le token dans le formulaire) touche le même
fichier JSON. Ce n'est qu'une ligne, mais le point n'est pas prioritaire : on décide au plan du point.

## 6. LP-SEC-03. Numéro du payeur dans l'Error Log

Détail : [rapport, LP-SEC-03](security-review-2026-10.md#lp-sec-03-payer-phone-numbers-written-to-the-error-log).

**Constat.** Un numéro refusé par `payer_msisdn()` ([`gateway.py:84-92`](../local_payments/gateway.py#L84-L92))
lève une `ValidationError`. Le rapport a constaté sur `acad.localhost` que Frappe enregistre alors
dans l'Error Log une trace avec les variables : `kwargs`, `msisdn`, `number`. Le sanitizer de Frappe
ne masque pas ces noms. Cela contredit ARCHITECTURE (« Security and permissions ») et la règle
`guest-surface`.

**À faire.**

1. D'abord, retrouver dans la source v16 **quel** mécanisme écrit cette entrée pour une
   `ValidationError` levée par une méthode whitelistée. La correction en dépend.
2. Refuser un numéro invalide sans passer par le journal d'erreurs. La piste du rapport : réponse
   structurée (`{"error": "invalid_msisdn", "message": …}`) avec le statut HTTP 417, et adapter
   la page de paiement, qui lit déjà le message.
3. Purger les entrées existantes.

**Tests.** Un POST avec un numéro invalide ne crée aucune ligne d'Error Log qui contienne le numéro.
La page affiche toujours le message sous le champ.

**Décision à prendre.** La purge : un patch (`patches.txt`) qui supprime les Error Log de
`start_attempt` qui contiennent un numéro, ou une procédure manuelle documentée ?

## 7. LP-SEC-07 (partie app). URL de callback

Détail : [rapport, LP-SEC-07](security-review-2026-10.md#lp-sec-07-callback-url-depends-on-host-configuration-and-is-not-forced-to-https).

**Constat (vérifié).** `callback_url()`
([`mtn_momo_settings.py:100-102`](../local_payments/local_payments/doctype/mtn_momo_settings/mtn_momo_settings.py#L100-L102))
appelle `get_url()`. Sans `host_name` dans la config du site, l'URL suit l'en-tête `Host` de la
requête du payeur, et son schéma peut être `http://`, que MTN refuse en production. Le callback se perd alors sans bruit.

**À faire.**

1. `validate()` du Settings : si `send_callback` est coché, exiger `host_name` dans la config du
   site, en `https://` en Production.
2. Construire l'URL de callback à partir de `host_name` seulement, jamais de la requête.
3. ARCHITECTURE, « Installation and configuration » : `host_name` obligatoire.

**Tests.** Settings refusé sans `host_name`, ou avec un `host_name` en `http://` en Production.
L'URL de callback ignore l'en-tête `Host` de la requête. Le Sandbox accepte l'hôte ngrok du dev.

**Hors app (déploiement).** Certificat TLS public sur le domaine de production, et hôte de callback
enregistré dans le portail marchand MTN.

## 8. LP-SEC-10. Hôte de production non contrôlé

Détail : [rapport, LP-SEC-10](security-review-2026-10.md#lp-sec-10-production-settings-accept-any-https-host).

**Constat (vérifié).** `validate_api_base_url()`
([`mtn_momo_settings.py:171-182`](../local_payments/local_payments/doctype/mtn_momo_settings/mtn_momo_settings.py#L171-L182))
contrôle l'hôte en Sandbox seulement. En Production, n'importe quel hôte HTTPS est accepté, y compris
celui du sandbox, et `target_environment = sandbox` passe aussi. Les identifiants marchand partent
vers cet hôte.

**À faire.** En Production, refuser `SANDBOX_HOST` et `target_environment = "sandbox"`. Une liste
d'hôtes autorisés viendra quand MTN Cameroun aura communiqué l'hôte de production : on l'ajoute aux
« Points to confirm before production » de [`gateways/mtn-momo.md`](gateways/mtn-momo.md).

**Tests.** Production refusée avec l'hôte sandbox, refusée avec `target_environment = sandbox`,
acceptée avec un autre hôte HTTPS.

---

## Hors de ce document

**Après la production** (code de l'app, non bloquant) : LP-SEC-05 (expiration des tokens), LP-SEC-04
(conservation des données, qui attend une décision sur la durée), LP-SEC-08, LP-SEC-09, LP-SEC-11,
LP-SEC-17, LP-IMP-05.

**Intégration ERPNext**, en attente : LP-SEC-01 et LP-IMP-03 (plan dans
[`LP-12-plan.md`](LP-12-plan.md)), LP-SEC-02.

**Déploiement**, à faire avant la production, mais hors du code : LP-SEC-06 (`X-Forwarded-For`),
LP-SEC-13 (HSTS, `frame-ancestors`), LP-SEC-12 (filtrage des adresses du callback, si MTN les
publie), LP-SEC-14 (Redis non exposé), partie proxy de LP-SEC-05 (ne pas journaliser le token), et
le certificat TLS du site.
