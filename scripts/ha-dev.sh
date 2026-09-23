#!/usr/bin/env bash
# Instance Home Assistant jetable pour tester l'intégration es_strasbourg.
#
# Usage : scripts/ha-dev.sh {start|stop|restart|logs|status|shell|reset}
#
#   start    crée la config si besoin et démarre HA sur http://localhost:8123
#   stop     arrête et supprime le conteneur (la config est conservée)
#   restart  redémarre HA (à faire après une modification du code Python)
#   logs     suit les logs (filtrés sur es_strasbourg avec : logs es)
#   status   état du conteneur
#   shell    shell dans le conteneur
#   reset    arrête HA et efface la config de test (base de stats comprise)
#
# Variables d'environnement :
#   HA_TAG     version de l'image (défaut : stable, ex. HA_TAG=2026.9.3)
#   HA_PORT    port local (défaut : 8123)
#   HA_CONFIG  dossier de config (défaut : <projet>/.ha-dev)
#   ENGINE     moteur de conteneurs (défaut : podman, sinon docker)
#
# L'intégration est montée en lecture seule depuis custom_components/ : pas de
# copie, un « restart » suffit pour prendre en compte une modification.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HA_TAG="${HA_TAG:-stable}"
HA_PORT="${HA_PORT:-8123}"
HA_CONFIG="${HA_CONFIG:-$ROOT/.ha-dev}"
IMAGE="ghcr.io/home-assistant/home-assistant:$HA_TAG"
NAME="es-ha-dev"

if [[ -z "${ENGINE:-}" ]]; then
    if command -v podman >/dev/null; then ENGINE=podman
    elif command -v docker >/dev/null; then ENGINE=docker
    else echo "Ni podman ni docker trouvé." >&2; exit 1
    fi
fi

log() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }

running() { [[ "$($ENGINE inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null)" == "true" ]]; }

init_config() {
    mkdir -p "$HA_CONFIG/custom_components" "$HA_CONFIG/es_strasbourg"

    if [[ ! -f "$HA_CONFIG/configuration.yaml" ]]; then
        log "Création de $HA_CONFIG/configuration.yaml"
        cat >"$HA_CONFIG/configuration.yaml" <<'EOF'
default_config:

homeassistant:
  # Le découpage en jours dépend du fuseau : on fixe celui des données ES.
  time_zone: Europe/Paris
  country: FR
  unit_system: metric
  # Le dossier de config n'est pas lisible par les actions par défaut.
  allowlist_external_dirs:
    - /config/es_strasbourg

logger:
  default: info
  logs:
    custom_components.es_strasbourg: debug
    homeassistant.components.imap: debug
EOF
    fi

    # CSV d'exemple, accessibles depuis l'action es_strasbourg.import_csv
    # (dossier autorisé par allowlist_external_dirs ci-dessus).
    for csv in "$ROOT"/*.csv; do
        [[ -e "$csv" ]] && cp -u "$csv" "$HA_CONFIG/es_strasbourg/"
    done
}

wait_ready() {
    log "Attente du démarrage de Home Assistant (jusqu'à 3 min)…"
    for _ in $(seq 1 90); do
        if curl -sf -o /dev/null "http://localhost:$HA_PORT/manifest.json"; then
            log "Prêt : http://localhost:$HA_PORT"
            return 0
        fi
        if ! running; then
            echo "Le conteneur s'est arrêté. Derniers logs :" >&2
            $ENGINE logs --tail 40 "$NAME" >&2 || true
            return 1
        fi
        sleep 2
    done
    echo "HA ne répond pas encore ; voir : $0 logs" >&2
    return 1
}

cmd_start() {
    if running; then
        log "Déjà démarré : http://localhost:$HA_PORT"
        return 0
    fi
    init_config
    $ENGINE rm -f "$NAME" >/dev/null 2>&1 || true

    log "Démarrage de $IMAGE avec $ENGINE"
    # :Z = relabel SELinux (sans effet si SELinux est absent).
    # En podman rootless, le root du conteneur correspond à l'utilisateur
    # courant : les fichiers de .ha-dev restent à vous.
    $ENGINE run -d \
        --name "$NAME" \
        -p "$HA_PORT:8123" \
        -e TZ=Europe/Paris \
        -v "$HA_CONFIG:/config:Z" \
        -v "$ROOT/custom_components/es_strasbourg:/config/custom_components/es_strasbourg:ro,Z" \
        "$IMAGE" >/dev/null

    wait_ready
    cat <<EOF

Étapes suivantes :
  1. Créez le compte sur http://localhost:$HA_PORT
  2. Paramètres → Appareils et services → Ajouter → Électricité de Strasbourg
  3. Historique : Outils de développement → Actions →
       action: es_strasbourg.import_csv
       data:
         path: /config/es_strasbourg/Consommation_Journaliere.csv
     (à faire AVANT l'IMAP : la série démarre au premier import)
  4. IMAP : Ajouter → IMAP, puis choisir ce compte dans les options d'ES
  5. Énergie : Paramètres → Tableaux de bord → Énergie → Consommation électrique
EOF
}

cmd_stop() {
    log "Arrêt de $NAME"
    $ENGINE rm -f "$NAME" >/dev/null 2>&1 || true
}

cmd_restart() {
    if running; then
        log "Redémarrage de $NAME"
        $ENGINE restart "$NAME" >/dev/null
        wait_ready
    else
        cmd_start
    fi
}

cmd_logs() {
    if [[ "${1:-}" == "es" ]]; then
        $ENGINE logs -f "$NAME" 2>&1 | grep --line-buffered -iE "es_strasbourg|imap"
    else
        $ENGINE logs -f "$NAME"
    fi
}

cmd_reset() {
    read -r -p "Effacer $HA_CONFIG (compte, base de statistiques, IMAP) ? [o/N] " answer
    [[ "$answer" =~ ^[oOyY]$ ]] || { echo "Annulé."; return 0; }
    cmd_stop
    if [[ "$ENGINE" == "podman" ]]; then
        # Fichiers créés par le conteneur : on les supprime dans l'espace de
        # noms utilisateur de podman pour éviter les soucis de droits.
        podman unshare rm -rf "$HA_CONFIG"
    else
        rm -rf "$HA_CONFIG"
    fi
    log "Config de test effacée."
}

case "${1:-}" in
    start) cmd_start ;;
    stop) cmd_stop ;;
    restart) cmd_restart ;;
    logs) cmd_logs "${2:-}" ;;
    status) $ENGINE ps -a --filter "name=^${NAME}\$" ;;
    shell) $ENGINE exec -it "$NAME" bash ;;
    reset) cmd_reset ;;
    *)
        sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'
        exit 1
        ;;
esac
