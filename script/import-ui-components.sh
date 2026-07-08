#!/bin/bash

###############################################################################
# Import widget + pagine ARFEA in OpenHAB via REST API.
#
# Richiede che l'utente admin sia gia' stato creato su OpenHAB (tramite
# la prima volta che apri http://<IP>:8080 o via Karaf).
#
# Uso:
#   sudo bash import-ui-components.sh
#   (oppure con password in variabile: ADMIN_PASS=xxx sudo -E bash import-ui-components.sh)
###############################################################################

set -e

UI_DIR="/opt/docker_store/arfea-controller/skeleton-openhab/ui"

if [[ $EUID -ne 0 ]]; then
  echo "ERRORE: esegui come root (sudo)"
  exit 1
fi

if [[ ! -d "$UI_DIR" ]]; then
  echo "ERRORE: directory $UI_DIR non trovata"
  exit 1
fi

# ── 1. Verifica OpenHAB raggiungibile ──────────────────────────────────────
echo "Verifica OpenHAB..."
code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 \
  "http://localhost:8080/rest/systeminfo" 2>/dev/null || echo 000)
if [[ "$code" != "200" && "$code" != "401" ]]; then
  echo "ERRORE: OpenHAB non raggiungibile (http=$code)."
  echo "Verifica: docker ps | grep openhab"
  exit 1
fi
echo "OpenHAB REST OK (http=$code)"

# ── 2. Chiedi password admin (o la prende da env) ──────────────────────────
if [[ -z "${ADMIN_PASS:-}" ]]; then
  echo ""
  echo "Password admin OpenHAB:"
  echo "(Se non hai admin, crealo aprendo http://<IP>:8080 nel browser"
  echo " e completando la prima pagina di setup)"
  echo ""
  read -r -s -p "Password: " ADMIN_PASS
  echo
fi

if [[ -z "$ADMIN_PASS" ]]; then
  echo "ERRORE: password vuota"
  exit 1
fi

# ── 3. Verifica credenziali ────────────────────────────────────────────────
echo ""
echo "Verifica credenziali admin..."
auth_code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 \
  -u "admin:$ADMIN_PASS" \
  "http://localhost:8080/rest/items?limit=1" 2>/dev/null || echo 000)
case "$auth_code" in
  200)
    echo "Credenziali admin OK"
    ;;
  401)
    echo "ERRORE: password admin errata"
    exit 1
    ;;
  *)
    echo "ERRORE: risposta inattesa da OpenHAB (http=$auth_code)"
    exit 1
    ;;
esac

# ── 4. Import widget e pagine ──────────────────────────────────────────────
echo ""
echo "Import componenti UI da $UI_DIR..."
for yml in "$UI_DIR"/*.yaml; do
  [[ -f "$yml" ]] || continue
  fname=$(basename "$yml" .yaml)

  if [[ "$fname" == widget_* ]]; then
    component_type="ui:widget"
  elif [[ "$fname" == page_* ]]; then
    component_type="ui:page"
  else
    echo "  $fname: tipo sconosciuto, salto"
    continue
  fi

  json=$(python3 -c "import yaml,json; print(json.dumps(yaml.safe_load(open('$yml'))))" 2>/dev/null)
  uid=$(python3 -c "import yaml; print(yaml.safe_load(open('$yml'))['uid'])" 2>/dev/null)

  if [[ -z "$json" || -z "$uid" ]]; then
    echo "  $fname: errore parsing YAML"
    continue
  fi

  http_code=$(curl -s -o /dev/null -w "%{http_code}" -X PUT \
    -u "admin:$ADMIN_PASS" \
    "http://localhost:8080/rest/ui/components/${component_type}/${uid}" \
    -H "Content-Type: application/json" \
    -d "$json")

  case "$http_code" in
    200|201) echo "  $fname ($component_type): OK ($http_code)" ;;
    *) echo "  $fname ($component_type): FALLITO (HTTP $http_code)" ;;
  esac
done

echo ""
echo "Import completato."
