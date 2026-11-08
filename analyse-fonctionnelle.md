# SkyEng Immo : analyse fonctionnelle

Version analysée : `skyeng_immo` 0.4.0 (commit `c086af9`), le 2026-10-12.

Ce document décrit ce que fait le code de l'application. Il a été établi par lecture des sources (hooks, install, DocTypes, tests, fixtures, CI), ainsi que des DocTypes PropMS `Property` et `Lease` auxquels l'app se rattache. Il n'a pas été vérifié sur un site en fonctionnement.

---

## 1. Ce qu'est l'application

**SkyEng Immo** ajoute une couche « vie de la résidence » à **PropMS** (gestion locative) et **ERPNext** (finance, actifs). Les deux sont obligatoires (`required_apps`).

Sa raison d'être : PropMS gère le **bail**, avec un seul Customer par unité, mais ne sait pas **qui vit réellement** dans le logement. Sans cette information, on ne peut ni contrôler les accès ni tenir un registre de guérite. SkyEng Immo ajoute cette couche sans modifier PropMS :

- les **occupants** de chaque unité ;
- le **registre des visiteurs** de la guérite ; 
- les **véhicules** et les **places de parking** ;
- un **suivi des équipements**, en ajoutant deux champs à l'`Asset` d'ERPNext ;
- un **tableau de bord « Immobilier »** qui regroupe patrimoine, finances, sécurité et maintenance (montants en XAF).

Tout est dans un seul module, `Immobilier`.

---

## 2. Les parties concernées

| Partie                       | Comment elle est représentée                                                                                                                           | Où                        |
| ---------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------- |
| **Propriétaire**      | Pas de DocType propre. C'est un`Customer` dans `Property.unit_owner`, et un champ `Lease.property_owner`                                           | PropMS                     |
| **Locataire (payeur)** | Le`Customer` du bail (`Lease.customer`). Dans l'Occupant, il apparaît comme « Locataire de référence », recopié automatiquement depuis le bail | PropMS, lu par skyeng_immo |
| **Occupant**           | Toute personne qui vit dans l'unité : Locataire titulaire, Conjoint(e), Enfant, Parent, Colocataire, Employé(e) de maison, Autre                       | **`Occupant`**     |
| **Visiteur**           | Personne de passage, avec un hôte, un motif, un badge et un éventuel véhicule                                                                         | **`Visitor Log`**  |
| **Gardien**            | Agent de guérite                                                                                                                                        | Rôle`Gardien`           |
| **Gestionnaire**       | Gestion locative complète                                                                                                                               | Rôle`Property Manager`  |
| **Technicien**         | Maintenance des équipements                                                                                                                             | Rôle`Technicien`        |

Le point clé : **le payeur et les habitants sont distincts**. Un bail a un seul Customer, mais une unité peut avoir plusieurs Occupants.

---

## 3. Rôles et permissions

Les trois rôles métier sont créés par du code ([install.py](../skyeng_immo/install.py)), à l'installation et à chaque `migrate`.

| DocType                  | System Manager | Property Manager                         | Gardien                            |
| ------------------------ | -------------- | ---------------------------------------- | ---------------------------------- |
| Occupant                 | Tout           | Créer / lire / modifier (pas supprimer) | **Lecture**                  |
| Visitor Log              | Tout           | Créer / lire / modifier                 | **Créer / lire / modifier** |
| Vehicle                  | Tout           | Créer / lire / modifier                 | Lecture                            |
| Parking Space            | Tout           | Créer / lire / modifier                 | Lecture                            |
| Property, Lease (PropMS) | Tout           | Écriture                                | **Aucune**                   |
| 12 rapports              | Accès         | **Aucun**                          | **Aucun**                    |

Le **Technicien** n'a de permission sur aucun DocType de l'app. Il est seulement prévu pour les équipes `Asset Maintenance Team` d'ERPNext.

En résumé, le Gardien saisit les entrées et sorties et consulte les résidents, le Property Manager administre tout sauf les suppressions, et le System Manager a tous les droits.

---

## 4. Les DocTypes et leur rôle

### 4.1 Occupant (`OCC-AAAA-#####`), « qui habite où »

- **Obligatoires** : nom complet, qualité, **unité** (`Property`), date d'entrée.
- **Facultatifs** : bail, pièce d'identité (type, numéro, expiration, scan), photo, coordonnées, personne à prévenir.
- **Règles** ([occupant.py](../skyeng_immo/immobilier/doctype/occupant/occupant.py)) :
  - la date de sortie ne peut pas précéder la date d'entrée ;
  - si un bail est renseigné, le **locataire de référence** est recopié depuis le bail, et l'unité doit être celle du bail (elle est remplie automatiquement si elle est vide) ;
  - le **statut** est calculé : `Parti` si la date de sortie est passée ou égale à aujourd'hui, sinon `Actif` ;
  - si la pièce d'identité a expiré, un **simple avertissement** s'affiche : refuser l'accès reste une décision du gardien.
- `get_active_occupants(unit)` liste les personnes autorisées dans une unité, pour la guérite.

### 4.2 Visitor Log (`VIS-AAAA-#####`), le registre de guérite

- **Obligatoires** : nom du visiteur, **unité visitée**, motif (Visite privée, Livraison, Prestataire / Entretien, Commercial, Visite de bien, Autre), heure d'entrée (par défaut maintenant).
- **Facultatifs** : hôte (un Occupant), pièce d'identité, badge remis, immatriculation, agent de garde.
- **Règles** ([visitor_log.py](../skyeng_immo/immobilier/doctype/visitor_log/visitor_log.py)) :
  - l'heure d'entrée ne peut pas être dans le futur, et la sortie doit être après l'entrée ;
  - **l'hôte doit habiter l'unité visitée**, sinon l'enregistrement est refusé ; un hôte « Parti » déclenche seulement un avertissement ;
  - statut et durée calculés : `A l'interieur` tant qu'il n'y a pas de sortie, puis `Sorti` avec la durée en minutes.
- API : `check_out_visitor` (sortie en un clic, refuse une double sortie) et `currently_inside` (qui est encore sur le site).

### 4.3 Parking Space (`{immeuble}-P{numéro}`)

- Rattachée à un **immeuble ou site** ; on renseigne aussi le niveau, le type (couverte, non couverte, box fermé, PMR) et la redevance mensuelle.
- **Attribution** : uniquement à une **unité du même immeuble**, ou à la propriété elle-même dans le cas d'une villa autonome. Si un bail est indiqué, il doit porter sur l'unité attributaire.
- **Statut** : `Attribuee` si la place a une unité, sinon `Libre`. `Hors service` et `Reservee visiteur` sont conservés s'ils sont déjà posés.
- API : `parking_summary` donne le nombre de places et de véhicules actifs par unité.
- Code : [parking_space.py](../skyeng_immo/immobilier/doctype/parking_space/parking_space.py).

### 4.4 Vehicle (nommé par l'immatriculation)

- Appartient à un **Occupant**, et l'unité est recopiée depuis cet occupant.
- L'immatriculation est **normalisée** en majuscules avec des espaces uniques.
- **Une place = un seul véhicule actif**, et la place doit être attribuée à l'unité du propriétaire du véhicule.
- Si l'occupant est « Parti », un avertissement s'affiche.
- API `lookup_plate` (contrôle à la barrière) : on obtient le véhicule de résident avec son propriétaire, sinon le visiteur actuellement à l'intérieur avec cette plaque, sinon « inconnu ».
- Code : [vehicle.py](../skyeng_immo/immobilier/doctype/vehicle/vehicle.py).

### 4.5 Extensions d'ERPNext `Asset` (équipements)

Deux champs sont ajoutés par code ([install.py](../skyeng_immo/install.py)) :

- `warranty_expiry_date` : ERPNext ne suit la garantie que sur Serial No ;
- `immo_property` : relie l'équipement (ascenseur, groupe électrogène…) à son unité ou son site.

### 4.6 DocTypes externes utilisés (non modifiés)

- **PropMS** : `Property` (immeubles et unités, en arbre), `Lease`, `Lease Invoice Schedule`, `Meter` / `Meter Reading`, `Daily Checklist`, `Insurance`.
- **ERPNext** : `Sales Invoice`, `Payment Entry`, `Asset`, `Asset Maintenance` / `Asset Repair`.

---

## 5. Les flux de fonctionnement

**① Installation.** `install-app` refuse de s'installer sans ERPNext et PropMS. Il crée ensuite les rôles et les champs Asset, puis charge les fixtures : 17 indicateurs, 7 graphiques, 12 rapports SQL, le workspace, la barre latérale et l'icône « Immobilier ».

**② Patrimoine (PropMS).** Le gestionnaire crée l'immeuble (`Property` groupe), puis ses unités, chacune avec un propriétaire (`unit_owner`). Il crée ensuite les places de parking de l'immeuble.

**③ Mise en location (PropMS).** Le gestionnaire crée un `Lease` (unité, Customer, échéancier). PropMS génère les `Sales Invoice`, puis les `Payment Entry` enregistrent les paiements. SkyEng Immo **ne fait que lire** ces données pour les tableaux de bord et les rapports.

**④ Emménagement.** Le gestionnaire crée un **Occupant** par personne du foyer, en le liant à l'unité et au bail. Il attribue une ou plusieurs places à l'unité, puis enregistre les **véhicules** des occupants sur ces places.

**⑤ Guérite, arrivée d'un visiteur.**

1. Le Gardien consulte les occupants actifs de l'unité.
2. Il crée un **Visitor Log** : unité, hôte, motif, pièce d'identité, badge, plaque.
3. Le système vérifie que l'hôte habite bien l'unité.

**⑥ Guérite, barrière.** Le Gardien saisit une plaque (`lookup_plate`) et obtient soit le résident et sa place, soit le visiteur en cours, soit « inconnu ».

**⑦ Guérite, départ.** `check_out_visitor` enregistre l'heure de sortie et la durée. `currently_inside` (et l'indicateur « Visiteurs présents ») montre qui est encore sur le site.

**⑧ Déménagement.** Le gestionnaire renseigne la date de sortie, et l'occupant passe à `Parti` lors de l'enregistrement. L'occupant n'est plus hôte valide, et ses véhicules déclenchent un avertissement.

**⑨ Équipements.** Un `Asset` est rattaché à une unité, avec une date de fin de garantie. Les rapports « Équipements par bien », « Maintenance des équipements » et « Historique des réparations » indiquent si l'équipement est encore sous garantie au moment de la panne.

**⑩ Pilotage.** Le workspace « Immobilier » est organisé en sections : Patrimoine & occupation, Finances (facturé, encaissé, impayés), Exploitation & sécurité, Analyses, Accès rapide et Rapports.

### Rapports livrés

| Rapport                      | Source                 | Contenu                                                             |
| ---------------------------- | ---------------------- | ------------------------------------------------------------------- |
| Taux d'occupation            | Property               | Unités louées / libres et loyer potentiel / effectif par immeuble |
| Rent Roll Immobilier         | Lease                  | Tous les baux : unité, locataire, dates, loyer mensuel, caution    |
| Échéances des baux         | Lease                  | Baux actifs triés par date de fin, jours restants, préavis        |
| Impayés par locataire       | Sales Invoice          | Montant impayé et retard par Customer                              |
| Historique des paiements     | Sales Invoice          | Factures, montant réglé et solde par unité                       |
| Occupants par unité         | Occupant               | Nombre d'occupants actifs, enfants, liste des personnes             |
| Registre des visiteurs       | Visitor Log            | Entrées / sorties, hôte, motif, véhicule, agent                  |
| Équipements par bien        | Asset                  | Équipements par unité, valeur, état de la garantie               |
| Maintenance des équipements | Asset Maintenance Task | Tâches et prochaines échéances                                   |
| Historique des réparations  | Asset Repair           | Pannes, coût, prise en charge sous garantie                        |
| Consommation des compteurs   | Meter Reading          | Index et consommation par compteur                                  |
| Historique de maintenance    | Daily Checklist        | Contrôles quotidiens par bien et zone                              |

---

## 6. Écarts entre l'intention et le comportement réel

Ces points sont relevés à la lecture du code ; ils n'ont pas été reproduits sur un site.

1. **Conflit de nom `Vehicle`** : ERPNext définit déjà un DocType `Vehicle` (module Setup, nommé par `license_plate`, utilisé par Delivery Trip / HRMS). Celui de skyeng_immo porte le même nom. L'un écrase l'autre à la synchronisation. **C'est le risque le plus sérieux.**
2. **Les rapports ne sont accessibles qu'au System Manager** : le Property Manager et le Gardien ne peuvent pas les ouvrir.
3. **Le Gardien n'a aucun droit sur `Property`** : il ne pourra probablement pas rechercher l'unité visitée dans le champ lien du Visitor Log.
4. **Les statuts ne sont pas recalculés avec le temps** : il n'y a pas de tâche planifiée. Un occupant dont la date de sortie (future) est passée reste « Actif » tant qu'on ne réenregistre pas sa fiche. Rien ne libère non plus sa place ni ne désactive ses véhicules.
5. **`Hors service` et `Reservee visiteur` ne peuvent pas être posés depuis le formulaire**, car le champ statut de Parking Space est en lecture seule.
6. **Aucun fichier JS** : le filtre « hôte = occupants actifs de l'unité », annoncé dans la description du champ, n'existe pas. Seule la validation côté serveur protège.
7. **Les API whitelistées ne vérifient pas les permissions** (`lookup_plate`, `currently_inside`, `parking_summary`) : tout utilisateur connecté peut lire les plaques et les visiteurs présents.
8. **L'indicateur « Hors garantie » a une date figée** (`< 2026-08-13`) au lieu de la date du jour.
9. **Le nom d'un véhicule est fixé avant la normalisation** : saisir « lt-123-a » donne un nom `lt-123-a` mais une plaque `LT-123-A`.
10. **Le README est en retard** : il ne cite que 2 DocTypes sur 4.
