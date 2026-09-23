"""Intégration Électricité de Strasbourg pour Home Assistant.

Récupère les CSV de consommation envoyés par mail (via l'intégration IMAP native)
et les injecte dans les statistiques long terme, exploitables par le tableau de
bord Énergie.

Deux voies d'alimentation :
  * automatique : écoute l'événement ``imap_content`` émis par l'intégration IMAP,
    télécharge la pièce jointe CSV et l'importe ;
  * manuelle : service ``es_strasbourg.import_csv`` pour un backfill depuis un
    fichier (utile pour l'historique initial).

La configuration se fait dans l'interface (config_flow.py), une seule instance.
"""

from __future__ import annotations

import asyncio
import base64
from email.errors import HeaderParseError
from email.header import decode_header, make_header
import logging

import voluptuous as vol

from homeassistant.components.recorder import get_instance
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.helpers.typing import ConfigType

from .const import (
    ATTR_PATH,
    CONF_IMAP_ENTRY,
    CONF_NAME,
    CONF_SENDER,
    CSV_CONTENT_TYPES,
    DEFAULT_IMPORT_PATH,
    DEFAULT_NAME,
    DEFAULT_STATISTIC_ID,
    DOMAIN,
    IMAP_DOMAIN,
    IMAP_EVENT,
    SERVICE_CLEAR_STATISTICS,
    SERVICE_IMPORT_CSV,
)
from .csv_parser import parse_csv
from .statistics import async_import_records

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

# Durée maximale d'attente de la suppression par le recorder (secondes).
CLEAR_STATISTICS_TIMEOUT = 30

IMPORT_CSV_SCHEMA = vol.Schema({vol.Optional(ATTR_PATH): cv.string})


def _decode_filename(raw: str) -> str:
    """Décode un nom de pièce jointe encodé MIME (RFC 2047), en minuscules.

    L'intégration IMAP transmet le nom brut, ex. ES envoie
    ``=?UTF-8?Q?Consommation=5fJournaliere.csv?=``.
    """
    try:
        return str(make_header(decode_header(raw))).lower()
    except (HeaderParseError, UnicodeError, LookupError):
        return raw.lower()


def _select_csv_part(parts: dict) -> str | None:
    """Renvoie la clé de la pièce jointe CSV dans le dict `parts` de l'event.

    Le nom de fichier prime : ES déclare son CSV en ``application/pdf``. Le type
    MIME ne sert qu'en repli, pour une pièce jointe sans nom exploitable.
    """
    for key, meta in parts.items():
        if _decode_filename(meta.get("filename") or "").endswith(".csv"):
            return key
    for key, meta in parts.items():
        if (meta.get("content_type") or "").lower() in CSV_CONTENT_TYPES:
            return key
    return None


def _decode_part(response: dict) -> bytes | None:
    """Décode `part_data` de imap.fetch_part selon son content-transfer-encoding."""
    raw = response.get("part_data")
    if raw is None:
        _LOGGER.error("Réponse imap.fetch_part sans part_data : %s", response)
        return None
    encoding = (response.get("content_transfer_encoding") or "").lower()
    if encoding == "base64":
        try:
            return base64.b64decode(raw)
        except (ValueError, TypeError):
            _LOGGER.exception("Décodage base64 de la pièce jointe impossible")
            return None
    if isinstance(raw, str):
        # 7bit / 8bit / quoted-printable simple : on repasse en octets bruts.
        return raw.encode("latin-1", errors="replace")
    return raw


async def _async_import_content(hass: HomeAssistant, content: bytes, name: str) -> int:
    records = await hass.async_add_executor_job(parse_csv, content)
    if not records:
        _LOGGER.warning("CSV ES vide ou non reconnu")
        return 0
    return await async_import_records(hass, records, DEFAULT_STATISTIC_ID, name)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Enregistre les actions d'import manuel et de remise à zéro."""

    async def _handle_import_csv(call: ServiceCall) -> dict:
        entries = hass.config_entries.async_loaded_entries(DOMAIN)
        if not entries:
            raise ServiceValidationError(
                "Intégration Électricité de Strasbourg non configurée : ajoutez-la "
                "dans Paramètres → Appareils et services."
            )
        name = entries[0].options.get(CONF_NAME, DEFAULT_NAME)

        # Un chemin relatif part du dossier de config (un chemin absolu est
        # conservé tel quel) : « es_strasbourg/x.csv » = /config/es_strasbourg/x.csv.
        path = hass.config.path(call.data.get(ATTR_PATH) or DEFAULT_IMPORT_PATH)
        # Le dossier de config n'est PAS autorisé d'office : seuls www/, les
        # dossiers média et allowlist_external_dirs le sont.
        if not await hass.async_add_executor_job(hass.config.is_allowed_path, path):
            allowed = ", ".join(sorted(hass.config.allowlist_external_dirs))
            raise ServiceValidationError(
                f"Chemin non autorisé : {path}. Dossiers autorisés : {allowed}. "
                "Ajoutez le dossier du fichier à homeassistant > "
                "allowlist_external_dirs dans configuration.yaml (ex. "
                f"{hass.config.path(DOMAIN)}), puis redémarrez Home Assistant."
            )

        def _read() -> bytes:
            with open(path, "rb") as file:
                return file.read()

        try:
            content = await hass.async_add_executor_job(_read)
        except OSError as err:
            raise ServiceValidationError(f"Lecture impossible de {path} : {err}") from err

        count = await _async_import_content(hass, content, name)
        return {"imported": count, "path": path}

    async def _handle_clear_statistics(call: ServiceCall) -> None:
        """Supprime tout l'historique importé, pour repartir d'un import propre."""
        done = asyncio.Event()
        get_instance(hass).async_clear_statistics(
            [DEFAULT_STATISTIC_ID],
            on_done=lambda: hass.loop.call_soon_threadsafe(done.set),
        )
        # On attend la fin réelle : un import lancé juste après repart de zéro.
        async with asyncio.timeout(CLEAR_STATISTICS_TIMEOUT):
            await done.wait()
        _LOGGER.warning("Statistique « %s » supprimée", DEFAULT_STATISTIC_ID)

    hass.services.async_register(
        DOMAIN,
        SERVICE_IMPORT_CSV,
        _handle_import_csv,
        schema=IMPORT_CSV_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    # Destructif : réservé aux administrateurs, comme recorder/clear_statistics.
    async_register_admin_service(
        hass, DOMAIN, SERVICE_CLEAR_STATISTICS, _handle_clear_statistics
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Écoute les mails ES reçus par l'intégration IMAP."""
    imap_entry = entry.options.get(CONF_IMAP_ENTRY)
    sender = entry.options.get(CONF_SENDER, "")
    name = entry.options.get(CONF_NAME, DEFAULT_NAME)

    async def _handle_imap_event(event: Event) -> None:
        data = event.data
        if imap_entry and data.get("entry_id") != imap_entry:
            return  # mail d'un autre compte IMAP
        if sender and sender not in (data.get("sender") or "").lower():
            return  # mail d'un autre expéditeur

        parts = data.get("parts") or {}
        if not parts:
            _LOGGER.debug("Mail ES sans parties multipart, ignoré (sujet=%s)", data.get("subject"))
            return

        part_key = _select_csv_part(parts)
        if part_key is None:
            _LOGGER.debug("Aucune pièce jointe CSV trouvée dans le mail ES : %s", parts)
            return

        try:
            response = await hass.services.async_call(
                IMAP_DOMAIN,
                "fetch_part",
                {"entry": data["entry_id"], "uid": data["uid"], "part": part_key},
                blocking=True,
                return_response=True,
            )
        except Exception:  # noqa: BLE001 - on log et on abandonne proprement
            _LOGGER.exception("Échec du téléchargement de la pièce jointe CSV")
            return

        content = _decode_part(response)
        if content is None:
            return

        try:
            await _async_import_content(hass, content, name)
        except Exception:  # noqa: BLE001 - un mail inattendu ne doit rien casser
            _LOGGER.exception("Échec de l'import du CSV reçu par mail")

    entry.async_on_unload(hass.bus.async_listen(IMAP_EVENT, _handle_imap_event))

    _LOGGER.info(
        "%s prêt : écoute des mails (compte %s, expéditeur %s) → statistique « %s »",
        DOMAIN,
        imap_entry or "tous",
        sender or "tous",
        DEFAULT_STATISTIC_ID,
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Le listener est retiré via entry.async_on_unload."""
    return True
