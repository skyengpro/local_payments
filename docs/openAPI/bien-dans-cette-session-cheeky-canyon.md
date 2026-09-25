ar

# Plan : local_payments cible Frappe v16 uniquement

## Contexte

L'app annonce « Targets Frappe v15 and v16 », alors qu'elle ne tourne et n'est testée qu'en v16.
`pyproject.toml` exige Python ≥ 3.14 et la CI installe `--frappe-branch version-16`. L'utilisateur
a déjà écrit « Code must work on v16 » dans `CLAUDE.md`, sans décision tracée. La review de LP-09
l'a relevé : règle changée hors périmètre, contradiction avec la ligne 4.

Objectif : une seule cible à jour, pour ne plus casser au gré d'une version de paquet. frappe,
erpnext et payments suivent tous `version-16`. La v15 n'est plus supportée. Le changement est tracé
par l'ADR 0003 et livré dans sa propre PR.

## Décisions (validées)

| #   | Sujet                     | Décision                                                                                                                                                                                                                                                                                                                                                                          |
| --- | ------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| R-1 | Cible                     | frappe`version-16`, erpnext `version-16`, payments `version-16`. Plus de v15.                                                                                                                                                                                                                                                                                                |
| R-2 | Branche de payments       | `version-16` (protégée, alimentée par backports, tip `cca07d9` du 2026-05-26), vérifiée par `git ls-remote` et l'API GitHub. Le bench de dev et la CI sont aujourd'hui sur `develop` : on les aligne. Les API utilisées par l'app (`create_payment_gateway`, `get_payment_gateway_controller`) sont identiques sur `version-16` (`payments/utils/utils.py`). |
| R-3 | Bench`frappe-bench-v15` | Gardé sur disque, jamais référencé ni utilisé. Rien à modifier : aucun fichier (workspace,`.claude/settings.json`) n'y renvoie.                                                                                                                                                                                                                                            |
| R-4 | `tmp/claude-setup/`     | Retiré du repo (`git rm -r`). C'est une copie périmée de `CLAUDE.md`, des rules et des skills, encore en « v15 and v16 ».                                                                                                                                                                                                                                                 |
| R-5 | Organisation              | PR séparée + ADR 0003. LP-09 ne garde dans`CLAUDE.md` que la ligne sur le commit. Les autres modifications de l'utilisateur dans `CLAUDE.md` passent dans cette réforme.                                                                                                                                                                                                    |

## Étape 0 : sortir la réforme de LP-09 (branche LP-09)

1. Copier le `CLAUDE.md` actuel (modifs de l'utilisateur + ligne commit) dans le scratchpad.
2. `git checkout HEAD -- CLAUDE.md` (désindexe et restaure), puis rajouter seulement la phrase
   sur `api._open_attempt` à la règle des commits.
3. La PR LP-09 reste à pousser par l'utilisateur.

## Étape 1 : branche de la réforme

`chore/target-frappe-v16-only`, créée depuis `develop` une fois LP-09 mergée. Si l'utilisateur veut
la lancer avant, on la crée depuis la pointe de LP-09 et on la rebase sur `develop` après le merge
(`ARCHITECTURE.md` et `CLAUDE.md` sont touchés des deux côtés).

## Étape 2 : points touchés dans le repo

| Fichier                                                     | Changement                                                                                                                                                                                                                                                                                                                                                                     |
| ----------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `docs/decisions/0003-target-frappe-v16-only.md` (nouveau) | ADR au format du repo (skill`lp-write-adr`) : contexte (double cible jamais testée, py ≥ 3.14, CI v16, payments sur develop), décision R-1/R-2, conséquences (v15 refusée, montée de version = nouvel ADR), options écartées (garder v15, suivre develop).                                                                                                           |
| `CLAUDE.md`                                               | Ligne 4 : « Targets Frappe v16 (frappe, erpnext and payments on`version-16`, ADR 0003) ». Réappliquer les modifs de l'utilisateur (« Code must work on v16 ») en corrigeant l'espace manquante après `scheduler.py`, et l'espace en fin de ligne. « does not exist in v15 or v16 » → « in v16 ». Méthode : vérifier les API dans les sources v16 installées. |
| `.claude/rules/reconcile-transactions.md`                 | « for both v15 and v16 » → « in the installed erpnext source (`version-16`) ».                                                                                                                                                                                                                                                                                          |
| `.claude/skills/lp-review-invariants/SKILL.md`            | Étape 4 : confirmer les API dans les sources v16 installées (frappe, erpnext, payments sur`version-16`) et citer le fichier. Plus de comparaison v15/v16.                                                                                                                                                                                                                  |
| `docs/ARCHITECTURE.md`                                    | l. 41 : payments suivi sur`version-16` (ADR 0003). l. 506 : `bench get-app payments --branch version-16` seul, sans « or version-15 ». « Related documents » : ajouter l'ADR 0003.                                                                                                                                                                                     |
| `.github/workflows/ci.yml`                                | l. 93 :`bench get-app payments --branch version-16` (au lieu de `develop`).                                                                                                                                                                                                                                                                                                |
| `README.md`                                               | Prérequis : Frappe v16 et payments`version-16`. Installation : `bench get-app payments --branch version-16` avant local_payments.                                                                                                                                                                                                                                         |
| `scripts/install-frappe-skills.sh`                        | Commentaire l. 46 : « v15/v16 » → « v16 ».                                                                                                                                                                                                                                                                                                                                |
| `tmp/claude-setup/`                                       | `git rm -r` (R-4).                                                                                                                                                                                                                                                                                                                                                           |

Laissés tels quels, et pourquoi :

- **`docs/decisions/0002-…md` (« neither in v15, nor in v16 ») :** c'est un constat historique. Un
  ADR n'est pas réécrit.
- **Skills communautaires `.claude/skills/frappe-*` :** génériques et tiers. La hiérarchie des
  sources de `CLAUDE.md` les place déjà sous les sources v16 installées.
- **`local_payments/patches.txt` (lien docs v14) :** simple lien de boilerplate Frappe, il ne
  touche pas au support de version.
- **`pyproject.toml` :** déjà `>=3.14`, `target-version = "py314"` et commentaire `frappe~=16.0.0`.
- **`linter.yml` :** déjà en Python 3.14.

## Étape 3 : environnement de dev

1. `bench --site lp-test.localhost backup`. C'est le seul site où payments est installé
   (vérifié avec `list-apps`).
2. `bench switch-to-branch version-16 payments`, avec `--upgrade` seulement si bench le demande.
3. `bench --site lp-test.localhost migrate` (site de dev), puis `bench build --app payments`.
4. Contrôle : `git -C apps/payments branch --show-current` affiche `version-16`.

Risque : passer de `develop` à `version-16` revient en arrière sur payments, ce que la migration ne
défait pas (colonnes ajoutées sur develop). Si `migrate` ou les tests cassent, on restaure la
sauvegarde, ou on réinstalle ce site de test.

## Vérification

1. `bench --site lp-test.localhost run-tests --app local_payments` et les tests purs, tous verts
   sur payments `version-16`.
2. `python scripts/check_pure_core.py`, puis `pre-commit run` sur les fichiers touchés.
3. `grep -rnE "v15|version-15" --exclude-dir=.git .`, qui ne doit plus renvoyer que l'ADR 0002
   et les skills communautaires `frappe-*`.
4. CI de la PR verte, avec payments installé en `version-16`.
5. Revue avec `lp-review-invariants` (étape 4 mise à jour) et `/humanizer` sur l'ADR et la doc.
