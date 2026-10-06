"""Constantes de l'intégration Électricité de Strasbourg."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "es_strasbourg"

# Options de l'entrée de configuration (assistant et écran Options).
CONF_IMAP_ENTRY = "imap_entry"
CONF_SENDER = "sender"
CONF_NAME = "name"
# Puissance souscrite (kVA) : active le calcul du coût au tarif bleu Base.
CONF_POWER = "power_kva"

# Filtre par défaut sur l'expéditeur du mail (sous-chaîne, insensible à la casse).
# ES envoie depuis vos-mesures-maconsolinky@strasbourg-electricite-reseaux.fr.
DEFAULT_SENDER = "strasbourg"

# Identifiant de la statistique externe. Doit contenir « : » et le préfixe
# (la « source ») doit valoir le domaine de l'intégration.
DEFAULT_STATISTIC_ID = f"{DOMAIN}:consumption"
DEFAULT_NAME = "Consommation Électricité de Strasbourg"

UNIT_KWH = "kWh"
UNIT_EUR = "EUR"

# Coût TTC (kWh + abonnement) au tarif bleu réglementé, option Base.
COST_STATISTIC_ID = f"{DOMAIN}:cost"
COST_NAME_SUFFIX = " (coût)"

# Historique open data de la CRE ; une copie est embarquée dans data/ en secours.
CRE_TARIFF_URL = (
    "https://www.cre.fr/fileadmin/Documents/Open_data/Marches_de_detail/Option_Base.csv"
)
EMBEDDED_TARIFF_FILE = "data/cre_option_base.csv"
# Les tarifs changent en février et en août : un contrôle hebdomadaire suffit
# (le barème est aussi vérifié à chaque démarrage).
TARIFF_REFRESH_INTERVAL = timedelta(days=7)

# Intégration IMAP native de HA : événement émis et domaine de ses entrées.
IMAP_DOMAIN = "imap"
IMAP_EVENT = "imap_content"

# Types MIME / extensions considérés comme la pièce jointe CSV.
CSV_CONTENT_TYPES = frozenset(
    {
        "text/csv",
        "application/csv",
        "application/vnd.ms-excel",
        "application/octet-stream",
    }
)

# Service de backfill manuel depuis un fichier.
SERVICE_IMPORT_CSV = "import_csv"
# Remise à zéro : supprime toute la statistique importée.
SERVICE_CLEAR_STATISTICS = "clear_statistics"
ATTR_PATH = "path"
# Relatif au dossier de config. Ce dossier doit figurer dans
# allowlist_external_dirs (le dossier de config ne l'est pas par défaut).
DEFAULT_IMPORT_PATH = f"{DOMAIN}/export.csv"
