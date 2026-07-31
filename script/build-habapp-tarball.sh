#!/bin/bash

###############################################################################
# Genera ota/habapp-<ver>.tar.xz: il CODICE HABApp (regole + librerie) come
# artefatto versionato aggiornabile dal controller via releases.json.
#
# Distinto dall'OTA del controller (build-update-tarball.sh, che bundla gli stessi
# sorgenti come "pavimento" per l'abilitazione offline). Questo tarball e' il
# CANALE DI AGGIORNAMENTO: il controller lo scarica, ne verifica lo sha256, lo
# spacchetta in $DATA_PATH/arfea-controller/habapp/<ver>/ e ri-provisiona.
#
# Contenuto (subset di habapp/<ver>/habapp, definito in habapp-subset.sh):
#   <ver>/
#     config.yml  logging.yml            # HABAPP_ROOT_FILES
#     lib/<system,thermostats,...>/      # HABAPP_LIB_DIRS
#     rules/<thermostats,irrigation,...>/# HABAPP_RULE_DIRS
#     rules/aasystem/tools.py            # HABAPP_RULE_FILES (base comune)
# L'entry radice e' <ver>/ cosi' si estrae direttamente in arfea-controller/habapp/.
#
# Uso:
#   ./script/build-habapp-tarball.sh                 # output in ota/
#   ./script/build-habapp-tarball.sh /percorso/out/  # output altrove
#
# Al termine stampa lo SHA256 da incollare nel blocco habapp_code di releases.json.
###############################################################################

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

# shellcheck source=script/habapp-subset.sh
source "$SCRIPT_DIR/habapp-subset.sh"

OUTPUT_DIR="${1:-$REPO_DIR/ota}"
TARBALL_NAME="habapp-${HABAPP_VER}.tar.xz"

HABAPP_SRC="$REPO_DIR/$HABAPP_SRC_REL"
if [[ ! -d "$HABAPP_SRC" ]]; then
  echo "ERRORE: sorgenti HABApp assenti ($HABAPP_SRC)" >&2
  exit 1
fi

mkdir -p "$OUTPUT_DIR"

STAGING_DIR=$(mktemp -d)
DST="$STAGING_DIR/$HABAPP_VER"
mkdir -p "$DST/lib" "$DST/rules"

echo "Preparazione tarball codice HABApp $HABAPP_VER..."

for f in "${HABAPP_ROOT_FILES[@]}"; do
  cp "$HABAPP_SRC/$f" "$DST/"
done
for d in "${HABAPP_LIB_DIRS[@]}"; do
  cp -r "$HABAPP_SRC/lib/$d" "$DST/lib/$d"
done
for d in "${HABAPP_RULE_DIRS[@]}"; do
  cp -r "$HABAPP_SRC/rules/$d" "$DST/rules/$d"
done
for f in "${HABAPP_RULE_FILES[@]}"; do
  mkdir -p "$DST/rules/$(dirname "$f")"
  cp "$HABAPP_SRC/rules/$f" "$DST/rules/$f"
done

# Pulizia runtime Python (di un'altra versione rispetto al container)
find "$STAGING_DIR" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
find "$STAGING_DIR" -name "*.pyc" -delete 2>/dev/null || true

tar -cJf "$OUTPUT_DIR/$TARBALL_NAME" -C "$STAGING_DIR" "$HABAPP_VER"
rm -rf "$STAGING_DIR"

TARBALL_PATH="$OUTPUT_DIR/$TARBALL_NAME"
TARBALL_SIZE=$(du -h "$TARBALL_PATH" | cut -f1)
SHA256=$(sha256sum "$TARBALL_PATH" | cut -d' ' -f1)

echo "Tarball creato: $TARBALL_PATH ($TARBALL_SIZE)"
echo "  regole:    [${HABAPP_RULE_DIRS[*]}] + base [${HABAPP_RULE_FILES[*]}]"
echo "  librerie:  [${HABAPP_LIB_DIRS[*]}]"
echo ""
echo "Blocco per releases.json (release bersaglio):"
echo "      \"habapp_code\": {"
echo "        \"version\": \"$HABAPP_VER\","
echo "        \"url\": \"https://cloud.domoticaundici.it/ota/$TARBALL_NAME\","
echo "        \"sha256\": \"$SHA256\""
echo "      }"
