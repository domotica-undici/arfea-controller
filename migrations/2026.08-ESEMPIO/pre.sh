#!/bin/bash
###############################################################################
# Migrazione ESEMPIO verso la release 2026.08 (pre).
#
# Eseguito dal controller PRIMA di pull/recreate. Variabili disponibili:
#   DATA_PATH, OH_CONF, FROM_VERSION, TO_VERSION
#
# Questo è un esempio commentato: non fa nulla di distruttivo. Sostituisci il
# corpo con le fix reali quando certifichi un salto di versione (vedi le release
# notes su GitHub di OpenHAB/HABApp/zwave-js-ui per i breaking change).
###############################################################################

set -euo pipefail

echo "Migrazione ${FROM_VERSION} -> ${TO_VERSION} (pre): nessuna azione (esempio)."

# --- Esempio di patch a un file di regole con sintassi cambiata ---
# file="${OH_CONF}/automation/js/qualcosa.js"
# if [[ -f "$file" ]]; then
#   sed -i 's/vecchiaApi/nuovaApi/g' "$file"
#   chown 9001:9001 "$file"      # SEMPRE: i file OpenHAB restano 9001:9001
#   echo "Patchato $file"
# fi

exit 0
