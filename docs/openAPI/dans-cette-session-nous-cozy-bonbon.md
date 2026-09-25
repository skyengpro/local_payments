# Tutoriel : tester MTN MoMo en sandbox sur acad.localhost (ticket EP-02)

Objectif : faire parler le code de `local_payments` à la vraie sandbox MTN, vérifier ce que voit le payeur et ce que le site enregistre, puis remplacer les fixtures et mettre à jour `docs/gateways/mtn-momo.md`.
Site : `acad.localhost` (frappe, payments, lms, local_payments), bench sur le port 8001. LMS n'intervient pas : le consommateur est un document quelconque (un `ToDo`), sans aucune dépendance à ERPNext.

Chaque étape suit le même schéma : **Faire** → **Attendu**.

---

## Partie A : Préparer l'environnement

### A1. Exposer le site à MTN
MTN doit pouvoir appeler `X-Callback-Url`. Ni `cloudflared` ni `ngrok` ne sont installés : en installer un, puis :

```bash
cloudflared tunnel --url http://localhost:8001     # ou : ngrok http 8001
```
Noter l'URL publique `https://<tunnel>`.

```bash
cd /workspace/development/frappe-bench
bench --site acad.localhost set-config host_name https://<tunnel>
```
**Attendu** : `get_url()` produit des liens en `https://<tunnel>/...` (c'est ce qui construit le callback).

### A2. Vérifier que worker et scheduler tournent
```bash
bench start                                   # web, worker, schedule, redis
bench --site acad.localhost enable-scheduler
bench --site acad.localhost doctor
```
**Attendu** : scheduler « enabled », au moins un worker actif. Sans eux, les scénarios callback et scheduler ne peuvent pas passer.

### A3. Ouvrir les outils d'observation
- Desk : listes `Local Payment`, `Integration Request`, `Error Log`.
- Terminal : `tail -f logs/worker.log logs/schedule.log`.
- Navigateur : onglet Network (F12) ouvert sur la page de paiement.

---

## Partie B : Générer l'API User et l'API Key

### B1. Récupérer la subscription key
Sur momodeveloper.mtn.com : s'abonner au produit **Collection**, copier la **Primary key**.

```bash
export SUB='<primary key>'
export HOST='<tunnel sans https://>'
```
(Variables d'environnement seulement : jamais dans un fichier du repo.)

### B2. Créer l'API User
```bash
export REF=$(uuidgen)
curl -i -X POST https://sandbox.momodeveloper.mtn.com/v1_0/apiuser \
  -H "X-Reference-Id: $REF" -H "Ocp-Apim-Subscription-Key: $SUB" \
  -H "Content-Type: application/json" \
  -d "{\"providerCallbackHost\": \"$HOST\"}"
```
**Attendu** : `201 Created`. `$REF` est l'**API User**.

### B3. Créer l'API Key
```bash
curl -s -X POST https://sandbox.momodeveloper.mtn.com/v1_0/apiuser/$REF/apikey \
  -H "Ocp-Apim-Subscription-Key: $SUB"
```
**Attendu** : `{"apiKey":"..."}`. La copier tout de suite, elle n'est affichée qu'une fois.
```bash
export KEY='<apiKey>'
```

### B4. Contrôler le couple
```bash
curl -s https://sandbox.momodeveloper.mtn.com/v1_0/apiuser/$REF -H "Ocp-Apim-Subscription-Key: $SUB"
curl -s -X POST https://sandbox.momodeveloper.mtn.com/collection/token/ \
  -H "Authorization: Basic $(printf '%s:%s' $REF $KEY | base64 -w0)" \
  -H "Ocp-Apim-Subscription-Key: $SUB"
```
**Attendu** : le premier renvoie `providerCallbackHost` et `targetEnvironment: sandbox`; le second renvoie `access_token` et `expires_in`. Conserver la forme de ces réponses (token masqué) pour les fixtures.

### B5. Relever les numéros de test
Dans le portail MTN, noter quel numéro produit succès, refus, échec, pending. Ces numéros iront dans le doc (Partie E).

---

## Partie C : Configurer le site

### C1. Créer MTN MoMo Settings
Desk → **MTN MoMo Settings** → Nouveau (rôle System Manager) :

| Champ | Valeur |
| --- | --- |
| gateway_name | `mtn-sandbox` (immuable) |
| enabled | coché |
| environment | Sandbox |
| currency | XAF |
| api_base_url | `https://sandbox.momodeveloper.mtn.com` |
| target_environment | `sandbox` |
| subscription_key | `$SUB` |
| api_user | `$REF` |
| api_key | `$KEY` |
| msisdn_prefix / msisdn_national_length | `237` / `9` |
| pending_timeout_minutes | `15` (ne pas changer sauf indication) |
| send_callback | coché |
| payer_message / payee_note | texte ASCII simple |

**Attendu** : sauvegarde OK, le gateway `MTN MoMo-mtn-sandbox` existe (liste Payment Gateway).

### C2. Créer une session de paiement fraîche
```bash
bench --site acad.localhost console
```
```python
g = frappe.get_doc("MTN MoMo Settings", "mtn-sandbox")
todo = frappe.get_doc(doctype="ToDo", description="lp-sandbox").insert()
url = g.get_payment_url(amount=100, currency="XAF",
    reference_doctype="ToDo", reference_docname=todo.name,
    title="Test", redirect_to="/")
frappe.db.commit(); print(url)
```
**Attendu** : URL `https://<tunnel>/local_payment_checkout?token=...`. À refaire (nouveau ToDo) avant **chaque** scénario : une session Open identique serait réutilisée.

### C3. Snippet de lecture d'état (à coller dans la console après chaque scénario)
```python
s = frappe.get_doc("Local Payment", {"token": "<token>"})
print({k: s.get(k) for k in ("status","paid_on","authorization","authorization_tries","amount_mismatch","duplicate")})
for a in s.attempts:   # nom du child table à confirmer dans le doctype
    print({k: a.get(k) for k in ("attempt_id","status","provider_status","confirmed_amount","confirmed_currency","next_check_on","expires_on","integration_request")})
```

---

## Partie D : Exécuter les scénarios

Ordre conseillé : D1 → D2 → D3 → D4 → D5 → D6 → D7 → D8 → D9 → D10 (le plus long, lancé en parallèle dès que possible) → D11.

### D1. Paiement réussi (+ traduction de devise)
**Faire** : ouvrir l'URL de C2, saisir le numéro de test « succès ».
**Attendu, navigateur** : la page poll (requêtes `get_status` toutes les 5 s) sans recharger, puis redirige seule vers l'exit URL.
**Attendu, serveur** : attempt `Succeeded`, `provider_status` `SUCCESSFUL`, `financialTransactionId` renseigné, `confirmed_amount`/`confirmed_currency` = 100/XAF. Session `Paid`, `paid_on` posé, `authorization` = `Done`, `authorization_tries` = 0. Un seul Integration Request, `Completed`, nommé sur l'attempt.
**Traduction de devise** (même run) : ouvrir l'Integration Request : le corps envoyé contient `EUR`, MTN répond `EUR`, mais la session reste `XAF` et `amount_mismatch` et `duplicate` restent à 0.

### D2. Refus du payeur et mauvais PIN
**Faire** : session fraîche, numéro de test « refus » ; recharger la page ; répéter avec « mauvais PIN/échec ».
**Attendu** : page en état échec après reload ; attempt `Failed`, `provider_status` = raison MTN (ex. `FAILED: ...`) ; session toujours `Open` ; `next_check_on` vide ; aucune autorisation lancée.
Si MTN laisse `PENDING` : le noter tel quel, ne pas toucher au mapping (le timeout local mène à `Unresolved`).

### D3. Réessai après échec
**Faire** : sur la session de D2 (attempt final), relancer un paiement depuis la page. Puis, pendant un attempt `Initiated`/`Pending`, tenter un second envoi.
**Attendu** : 1er cas, second attempt accepté avec un nouvel `attempt_id`. 2e cas, message « already waiting for your approval », aucune seconde ligne d'attempt.

### D4. Refus locaux
**Faire** : envoyer (a) un numéro mal formé, (b) un numéro de bonne longueur mais préfixe étranger, (c) un paiement sur une session non `Open` (déjà `Paid`, ex. celle de D1).
**Attendu** : erreur affichée sur la page, aucune ligne d'attempt créée, aucun appel sortant (rien dans Network vers MTN, aucun nouvel Integration Request).

### D5. Référence dupliquée
**Faire** : en console, rejouer `request_to_pay` avec l'`attempt_id` d'un attempt de D1 (via le client `MtnMomoClient` ou l'`initiate` du Settings).
**Attendu** : MTN répond `409` ; l'attempt reste `Initiated` et passe au polling de statut. Jamais `Error`.

### D6. Indisponibilité
**Faire** : (a) mettre une mauvaise `subscription_key` et lancer un paiement ; (b) tester une base URL injoignable via un client construit à la main en console (la validation Sandbox n'accepte que l'hôte sandbox).
**Attendu** : mapping confirmé contre le réel : unauthorized et not_sent → `Error`, timeout → `Initiated`. Chaque refus laisse un Error Log qui nomme l'outcome et le code MTN, **sans le numéro du payeur**. L'attempt reste interrogeable.
**Ensuite** : remettre la vraie `subscription_key`.

### D7. Callback
**Faire** : payer avec `send_callback` coché, tunnel actif, en surveillant les logs du tunnel et `worker.log`.
**Attendu** : noter une fois si MTN rappelle, avec quel verbe (PUT/POST), combien de fois. Un callback reçu met en file **un** job `reconcile` dédupliqué et règle l'attempt avant le poll suivant.
Test négatif :
```bash
curl -i -X PUT "https://<tunnel>/api/method/local_payments.api.mtn_momo_callback?attempt=xyz"
curl -i -X PUT "https://<tunnel>/api/method/local_payments.api.mtn_momo_callback?attempt=$(uuidgen)"
```
**Attendu** : rien ne change, même réponse vide que pour un cas valide.

### D8. Sonde du filtre de texte
**Faire** : mettre dans `payer_message` un texte accentué (« Reglement de l'annee, e accentue: é »), payer.
**Attendu** : si MTN accepte (`202`), `clean_text` peut être élargi ; s'il refuse (`400`), le filtre actuel reste. Consigner le résultat.

### D9. Scheduler seul
**Faire** : session fraîche, lancer le paiement, **fermer l'onglet** juste après l'envoi, approuver côté MTN.
**Attendu** : en un cycle du cron `*/2`, `check_due_attempts` amène l'attempt à l'état final ; l'autorisation s'exécute depuis le job (visible dans `worker.log`/`schedule.log`, sans requête `get_status`).

### D10. Non approuvé après l'échéance (le plus long, mesure l'expiration MTN)
**Faire** : `pending_timeout_minutes` à 15 (défaut restauré), numéro « pending » ou approbation jamais donnée. Observer toutes les 2-3 minutes.
**Attendu** : `Pending` tant que `expires_on` n'est pas passé ; après, `Unresolved` avec `next_check_on` posé par l'échelle (pas vidé). La page affiche « still waiting » et cesse de poller. Noter **quand MTN lui-même expire** la demande (`FAILED`, ex. `COULD_NOT_PERFORM_TRANSACTION`), puis décider : garder 15 ou changer le défaut.
Pour accélérer les autres scénarios, on peut réduire temporairement le timeout, mais **jamais** pendant cette mesure.

### D11. Forme de `reason`
**Faire** : relire le corps brut du `FAILED` de D2 (Integration Request ou GET manuel du statut).
**Attendu** : confirmer `{code, message}` comme dans l'OpenAPI.

---

## Partie E : Livrables dans le repo

1. **Fixtures** : remplacer `local_payments/tests/fixtures/mtn_momo.json` par les réponses capturées (token, `access_token`, numéros retirés). Lancer les tests existants **sans les modifier** :
   ```bash
   bench --site acad.localhost run-tests --app local_payments
   python scripts/check_pure_core.py
   ```
   Si un test casse, la réalité diffère du code : le noter, ne pas tordre le test.
2. **Code** : dans `providers/mtn_momo.py`, `reason_code()` perd sa branche « code simple » si D11 la confirme. Élargir `clean_text` uniquement si D8 le permet. Aucun mapping de statut modifié.
3. **Doc** `docs/gateways/mtn-momo.md` : retirer de « Points to confirm » les lignes confirmées (forme de la réponse status, `reason`, fixtures, rejet/PIN) ; écrire « still unknown » pour ce qui reste (prod Cameroun, min/max de montants) ; consigner quel numéro de test donne quelle issue, le délai d'expiration mesuré, la décision sur `pending_timeout_minutes` (et son `default` dans le json du doctype si changé), verbe et nombre de callbacks.
4. Revue finale avec le skill `lp-review-invariants` : aucun secret ni numéro dans fixtures, logs, doc ; `providers/` sans import `frappe`.

## Partie F : Nettoyage
Remettre `pending_timeout_minutes` à 15 et la vraie `subscription_key` ; supprimer les ToDo de test ; ne jamais lancer `migrate`/`reinstall` hors du site de dev.
