# Électricité de Strasbourg → Home Assistant

Intégration Home Assistant qui récupère les **CSV de consommation** envoyés par
mail par [Électricité de Strasbourg](https://www.es-energies.fr/) et les injecte
dans les **statistiques long terme**, utilisables dans le **tableau de bord Énergie**.

ES ne fournit pas d'API, mais l'espace client permet de programmer l'envoi
périodique d'un mail avec un CSV en pièce jointe. Cette intégration écoute ces
mails via l'intégration **IMAP native** de Home Assistant, décode le CSV et
alimente une statistique cumulée d'énergie — avec les vrais horodatages passés.

## Comment ça marche

```
Mail ES (CSV) ──▶ Intégration IMAP de HA ──event imap_content──▶ es_strasbourg
                                                                      │
                          imap.fetch_part (pièce jointe CSV) ◀────────┤
                                                                      │
                     parse CSV → async_add_external_statistics ───────┘
                                                                      │
                                                                      ▼
                                                         Tableau de bord Énergie
```

Le CSV contient une colonne **Index** (relevé de compteur **cumulé**, en kWh).
On pose un point de statistique par jour sur l'heure 23h–minuit (heure locale),
avec `state = Index` et `sum = Index − Index initial` (la série démarre à 0).
Home Assistant en déduit la consommation de chaque période par différence.
C'est **idempotent** : réimporter un jour écrase proprement sa valeur (utile
quand une valeur *estimée* est remplacée plus tard par une *réelle*).

## Prérequis

- Home Assistant **2026.9** ou plus récent (version testée).
- [HACS](https://hacs.xyz/) pour l'installation.
- L'intégration **`recorder`** active (par défaut).
- Un compte mail accessible en **IMAP** (idéalement une adresse dédiée).
- L'envoi périodique du CSV programmé depuis l'espace client ES (par ex. chaque
  semaine).

## Installation (HACS)

1. Dans HACS : menu **⋮ → Dépôts personnalisés**, ajoutez
   `https://github.com/neurones67/es-homeassistant` avec le type **Intégration**.
2. Recherchez **Électricité de Strasbourg** dans HACS, puis **Télécharger**.
3. Redémarrez Home Assistant.

<details>
<summary>Installation manuelle</summary>

Copiez le dossier `custom_components/es_strasbourg` dans
`<config>/custom_components/`, puis redémarrez Home Assistant.
</details>

## Configuration

### 1. Intégration IMAP (réception des mails)

> Gmail : activez la **validation en deux étapes** puis créez un
> **mot de passe d'application** (16 caractères) — c'est lui qu'on utilise en
> IMAP, jamais votre mot de passe Google principal.

Ajoutez l'intégration **IMAP** (Paramètres → Appareils et services → Ajouter) :

| Champ | Valeur (Gmail) |
|-------|----------------|
| Serveur | `imap.gmail.com` |
| Port | `993` |
| Nom d'utilisateur | `votre.adresse@gmail.com` |
| Mot de passe | *mot de passe d'application* |
| Dossier | `INBOX` (ou un label dédié, voir ci-dessous) |
| Critère de recherche | `UnSeen UnDeleted` (défaut) — ou ciblez ES, ex. `FROM "strasbourg-electricite-reseaux.fr" UnDeleted` |

> **Astuce Gmail** : créez un filtre qui applique un label `ES` aux mails d'ES,
> et pointez le dossier IMAP sur `ES`. Les mails restent bien identifiés et le
> critère de recherche peut rester simple.

L'intégration IMAP émet alors un événement `imap_content` à chaque nouveau mail
correspondant. **Aucune automation à écrire** : `es_strasbourg` écoute cet
événement directement.

### 2. Intégration Électricité de Strasbourg

Paramètres → Appareils et services → **Ajouter une intégration** →
**Électricité de Strasbourg**. L'assistant propose :

| Champ | Rôle |
|-------|------|
| Compte IMAP | Boîte qui reçoit les CSV d'ES. Vide = tous les comptes IMAP. |
| Filtre sur l'expéditeur | Texte cherché dans l'adresse de l'expéditeur (défaut `strasbourg` ; ES envoie depuis `vos-mesures-maconsolinky@strasbourg-electricite-reseaux.fr`). Vide = tout expéditeur. |
| Nom de la statistique | Nom affiché dans le tableau Énergie. |

Ces réglages se modifient ensuite via **Configurer** sur l'intégration.

### 3. Chargement de l'historique initial (backfill)

Les mails ne contiennent que les derniers jours. Pour charger l'historique,
téléchargez un export depuis l'espace client ES, puis injectez-le en une fois :

1. Copiez votre CSV dans un dossier dédié, par ex.
   `<config>/es_strasbourg/export.csv`.
2. Autorisez ce dossier dans `configuration.yaml`, puis redémarrez. Home
   Assistant n'autorise **pas** la lecture du dossier de config lui-même, et
   `www/` est à éviter : son contenu est servi publiquement sous `/local/`.

   ```yaml
   homeassistant:
     allowlist_external_dirs:
       - /config/es_strasbourg
   ```

3. Dans **Outils de développement → Actions**, appelez :

   ```yaml
   action: es_strasbourg.import_csv
   data:
     path: /config/es_strasbourg/export.csv   # optionnel pour ce nom par défaut
   ```

   La réponse indique le nombre de points importés.

### 4. Ajout au tableau de bord Énergie

Paramètres → Tableaux de bord → **Énergie** → *Ajouter une consommation* →
choisissez **Consommation Électricité de Strasbourg**.

> La statistique étant *externe*, elle apparaît sous son nom configuré. Le coût
> peut être associé via un tarif fixe/entité dans la config Énergie si souhaité.

## Détails techniques du format CSV

- Séparateur `;`, encodage **Windows-1252** (parfois UTF-8) — géré automatiquement.
- Colonnes : `Date` (`JJ-MM-AAAA`), `Nature relève` (`evt réelle` / `estimée`),
  `Consommation (kWh)`, `Index`.
- L'`Index` est cumulé, séparateur de milliers = espace (`20 007` → `20007`).
- Invariant vérifié sur les données réelles :
  `Consommation(J) == Index(J+1) − Index(J)`.
- Certains exports (après un **changement de compteur**) alignent **plusieurs
  couples `Consommation;Index`** : les données basculent d'un couple au suivant à
  une date donnée, l'`Index` restant continu. Les couples sont détectés depuis
  l'en-tête et fusionnés automatiquement (on retient le couple dont l'`Index` est
  renseigné pour le jour).
- Le jour courant peut n'avoir qu'un Index (consommation vide) : il est importé
  quand même (l'Index suffit pour la statistique cumulée).
- `NC` (« non communiqué ») remplace parfois un nombre : traité comme absent. Un
  jour sans Index est sauté ; sa consommation est comptée au jour connu suivant.
- Dans les mails, ES déclare le CSV en `application/pdf` et encode son nom
  (`=?UTF-8?Q?Consommation=5fJournaliere.csv?=`) : la pièce jointe est donc
  repérée par son nom décodé, pas par son type MIME.

## Tests

Le parseur se teste sans Home Assistant :

```bash
python3 tests/test_csv_parser.py
```

Les tests complets (`tests/test_integration.py`) démarrent un Home Assistant de
test avec un recorder SQLite en mémoire. Ils vérifient, jour par jour, que la
consommation vue par le tableau Énergie correspond au CSV (y compris pour des
imports successifs qui se chevauchent, et pour la réception simulée d'un mail IMAP).
`tests/test_config_flow.py` couvre l'assistant et l'écran Options. Les mêmes tests,
ainsi que les validations HACS et hassfest, tournent sur GitHub Actions
(`.github/workflows/validate.yml`) :

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements_test.txt
.venv/bin/python -m pytest -q
```

### Instance de test (podman)

`scripts/ha-dev.sh` lance un Home Assistant jetable avec l'intégration montée
depuis `custom_components/` (config dans `.ha-dev/`, CSV d'exemple copiés dans
`/config/es_strasbourg/`) :

```bash
scripts/ha-dev.sh start      # http://localhost:8123
scripts/ha-dev.sh logs es    # logs filtrés es_strasbourg / imap
scripts/ha-dev.sh restart    # après une modification du code
scripts/ha-dev.sh reset      # tout effacer (compte, statistiques)
```

## Limitations

- **Granularité journalière** : ES ne fournit (option Base) qu'un relevé par jour.
  Les barres horaires du tableau Énergie regrouperont donc la conso du jour sur
  la tranche 23h–minuit ; les vues journalières/mensuelles sont exactes.
- **Importez l'historique le plus ancien en premier** : la série démarre à 0 au
  premier import. Un import ultérieur de jours *antérieurs* produirait des
  valeurs négatives au début de la série.
- **Trous entre deux imports** : si des jours manquent entre deux CSV, toute leur
  consommation apparaît sur le premier jour suivant le trou. Importez un export
  couvrant la période manquante pour la répartir correctement.
- Contrats **HP/HC** : non gérés ici (l'export d'exemple est en option Base, une
  seule série). Le format aurait des colonnes supplémentaires à mapper sur deux
  statistiques.

## Structure du dépôt

```
custom_components/es_strasbourg/   # l'intégration (assistant : config_flow.py)
tests/                             # tests parseur, intégration, assistant
scripts/ha-dev.sh                  # instance HA jetable (podman/docker)
hacs.json                          # métadonnées HACS
requirements_test.txt              # dépendances de test (versions figées)
scripts/generate_sample_csv.py     # génère les CSV d'exemple
export_consommations_energie.csv   # export d'exemple (synthétique)
Consommation_Journaliere.csv       # export d'exemple (synthétique, changement de compteur)
```

Les CSV d'exemple ont exactement le format des exports ES, mais leurs valeurs
sont **inventées** (profil saisonnier + bruit à graine fixe). Pour les
régénérer : `python3 scripts/generate_sample_csv.py`.

## Licence

[MIT](LICENSE).
