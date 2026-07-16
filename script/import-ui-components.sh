#!/bin/bash
###############################################################################
# Import widget + pagine ARFEA in OpenHAB via REST API.
#
# NON usa la Basic Auth: la REST di OpenHAB la rifiuta di default (anche con la
# password admin GIUSTA risponde 401 -> falso "password errata"). Invece conia un
# API token admin dalla console Karaf del container openhab (come fa l'installer)
# e usa l'header Bearer. Se il controller e' attivo, delega a lui l'import.
#
# Uso:
#   sudo bash import-ui-components.sh
#   OH_TOKEN=xxxxx sudo -E bash import-ui-components.sh   # usa un token gia' pronto
###############################################################################

set -e

UI_DIR="/opt/docker_store/arfea-controller/skeleton-openhab/ui"
OH_CONTAINER="${OH_CONTAINER:-openhab}"
OH_URL="http://localhost:8080"

[[ $EUID -eq 0 ]] || { echo "ERRORE: esegui come root (sudo)"; exit 1; }
[[ -d "$UI_DIR" ]] || { echo "ERRORE: directory $UI_DIR non trovata"; exit 1; }

# Lo script gira SULL'HOST, dove localhost:8080 raggiunge OpenHAB direttamente e
# `docker exec openhab` conia il token: percorso più robusto della delega al
# controller (che dovrebbe passare da nsenter). NB: l'import automatico DEL
# CONTROLLER usa nsenter e richiede security_opt apparmor:unconfined nel compose.

# ── 1. OpenHAB raggiungibile? ──
echo "Verifica OpenHAB..."
code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "$OH_URL/rest/systeminfo" 2>/dev/null || echo 000)
[[ "$code" == "200" || "$code" == "401" ]] || { echo "ERRORE: OpenHAB non raggiungibile (http=$code)."; exit 1; }
echo "OpenHAB REST OK (http=$code)"

# ── 2. Token admin: da env, oppure coniato via console Karaf ──
TOKEN="${OH_TOKEN:-}"
if [[ -z "$TOKEN" ]]; then
  echo "Conio un token admin dalla console Karaf del container '$OH_CONTAINER'..."
  command -v docker >/dev/null || { echo "ERRORE: docker non disponibile e nessun OH_TOKEN fornito"; exit 1; }
  # Il comando Karaf va passato via STDIN: come argomento su OpenHAB 5.x non viene
  # eseguito (stampa solo "Closed"). Il token ha forma oh.<nome>.<segreto>.
  out=$(docker exec "$OH_CONTAINER" sh -c \
        "echo 'openhab:users addApiToken admin arfeaImport$(date +%s) arfea' | /openhab/runtime/bin/client -p habopen" 2>/dev/null | tr -d '\r' || true)
  TOKEN=$(echo "$out" | grep -oE 'oh\.[A-Za-z0-9._-]+' | tail -1)
fi
if [[ -z "$TOKEN" || "$TOKEN" == *[Ee]rror* ]]; then
  echo "ERRORE: impossibile ottenere un token admin."
  echo "  Assicurati che l'utente 'admin' esista (apri $OH_URL nel browser e completa il setup)"
  echo "  oppure passa un token gia' pronto: OH_TOKEN=xxxx sudo -E bash $0"
  exit 1
fi
echo "Token admin ottenuto."

# ── 3. Import widget e pagine (Bearer) ──
echo ""
echo "Import componenti UI da $UI_DIR..."
for yml in "$UI_DIR"/*.yaml; do
  [[ -f "$yml" ]] || continue
  fname=$(basename "$yml" .yaml)
  case "$fname" in
    widget_*) ctype="ui:widget" ;;
    page_*)   ctype="ui:page" ;;
    *) echo "  $fname: tipo sconosciuto, salto"; continue ;;
  esac
  json=$(python3 -c "import yaml,json;print(json.dumps(yaml.safe_load(open('$yml'))))" 2>/dev/null || true)
  uid=$(python3 -c "import yaml;print(yaml.safe_load(open('$yml'))['uid'])" 2>/dev/null || true)
  [[ -n "$json" && -n "$uid" ]] || { echo "  $fname: errore parsing YAML"; continue; }
  # PUT aggiorna un componente esistente (404 se non c'è) → in tal caso POST crea.
  http_code=$(curl -s -o /dev/null -w "%{http_code}" -X PUT \
    "$OH_URL/rest/ui/components/${ctype}/${uid}" \
    -H "Authorization: Bearer ${TOKEN}" \
    -H "Content-Type: application/json" \
    -d "$json")
  if [[ "$http_code" == "404" ]]; then
    http_code=$(curl -s -o /dev/null -w "%{http_code}" -X POST \
      "$OH_URL/rest/ui/components/${ctype}" \
      -H "Authorization: Bearer ${TOKEN}" \
      -H "Content-Type: application/json" \
      -d "$json")
  fi
  case "$http_code" in
    200|201) echo "  $fname ($ctype): OK ($http_code)" ;;
    *) echo "  $fname ($ctype): FALLITO (HTTP $http_code)" ;;
  esac
done
echo ""
echo "Import completato."
