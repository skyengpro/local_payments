# Plan : local_payments cible Frappe v16 uniquement

## Contexte

L'app annonçait « Targets Frappe v15 and v16 », alors qu'elle ne tourne et n'est testée qu'en v16.
`pyproject.toml` exige Python ≥ 3.14 et la CI installe `--frappe-branch version-16`. Le bench de dev
et la CI suivaient la branche `develop` de payments.

Objectif : une seule cible, figée, pour ne plus casser au gré d'un commit de payments. frappe,
erpnext et payments sont en v16. La v15 n'est pas supportée. L'app est encore en développement :
la documentation est mise en conformité comme si ce choix avait été fait dès le départ, sans ADR.
Le changement est livré dans sa propre PR.

## Décisions (validées)

| #   | Sujet               | Décision                                                                                                                                                                                                                                                                                                    |
| --- | ------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| R-1 | Cible               | frappe `version-16`, erpnext `version-16`, payments `version-16`. Pas de v15.                                                                                                                                                                                                                              |
| R-2 | Version de payments | Épinglée par SHA sur la branche stable `version-16`, jamais sur `develop` : `cca07d9f9392e2ea0e521c5975151db9e4b6c321` (tip du 2026-05-26). payments ne publie pas de tag : le SHA garantit une installation reproductible et un point de retour connu. Les API utilisées (`create_payment_gateway`, `get_payment_gateway_controller`) sont celles de `payments/utils/utils.py` sur ce commit. |
| R-3 | Montée de payments  | On lance les tests sur le nouveau SHA de `version-16`, puis on remplace le SHA partout où il apparaît (CI, README, ARCHITECTURE). Pas d'ADR.                                                                                                                                                                |
| R-4 | Autres benchs       | Seul `frappe-bench-v14` existe à côté du bench de dev. Il est hors périmètre et aucun fichier du repo n'y renvoie.                                                                                                                                                                                          |
| R-5 | `tmp/claude-setup/` | Retiré du repo (`git rm -r`). C'était une copie périmée de `CLAUDE.md`, des rules et des skills, encore en « v15 and v16 ».                                                                                                                                                                                |
| R-6 | Organisation        | Branche `chore/target-frappe-v16-only`, rebasée sur `develop` après le merge de LP-09, PR séparée. Pas d'ADR : on corrige l'existant.                                                                                                                                                                      |

## Points touchés dans le repo

| Fichier                                        | Changement                                                                                                                                                                  |
| ---------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `CLAUDE.md`                                    | Ligne 4 : cible v16 (frappe, erpnext et payments sur `version-16`, payments épinglé par SHA). « does not exist in v15 or v16 » → « in v16 ». Vérifier les API dans les sources v16 installées. |
| `.claude/rules/reconcile-transactions.md`      | « for both v15 and v16 » → « in the installed v16 source ».                                                                                                                 |
| `.claude/skills/lp-review-invariants/SKILL.md` | Étape 4 : confirmer les API sensibles à la version dans les sources v16 installées (frappe, erpnext, payments) et citer le fichier.                                         |
| `docs/ARCHITECTURE.md`                         | payments épinglé par SHA sur `version-16`. Installation : `bench get-app payments --branch version-16`, puis `git -C apps/payments checkout <SHA>`.                          |
| `.github/workflows/ci.yml`                     | `bench get-app payments --branch version-16`, puis checkout du SHA épinglé (au lieu de `develop`).                                                                          |
| `README.md`                                    | Prérequis v16 et installation de payments au SHA épinglé avant local_payments.                                                                                              |
| `scripts/install-frappe-skills.sh`             | Commentaire : « v15/v16 » → « v16 ».                                                                                                                                        |
| `local_payments/tests/test_reconcile.py`       | La seconde connexion du test de verrou utilisait `db_name` comme utilisateur, convention v15. Elle reprend les paramètres de `frappe.connect` en v16 : `db_user` et `db_socket`. |
| `tmp/claude-setup/`                            | `git rm -r` (R-5).                                                                                                                                                          |

Laissés tels quels, et pourquoi :

- **`docs/decisions/0002-…md` (« neither in v15, nor in v16 ») :** constat historique sur
  frappe/payments#204. Un ADR n'est pas réécrit.
- **Skills communautaires `.claude/skills/frappe-*` :** génériques et tiers. La hiérarchie des
  sources de `CLAUDE.md` les place sous les sources v16 installées.
- **`local_payments/patches.txt` (lien docs v14) et commentaires de `hooks.py` :** boilerplate
  Frappe commenté, sans effet sur le support de version.
- **`pyproject.toml` :** déjà `>=3.14`, `target-version = "py314"` et commentaire `frappe~=16.0.0`.
- **`linter.yml` :** déjà en Python 3.14.

## Audit du code (dépendances v15)

Recherche dans `local_payments/` et `scripts/` : contrôles de version, fallbacks d'import,
`hasattr`/`getattr` défensifs sur `frappe`, anciennes API (`frappe.cache()`, `FrappeTestCase`,
`frappe.tests.utils`). Aucun résultat dans le code de l'app. Les tests utilisent déjà
`frappe.tests.IntegrationTestCase`. Seul écart trouvé : la connexion du test de verrou, corrigée
ci-dessus (`frappe/__init__.py`, `connect`, sur `version-16`).

## Environnement de dev

1. `bench --site lp-test.localhost backup`. C'est le seul site où payments est installé.
2. Ajout du suivi de `version-16` au remote (`remote.upstream.fetch`), puis
   `git -C apps/payments checkout -b version-16 --track upstream/version-16`.
3. `bench --site lp-test.localhost migrate`, puis `bench build --app payments`. Aucune erreur.
4. Contrôle : `git -C apps/payments log -1` affiche `cca07d9`.

Le bench de dev suit la branche `version-16`. La CI et les installations documentées sont
épinglées sur le SHA.

## Vérification

1. `bench --site lp-test.localhost run-tests --app local_payments` et les tests purs, tous verts
   sur payments `cca07d9`.
2. `python scripts/check_pure_core.py`, puis `pre-commit run` sur les fichiers touchés.
3. `grep -rnE "v15|version-15" --exclude-dir=.git .` ne renvoie plus que l'ADR 0002, les skills
   communautaires `frappe-*` et ce plan.
4. CI de la PR verte, avec payments installé au SHA épinglé.
