#!/bin/bash

###############################################################################
# Genera arfea-controller.tar.xz per il self-update delle centraline
#
# Contenuto del tarball:
#   arfea-controller/
#     app/                    # Codice Python FastAPI
#     Dockerfile
#     docker-compose.yml
#     requirements.txt
#     MANUALE.md
#     config/                 # arfea.yml template (solo prima installazione)
#     skeleton-openhab/       # File OpenHAB
#       conf/                   # items, regole JS, sitemap classica
#       cont-init.d/
#       ui/                     # widget + page YAML da importare via REST API
#     templates/              # Config predefinite per servizi opzionali
#       zigbee2mqtt/
#       zwave-js-ui/
#     migrations/             # Script di migrazione versione (release certificate)
#     script/                 # Script utili (fallback manuali)
#       import-ui-components.sh
#
# Uso:
#   ./script/build-update-tarball.sh
#   ./script/build-update-tarball.sh /percorso/output/
###############################################################################

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
OUTPUT_DIR="${1:-$REPO_DIR}"
TARBALL_NAME="arfea-controller.tar.xz"

STAGING_DIR=$(mktemp -d)
STAGING="$STAGING_DIR/arfea-controller"

echo "Preparazione tarball aggiornamento..."

mkdir -p "$STAGING"

# ── Controller files ──
cp "$REPO_DIR/arfea-controller/Dockerfile" "$STAGING/"
cp "$REPO_DIR/arfea-controller/docker-compose.yml" "$STAGING/"
cp "$REPO_DIR/arfea-controller/requirements.txt" "$STAGING/"
cp "$REPO_DIR/MANUALE.md" "$STAGING/"
cp -r "$REPO_DIR/arfea-controller/app" "$STAGING/app"
# config/ serve solo per la prima installazione (preparaArfea-armbian-1.sh).
# Il self-update del controller salta volutamente config/ per non sovrascrivere
# arfea.yml con le impostazioni dell'utente — vedi _extract_and_install in main.py.
cp -r "$REPO_DIR/arfea-controller/config" "$STAGING/config"

# ── Skeleton OpenHAB ──
mkdir -p "$STAGING/skeleton-openhab"
cp -r "$REPO_DIR/skeleton-openhab/conf" "$STAGING/skeleton-openhab/conf"
cp -r "$REPO_DIR/skeleton-openhab/cont-init.d" "$STAGING/skeleton-openhab/cont-init.d"
cp -r "$REPO_DIR/skeleton-openhab/ui" "$STAGING/skeleton-openhab/ui"

# ── Template config (zigbee2mqtt, zwave-js-ui) ──
if [[ -d "$REPO_DIR/templates" ]]; then
  cp -r "$REPO_DIR/templates" "$STAGING/templates"
fi

# ── Migrazioni di versione (release certificate) ──
# Finiscono in $DATA_PATH/arfea-controller/migrations/ e sono eseguite dal
# release_manager durante l'apply di un upgrade (pre.sh/post.sh per versione).
if [[ -d "$REPO_DIR/migrations" ]]; then
  cp -r "$REPO_DIR/migrations" "$STAGING/migrations"
fi

# ── Script utili (fallback manuali, migrazione, ecc.) ──
mkdir -p "$STAGING/script"
cp "$REPO_DIR/script/import-ui-components.sh" "$STAGING/script/"
cp "$REPO_DIR/script/migrate-to-controller.sh" "$STAGING/script/"

# ── Pulizia file non necessari ──
find "$STAGING" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
find "$STAGING" -name "*.pyc" -delete 2>/dev/null || true

# ── Creazione tarball ──
tar -cJf "$OUTPUT_DIR/$TARBALL_NAME" -C "$STAGING_DIR" arfea-controller

rm -rf "$STAGING_DIR"

TARBALL_SIZE=$(du -h "$OUTPUT_DIR/$TARBALL_NAME" | cut -f1)
echo "Tarball creato: $OUTPUT_DIR/$TARBALL_NAME ($TARBALL_SIZE)"
