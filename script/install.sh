#!/bin/bash
###############################################################################
# arfea-controller — installer autonomo
#
# Installa e avvia arfea-controller su una centralina GIÀ PREPARATA. È il
# percorso di installazione "da utente esterno": non tocca OS, utenti, VPN o
# infrastrutture private. Lo stesso installer è richiamabile in modo
# non-interattivo (variabili d'ambiente) da uno script di provisioning host.
#
# PREREQUISITI (host già pronto):
#   - Docker + plugin 'docker compose' installati e attivi
#   - i dati persistenti staranno in /opt/docker_store (owner 9001:9001 per OpenHAB)
#   - eventuali porte seriali (zwave/zigbee/modbus/thread) già identificate
#   - curl, tar/xz disponibili; esecuzione come root (sudo)
#
# USO INTERATTIVO:
#   sudo ./script/install.sh
#
# USO NON-INTERATTIVO (env), es. da un provisioning host:
#   sudo ARFEA_NONINTERACTIVE=1 \
#        ARFEA_SERVICES="habapp,zwave-js-ui" \
#        ARFEA_API_KEY="…"            # se assente, generata \
#        ARFEA_UPDATE_URL="…"  ARFEA_RELEASES_URL="…"   # opzionali (OTA) \
#        ARFEA_WEBDAV_URL="…"  ARFEA_WEBDAV_USER="…"  ARFEA_WEBDAV_PASS="…" \
#        ARFEA_ZWAVE_DEVICE="/dev/ttyACM0" \
#        ARFEA_ZIGBEE_DEVICE="/dev/serial/by-id/…" \
#        ARFEA_MODBUS_DEVICE="/dev/ttyUSB0" \
#        ARFEA_OPENHAB_DEVICES="/dev/ttyAML1:/dev/rs485,/dev/ttyUSB1:/dev/ttyUSB1"  # porte OpenHAB extra (csv) \
#        ARFEA_OTBR_DEVICE="/dev/ttyACM0" ARFEA_OTBR_INFRA_IF="end0" \
#        ./script/install.sh
#
# NB: nessun segreto è incluso nel repo. La password admin di OpenHAB è generata
#     con openssl a runtime e salvata solo in locale in
#     /opt/docker_store/arfea-controller/.credentials (chmod 600).
###############################################################################

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

# Default privati (OTA + WebDAV di domoticaundici). Presente solo nella repo
# privata: export-public.sh lo esclude, quindi nella repo pubblica non c'è e
# l'installer ricade su prompt/vuoto. Definisce le variabili ARFEA_*_DEFAULT.
if [[ -f "$SCRIPT_DIR/arfea-defaults.env" ]]; then
  # shellcheck disable=SC1091
  source "$SCRIPT_DIR/arfea-defaults.env"
fi

DATA_PATH="${ARFEA_DATA_PATH:-/opt/docker_store}"
CTRL_DIR="$DATA_PATH/arfea-controller"
OH_UID=9001
OH_GID=9001
OH_ADMIN_USER="admin"

log() { echo "$(date +%F_%T) [install] $*"; }
die() { echo "ERRORE: $*" >&2; exit 1; }

# Variabili di configurazione (default; sovrascrivibili da env o prompt).
# Precedenza: env ARFEA_* > default privati (arfea-defaults.env) > vuoto.
ARFEA_API_KEY="${ARFEA_API_KEY:-}"
ARFEA_SERVICES="${ARFEA_SERVICES:-}"
ARFEA_UPDATE_URL="${ARFEA_UPDATE_URL:-${ARFEA_UPDATE_URL_DEFAULT:-}}"
ARFEA_RELEASES_URL="${ARFEA_RELEASES_URL:-${ARFEA_RELEASES_URL_DEFAULT:-}}"
ARFEA_WEBDAV_URL="${ARFEA_WEBDAV_URL:-${ARFEA_WEBDAV_URL_DEFAULT:-}}"
ARFEA_WEBDAV_USER="${ARFEA_WEBDAV_USER:-${ARFEA_WEBDAV_USER_DEFAULT:-}}"
ARFEA_WEBDAV_PASS="${ARFEA_WEBDAV_PASS:-${ARFEA_WEBDAV_PASS_DEFAULT:-}}"
ARFEA_ZWAVE_DEVICE="${ARFEA_ZWAVE_DEVICE:-}"
ARFEA_ZIGBEE_DEVICE="${ARFEA_ZIGBEE_DEVICE:-}"
ARFEA_MODBUS_DEVICE="${ARFEA_MODBUS_DEVICE:-}"
# Porte seriali AGGIUNTIVE per OpenHAB (csv "src:tgt[:mode]"), oltre a quella
# Modbus. Es: "/dev/ttyAML1:/dev/rs485,/dev/ttyUSB1:/dev/ttyUSB1".
ARFEA_OPENHAB_DEVICES="${ARFEA_OPENHAB_DEVICES:-}"
ARFEA_OTBR_DEVICE="${ARFEA_OTBR_DEVICE:-}"
ARFEA_OTBR_INFRA_IF="${ARFEA_OTBR_INFRA_IF:-}"

INSTALL_HABAPP=false; INSTALL_ZWAVE=false; INSTALL_ZIGBEE=false
INSTALL_NODERED=false; INSTALL_OTBR=false

# ── Prerequisiti ────────────────────────────────────────────────────────────
check_prereqs() {
  [[ "$(id -u)" -eq 0 ]] || die "esegui come root (sudo)."
  command -v docker >/dev/null || die "Docker non installato."
  docker info >/dev/null 2>&1 || die "il daemon Docker non è attivo."
  docker compose version >/dev/null 2>&1 || die "manca il plugin 'docker compose'."
  command -v curl >/dev/null || die "curl non installato."
  command -v tar >/dev/null || die "tar non installato."
  [[ -f "$SCRIPT_DIR/build-update-tarball.sh" ]] || die "build-update-tarball.sh non trovato accanto a install.sh."
}

# ── Config: interattiva o da env ────────────────────────────────────────────
is_interactive() { [[ -z "${ARFEA_NONINTERACTIVE:-}" && -t 0 ]]; }

parse_services_csv() {
  local IFS=','; read -ra items <<< "$1"
  for s in "${items[@]}"; do
    case "$(echo "$s" | tr -d ' ')" in
      habapp) INSTALL_HABAPP=true ;;
      zwave-js-ui|zwave) INSTALL_ZWAVE=true ;;
      zigbee2mqtt|zigbee) INSTALL_ZIGBEE=true ;;
      node-red|nodered) INSTALL_NODERED=true ;;
      otbr) INSTALL_OTBR=true ;;
      "" ) ;;
      *) log "servizio '$s' non riconosciuto, ignorato" ;;
    esac
  done
}

configure() {
  if is_interactive; then
    echo "=== arfea-controller — configurazione ==="
    if [[ -z "$ARFEA_API_KEY" ]]; then
      ARFEA_API_KEY=$(openssl rand -hex 16)
      echo "API Key generata: $ARFEA_API_KEY"
      read -r -p "Confermi? (s = sì, n = inserisci la tua): " c
      [[ "$c" =~ ^[SsYy] ]] || read -r -p "API Key: " ARFEA_API_KEY
    fi
    [[ -n "$ARFEA_UPDATE_URL"   ]] || read -r -p "URL OTA controller (vuoto = OTA disattivato): " ARFEA_UPDATE_URL
    [[ -n "$ARFEA_RELEASES_URL" ]] || read -r -p "URL manifest releases.json (vuoto = disattivato): " ARFEA_RELEASES_URL
    if [[ -z "$ARFEA_WEBDAV_URL" ]]; then
      read -r -p "WebDAV URL per backup (vuoto = salta): " ARFEA_WEBDAV_URL
      if [[ -n "$ARFEA_WEBDAV_URL" ]]; then
        read -r -p "  WebDAV utente: " ARFEA_WEBDAV_USER
        read -r -s -p "  WebDAV password: " ARFEA_WEBDAV_PASS; echo
      fi
    fi
    [[ -n "$ARFEA_SERVICES" ]] || read -r -p "Servizi opzionali (csv: habapp,zwave-js-ui,zigbee2mqtt,node-red,otbr): " ARFEA_SERVICES
    parse_services_csv "$ARFEA_SERVICES"
    $INSTALL_ZWAVE  && [[ -z "$ARFEA_ZWAVE_DEVICE"  ]] && read -r -p "Device Z-Wave (es. /dev/ttyACM0): " ARFEA_ZWAVE_DEVICE
    $INSTALL_ZIGBEE && [[ -z "$ARFEA_ZIGBEE_DEVICE" ]] && read -r -p "Device Zigbee (es. /dev/serial/by-id/...): " ARFEA_ZIGBEE_DEVICE
    $INSTALL_OTBR   && [[ -z "$ARFEA_OTBR_DEVICE"   ]] && read -r -p "Device Thread OTBR (es. /dev/ttyACM0): " ARFEA_OTBR_DEVICE
    [[ -n "$ARFEA_OPENHAB_DEVICES" ]] || read -r -p "Porte seriali OpenHAB (csv src:tgt, vuoto = aggiungile poi dall'interfaccia): " ARFEA_OPENHAB_DEVICES
  else
    [[ -n "$ARFEA_API_KEY" ]] || ARFEA_API_KEY=$(openssl rand -hex 16)
    parse_services_csv "$ARFEA_SERVICES"
  fi

  if $INSTALL_OTBR && [[ -z "$ARFEA_OTBR_INFRA_IF" ]]; then
    ARFEA_OTBR_INFRA_IF=$(ip -o -4 route show default 2>/dev/null | awk '{print $5}' | head -1)
  fi
  log "config: servizi=[habapp=$INSTALL_HABAPP zwave=$INSTALL_ZWAVE zigbee=$INSTALL_ZIGBEE nodered=$INSTALL_NODERED otbr=$INSTALL_OTBR]"
  log "config: OTA=$([[ -n "$ARFEA_UPDATE_URL" ]] && echo "$ARFEA_UPDATE_URL" || echo "disattivato")"
  # Le credenziali WebDAV non vanno mai stampate: solo se sono impostate o no.
  log "config: backup WebDAV=$([[ -n "$ARFEA_WEBDAV_URL" ]] && echo "configurato" || echo "non configurato")"
}

# ── Deploy: build del tarball (stesso layout dell'OTA) ed estrazione ─────────
deploy_controller() {
  log "Creazione struttura in $DATA_PATH ..."
  mkdir -p "$CTRL_DIR"/{config,backups}
  if [[ ! -d "$DATA_PATH/openhab/userdata" ]]; then
    mkdir -p "$DATA_PATH/openhab"
    chown "$OH_UID:$OH_GID" "$DATA_PATH/openhab"
  fi

  log "Pacchettizzazione ed estrazione arfea-controller ..."
  local tmp; tmp=$(mktemp -d)
  "$SCRIPT_DIR/build-update-tarball.sh" "$tmp" >/dev/null
  tar -xJf "$tmp/arfea-controller.tar.xz" --strip-components=1 -C "$CTRL_DIR/"
  rm -rf "$tmp"

  # Se l'installer gira DALLA repo clonata dentro il target (CTRL_DIR) — es.
  # `git clone …/arfea-controller.git` dentro /opt/docker_store — l'estrazione
  # lascia lì la sottocartella sorgente "arfea-controller/" della repo (app/,
  # Dockerfile, …): ridondante e confondente. La rimuoviamo (guardata dalla
  # presenza di app/main.py, così non tocchiamo altro). Meglio comunque clonare
  # la repo FUORI dal target.
  if [[ "$REPO_DIR" -ef "$CTRL_DIR" && -f "$CTRL_DIR/arfea-controller/app/main.py" ]]; then
    log "Repo clonata dentro $CTRL_DIR: rimuovo la sottocartella sorgente ridondante arfea-controller/"
    rm -rf "$CTRL_DIR/arfea-controller"
  fi

  # Servizi opzionali: cartelle + template
  if $INSTALL_ZWAVE || $INSTALL_ZIGBEE; then
    if [[ ! -f "$DATA_PATH/mosquitto/config/mosquitto.conf" ]]; then
      mkdir -p "$DATA_PATH/mosquitto"/{config,data,log}
      printf 'allow_anonymous true\nlistener 1883 0.0.0.0\n' > "$DATA_PATH/mosquitto/config/mosquitto.conf"
    fi
  fi
  if $INSTALL_ZWAVE; then
    mkdir -p "$DATA_PATH/zwave-js-ui"
    [[ -f "$CTRL_DIR/templates/zwave-js-ui/settings.json" && ! -f "$DATA_PATH/zwave-js-ui/settings.json" ]] \
      && cp "$CTRL_DIR/templates/zwave-js-ui/settings.json" "$DATA_PATH/zwave-js-ui/settings.json"
  fi
  if $INSTALL_ZIGBEE; then
    mkdir -p "$DATA_PATH/zigbee2mqtt/data"
    [[ -f "$CTRL_DIR/templates/zigbee2mqtt/configuration.yaml" && ! -f "$DATA_PATH/zigbee2mqtt/data/configuration.yaml" ]] \
      && cp "$CTRL_DIR/templates/zigbee2mqtt/configuration.yaml" "$DATA_PATH/zigbee2mqtt/data/configuration.yaml"
  fi
  $INSTALL_HABAPP  && { mkdir -p "$DATA_PATH/openhab/conf/habapp"; chown -R "$OH_UID:$OH_GID" "$DATA_PATH/openhab/conf/habapp"; }
  $INSTALL_NODERED && { mkdir -p "$DATA_PATH/node-red"; chown -R 1000:1000 "$DATA_PATH/node-red"; }
  # NB: 'if' e non '$VAR && cmd': con set -e una guardia falsa come ULTIMA
  # istruzione della funzione la farebbe ritornare 1, abortendo l'installer
  # prima di configure_yml (api_key/webdav/update_url resterebbero i placeholder).
  if $INSTALL_OTBR; then mkdir -p "$DATA_PATH/otbr/data"; fi
}

# ── Config di arfea.yml (scrittura dei soli campi noti) ──────────────────────
yml_scalar() { # key value file  → sostituisce la riga "  key: ..." (indent 2)
  local key="$1" val="$2" f="$3"
  # Il valore va tra doppi apici YAML: prima escape per il contesto YAML
  # double-quoted (\ e "), poi escape dei caratteri speciali della replacement
  # di sed (\ & e il delimitatore |). Senza, una password con '"'/'\' darebbe
  # YAML non valido e un URL con '&' (querystring) romperebbe il sed.
  local y="${val//\\/\\\\}"; y="${y//\"/\\\"}"
  local esc; esc=$(printf '%s' "$y" | sed -e 's/[\\&|]/\\&/g')
  sed -i -E "s|^(  ${key}:).*|\1 \"${esc}\"|" "$f"
}
enable_service() { # service file
  awk -v svc="  ${1}:" '$0==svc{i=1} i&&/enabled:/{sub(/false/,"true");i=0} {print}' "$2" > "$2.t" && mv "$2.t" "$2"
}
add_devices_to_service() { # svc csv("src:tgt[:mode]",...) file
  local svc="$1" csv="$2" f="$3"
  [[ -n "$csv" ]] || return 0
  # Inserisce/accoda i mapping device nel blocco del servizio indicato. Il blocco
  # è delimitato dalle chiavi-servizio a 2 spazi. Se il servizio ha già "    devices:"
  # accoda le voci; altrimenti crea il blocco appena prima della fine (prossima
  # chiave-servizio o EOF). Serve perché openhab nel template NON ha più device.
  awk -v svc="$svc" -v csv="$csv" '
    function emit(   n,a,i,e){ n=split(csv,a,","); for(i=1;i<=n;i++){e=a[i]; gsub(/ /,"",e); if(e!="") print "      - \"" e "\"" } }
    $0 ~ "^  " svc ":[[:space:]]*$" { inblk=1; print; next }
    inblk && /^  [A-Za-z0-9_-]+:[[:space:]]*$/ { if(!done){ print "    devices:"; emit(); done=1 } inblk=0 }
    inblk && /^    devices:[[:space:]]*$/ { print; if(!done){ emit(); done=1 } next }
    { print }
    END { if(inblk && !done){ print "    devices:"; emit() } }
  ' "$f" > "$f.t" && mv "$f.t" "$f"
}

configure_openhab_devices() { # file  — costruisce la lista device openhab da env
  local f="$1"; local list=()
  # Modbus RTU: mappato al target convenzionale /dev/ttyUSB0 (env-only)
  [[ -n "$ARFEA_MODBUS_DEVICE" ]] && list+=("${ARFEA_MODBUS_DEVICE}:/dev/ttyUSB0")
  # Porte aggiuntive "src:tgt[:mode]" separate da virgola
  if [[ -n "$ARFEA_OPENHAB_DEVICES" ]]; then
    local IFS=','; local extra; read -ra extra <<< "$ARFEA_OPENHAB_DEVICES"
    local e
    for e in "${extra[@]}"; do e="${e// /}"; [[ -n "$e" ]] && list+=("$e"); done
  fi
  [[ ${#list[@]} -gt 0 ]] || return 0
  local csv; local IFS=','; csv="${list[*]}"
  add_devices_to_service openhab "$csv" "$f"
}

configure_yml() {
  local YML="$CTRL_DIR/config/arfea.yml"
  [[ -f "$YML" ]] || die "arfea.yml non trovato dopo l'estrazione."

  yml_scalar api_key "$ARFEA_API_KEY" "$YML"
  # OTA: scrivi solo se valorizzati, altrimenti lascia il valore del template
  # (nella repo pubblica è un placeholder; nella privata l'URL reale).
  [[ -n "$ARFEA_UPDATE_URL"   ]] && yml_scalar update_url   "$ARFEA_UPDATE_URL"   "$YML"
  [[ -n "$ARFEA_RELEASES_URL" ]] && yml_scalar releases_url "$ARFEA_RELEASES_URL" "$YML"
  if [[ -n "$ARFEA_WEBDAV_URL" ]]; then
    yml_scalar webdav_url      "$ARFEA_WEBDAV_URL"  "$YML"
    yml_scalar webdav_user     "$ARFEA_WEBDAV_USER" "$YML"
    yml_scalar webdav_password "$ARFEA_WEBDAV_PASS" "$YML"
  fi

  $INSTALL_HABAPP  && enable_service habapp      "$YML"
  $INSTALL_ZWAVE   && enable_service zwave-js-ui "$YML"
  $INSTALL_ZIGBEE  && enable_service zigbee2mqtt "$YML"
  $INSTALL_NODERED && enable_service node-red    "$YML"
  $INSTALL_OTBR    && enable_service otbr        "$YML"

  [[ -n "$ARFEA_ZWAVE_DEVICE"  ]] && sed -i "s|/dev/ttyACM0:/dev/zwave|${ARFEA_ZWAVE_DEVICE}:/dev/zwave|" "$YML"
  [[ -n "$ARFEA_ZIGBEE_DEVICE" ]] && sed -i "s|/dev/serial/by-id/usb-ITEAD_SONOFF_Zigbee_3.0_USB_Dongle_Plus_V2_20231031184237-if00:/dev/zigbee|${ARFEA_ZIGBEE_DEVICE}:/dev/zigbee|" "$YML"
  # openhab: nessun device nel template — costruiamo il blocco da Modbus + extra
  # (in mancanza, si aggiungono poi dalla Web UI: Servizi → Dispositivi).
  configure_openhab_devices "$YML"
  if $INSTALL_OTBR; then
    [[ -n "$ARFEA_OTBR_DEVICE" && "$ARFEA_OTBR_DEVICE" != "/dev/ttyACM0" ]] \
      && sed -i "s|\"/dev/ttyACM0:/dev/ttyACM0\"|\"${ARFEA_OTBR_DEVICE}:/dev/ttyACM0\"|" "$YML"
    [[ -n "$ARFEA_OTBR_INFRA_IF" ]] && sed -i "s|OT_INFRA_IF: \"end0\"|OT_INFRA_IF: \"${ARFEA_OTBR_INFRA_IF}\"|" "$YML"
    log "NB: OTBR (Thread/Matter) richiede una preparazione host aggiuntiva (IPv6, avahi/bluez, snap chip-tool) non gestita da questo installer."
  fi
  log "arfea.yml configurato"
}

# ── Deploy skeleton OpenHAB (owner 9001:9001) ───────────────────────────────
deploy_openhab_skeleton() {
  local SK="$CTRL_DIR/skeleton-openhab"
  [[ -d "$SK" ]] || { log "skeleton-openhab assente, salto"; return 0; }
  if [[ -d "$SK/conf" ]]; then
    mkdir -p "$DATA_PATH/openhab/conf"
    cp -r "$SK/conf/." "$DATA_PATH/openhab/conf/"
    chown -R "$OH_UID:$OH_GID" "$DATA_PATH/openhab/conf"
  fi
  if [[ -d "$SK/cont-init.d" ]]; then
    mkdir -p "$DATA_PATH/openhab/cont-init.d"
    cp -r "$SK/cont-init.d/." "$DATA_PATH/openhab/cont-init.d/"
    chmod +x "$DATA_PATH/openhab/cont-init.d/"* 2>/dev/null || true
    chown -R "$OH_UID:$OH_GID" "$DATA_PATH/openhab/cont-init.d"
  fi
  log "skeleton OpenHAB deployato"
}

# ── Avvio stack + attesa OpenHAB ─────────────────────────────────────────────
start_stack() {
  log "Build e avvio dello stack..."
  ( cd "$CTRL_DIR" && docker compose build && docker compose up -d )
  log "Attendo che OpenHAB sia in esecuzione..."
  while [[ "$(docker inspect -f '{{.State.Running}}' openhab 2>/dev/null)" != "true" ]]; do sleep 2; done
}

wait_openhab_rest() {
  log "Attesa REST API OpenHAB (fino a ~8 min)..."
  local t=0 code
  while true; do
    code=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 3 --max-time 5 \
      "http://localhost:8080/rest/systeminfo" 2>/dev/null || echo 000)
    [[ "$code" == "200" || "$code" == "401" ]] && break
    t=$((t+1)); [[ $t -ge 240 ]] && { log "ATTENZIONE: REST non pronta (code=$code)"; return 1; }
    sleep 2
  done
  local k=0
  while ! timeout 10 docker exec openhab /openhab/runtime/bin/client -p habopen "shell:info" &>/dev/null; do
    k=$((k+1)); [[ $k -ge 36 ]] && { log "ATTENZIONE: console Karaf non pronta"; return 1; }
    sleep 5
  done
  log "OpenHAB REST + Karaf pronti"
}

# ── Admin OpenHAB (password generata) + token + import UI ────────────────────
OH_ADMIN_PASS=""; OH_ADMIN_TOKEN=""
setup_openhab_admin() {
  OH_ADMIN_PASS=$(openssl rand -hex 12)
  docker exec openhab /openhab/runtime/bin/client -p habopen \
    "openhab:users add ${OH_ADMIN_USER} ${OH_ADMIN_PASS} administrator" &>/dev/null || true
  local out
  out=$(docker exec openhab /openhab/runtime/bin/client -p habopen \
    "openhab:users addApiToken ${OH_ADMIN_USER} arfeaInstall arfea" 2>/dev/null | tr -d '\r')
  OH_ADMIN_TOKEN=$(echo "$out" | awk 'NF {t=$NF} END {print t}')
  [[ -n "$OH_ADMIN_TOKEN" && "$OH_ADMIN_TOKEN" != *error* ]] || { log "ATTENZIONE: token admin non generato, salto import UI"; return 1; }
  log "utente admin OpenHAB creato e token generato"
}

import_ui() {
  local UI_DIR="$CTRL_DIR/skeleton-openhab/ui"
  [[ -d "$UI_DIR" && -n "$OH_ADMIN_TOKEN" ]] || return 0
  local yml uid ctype json code
  for yml in "$UI_DIR"/*.yaml; do
    [[ -f "$yml" ]] || continue
    case "$(basename "$yml")" in
      widget_*) ctype="ui:widget" ;;
      page_*)   ctype="ui:page" ;;
      *) continue ;;
    esac
    uid=$(python3 -c "import yaml;print(yaml.safe_load(open('$yml'))['uid'])" 2>/dev/null) || continue
    json=$(python3 -c "import yaml,json;print(json.dumps(yaml.safe_load(open('$yml'))))" 2>/dev/null) || continue
    code=$(curl -s -o /dev/null -w "%{http_code}" -X PUT \
      "http://localhost:8080/rest/ui/components/${ctype}/${uid}" \
      -H "Authorization: Bearer ${OH_ADMIN_TOKEN}" \
      -H "Content-Type: application/json" -d "$json")
    log "UI $(basename "$yml"): HTTP $code"
  done
}

save_credentials() {
  local f="$CTRL_DIR/.credentials"
  umask 077
  cat > "$f" <<EOF
# arfea-controller — credenziali (generato $(date +%F_%T)). NON committare.
ARFEA_API_KEY=$ARFEA_API_KEY
OPENHAB_ADMIN_USER=$OH_ADMIN_USER
OPENHAB_ADMIN_PASS=$OH_ADMIN_PASS
EOF
  chmod 600 "$f"
  log "credenziali salvate in $f (chmod 600)"
}

final_info() {
  echo ""
  echo "======================================================"
  echo " Installazione completata."
  echo "  Controller:  http://<ip-lan>:8888   (docs: /docs)"
  echo "  OpenHAB:     http://<ip-lan>:8080"
  echo "  Admin OpenHAB: utente '$OH_ADMIN_USER' (password in $CTRL_DIR/.credentials)"
  echo "  API Key controller: in $CTRL_DIR/.credentials"
  echo "======================================================"
}

main() {
  check_prereqs
  configure
  deploy_controller
  configure_yml
  deploy_openhab_skeleton
  start_stack
  if wait_openhab_rest; then
    setup_openhab_admin && import_ui || true
  fi
  save_credentials
  final_info
}

main "$@"
