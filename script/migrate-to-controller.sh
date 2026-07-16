#!/bin/bash

###############################################################################
# migrate-to-controller.sh
#
# Migra una centralina esistente verso la struttura arfea-controller.
# Riconosce due tipi di installazione di partenza e agisce di conseguenza:
#
#   A) DOCKER  — vecchio stack docker-compose (docker-compose-arfea-2.yml):
#                openhab + servizi girano già in container.
#
#   B) NATIVE  — OpenHAB installato "nativo" sul sistema operativo (apt/deb),
#                senza Docker. Cartelle tipiche:
#                  - fino alla 2.5.x:  /etc/openhab2, /var/lib/openhab2,
#                                      /usr/share/openhab2/addons
#                  - dalla 3.x in poi: /etc/openhab,  /var/lib/openhab,
#                                      /usr/share/openhab/addons
#                Servizi systemd nativi tipici: openhab(2), habapp, mosquitto,
#                samba (smbd/nmbd), frontail.
#
# Sequenza (come richiesto):
#   1) Rileva se sul sistema c'è un OpenHAB attivo — via Docker (container)
#      oppure nativo (cartelle /etc/openhab(2) + unità systemd).
#   2) Migra all'ultima versione Docker con arfea-controller.
#
# Cosa fa (comune):
#   - Backup dei dati esistenti (le cartelle native NON vengono eliminate:
#     restano come backup).
#   - Estrae il tarball arfea-controller e configura arfea.yml.
#   - Avvia il nuovo stack (controller + servizi rilevati).
#
# Cosa fa in più per il caso NATIVE:
#   - Installa Docker se assente (può richiedere un reboot + ri-esecuzione).
#   - Copia conf/userdata/addons nativi in /opt/docker_store/openhab (owner 9001).
#   - Migra la config HABApp, abilita sul controller i servizi che erano nativi
#     (habapp, mosquitto, samba), rileva le porte seriali USB (zwave/modbus).
#   - A migrazione riuscita: systemctl stop + disable dei servizi nativi, così
#     al boot parte SOLO lo stack Docker (frontail viene solo fermato/disabilitato,
#     non serve più nelle nuove installazioni).
#
# Cosa NON tocca:
#   - I dati esistenti (vengono copiati/preservati, mai cancellati).
#   - Le cartelle native /etc/openhab(2), /var/lib/openhab(2): restano come backup.
#
# Uso:
#   sudo bash migrate-to-controller.sh
#   sudo bash migrate-to-controller.sh /path/old-compose.yml /path/tarball.tar.xz
#   sudo MIGRATE_MODE=native bash migrate-to-controller.sh   # forza la modalità
###############################################################################

set -e

# ── Parametri ──────────────────────────────────────────────────────────────
OLD_COMPOSE_PATH="${1:-}"
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
TARBALL_PATH="${2:-$SCRIPT_DIR/arfea-controller.tar.xz}"
FORCE_MODE="${MIGRATE_MODE:-}"        # "native" | "docker" per forzare

OH_UID=9001
OH_GID=9001
DATA_PATH="/opt/docker_store"
DEST="$DATA_PATH/openhab"             # target dei dati OpenHAB nel mondo Docker

timestamp() { date +"%F_%T_%Z"; }
log()  { echo "$(timestamp) $*"; }
warn() { echo "$(timestamp) ATTENZIONE: $*" >&2; }
die()  { echo "ERRORE: $*" >&2; exit 1; }

# ── Pre-check comuni ────────────────────────────────────────────────────────
[[ $EUID -eq 0 ]] || die "esegui come root (sudo)"

# Tarball: se manca ma c'è build-update-tarball.sh accanto, generalo al volo.
if [[ ! -f "$TARBALL_PATH" ]]; then
  if [[ -x "$SCRIPT_DIR/build-update-tarball.sh" ]]; then
    log "Tarball non presente: lo genero con build-update-tarball.sh ..."
    _tmpbuild=$(mktemp -d)
    "$SCRIPT_DIR/build-update-tarball.sh" "$_tmpbuild" >/dev/null
    TARBALL_PATH="$_tmpbuild/arfea-controller.tar.xz"
  fi
fi
[[ -f "$TARBALL_PATH" ]] || die "tarball non trovato in $TARBALL_PATH (passalo come 2° argomento)"

# ═════════════════════════════════════════════════════════════════════════════
# HELPER YAML (condivisi)
# ═════════════════════════════════════════════════════════════════════════════

# Abilita un servizio (enabled: false -> true) nel blocco del servizio dato.
enable_service() {
  awk -v svc="  ${1}:" '$0==svc{i=1} i&&/enabled:/{sub(/false/,"true");i=0} {print}' \
    "$2" > "$2.t" && mv "$2.t" "$2"
}

# Riscrive integralmente la lista "devices:" del SOLO servizio openhab.
#   $1 = file yml
#   $2 = lista device separati da newline ("src:tgt" ciascuno); vuota = rimuove
#        del tutto le voci device (il container parte senza seriali).
set_openhab_devices() {
  local f="$1" devlist="$2" has=0
  [[ -n "${devlist//[$'\n\r\t ']/}" ]] && has=1
  awk -v devlist="$devlist" -v has="$has" '
    BEGIN { n = split(devlist, D, "\n") }
    /^  openhab:[[:space:]]*$/ { inoh = 1 }
    inoh && /^  [A-Za-z0-9_-]+:[[:space:]]*$/ && $0 !~ /^  openhab:/ { inoh = 0 }
    {
      if (inoh && $0 ~ /^    devices:[[:space:]]*$/) {
        skip = 1
        if (has == "1") {
          print "    devices:"
          for (i = 1; i <= n; i++) if (D[i] != "") print "      - \"" D[i] "\""
        }
        next
      }
      if (skip == 1 && $0 ~ /^      -[[:space:]]/) { next }   # scarta vecchie voci
      if (skip == 1) skip = 0                                 # fine lista device
      print
    }
  ' "$f" > "$f.t" && mv "$f.t" "$f"
}

# Imposta il valore di -Dgnu.io.rxtx.SerialPorts nel EXTRA_JAVA_OPTS di openhab.
set_rxtx_ports() {
  local f="$1" ports="$2"
  sed -i -E "s#-Dgnu\.io\.rxtx\.SerialPorts=[^\" ]*#-Dgnu.io.rxtx.SerialPorts=${ports}#" "$f"
}

# Imposta il GID di dialout (group_add) del container openhab, se diverso da 20.
set_dialout_gid() {
  local f="$1" gid="$2"
  [[ "$gid" =~ ^[0-9]+$ && "$gid" != "20" ]] || return 0
  sed -i -E "s#^(      - \")20(\")#\1${gid}\2#" "$f"
}

# API key + disattivazione OTA al primo boot (comune ai due flussi).
configure_yml_base() {
  local YML="$1"
  ARFEA_API_KEY=$(openssl rand -hex 16)
  sed -i "s|CAMBIARE-CON-CHIAVE-UNICA|${ARFEA_API_KEY}|" "$YML"
  # Disabilita self-update automatico al boot: evita che il controller scarichi
  # un tarball remoto e applichi un update non desiderato subito dopo la
  # migrazione. Riabilitabile a mano in arfea.yml.
  sed -i 's|^  update_url:.*|  update_url: ""|' "$YML"
}

# Attende che il container openhab sia in esecuzione (max ~2 min).
wait_openhab_running() {
  log "Attendo che openhab parta..."
  local tries=0
  while [[ "$(docker inspect -f '{{.State.Running}}' openhab 2>/dev/null)" != "true" ]]; do
    tries=$((tries + 1))
    if [[ $tries -ge 60 ]]; then
      warn "openhab non partito dopo 2 minuti."
      break
    fi
    sleep 2
  done
}

# Importa i widget + pagine ARFEA (sitemap inclusa: il file .sitemap è già
# copiato da deploy_arfea_skeleton in conf/sitemaps). Delega al controller, che
# conia il token admin dalla console Karaf e fa le PUT REST. Non bloccante: il
# controller comunque reimporta i widget nuovi al proprio avvio.
import_arfea_ui() {
  log "Import widget/pagine ARFEA nella UI di OpenHAB..."
  local tries=0
  while ! curl -fsS --max-time 3 http://localhost:8888/api/health >/dev/null 2>&1; do
    tries=$((tries + 1)); [[ $tries -ge 30 ]] && break; sleep 2
  done
  if curl -fsS --max-time 3 http://localhost:8888/api/health >/dev/null 2>&1; then
    ( curl -s --max-time 240 -X POST http://localhost:8888/api/system/import-ui >/dev/null 2>&1 || true ) &
    log "  import widget/pagine avviato (il controller usa il token Karaf)"
  else
    warn "  controller non raggiungibile su :8888: i widget verranno importati al suo avvio, o con import-ui-components.sh"
  fi
}

# buildx (necessario per multi-arch su ARM).
ensure_buildx() {
  if ! docker buildx version &>/dev/null; then
    log "Installazione docker-buildx-plugin..."
    apt-get update -qq || true
    apt-get install -y -qq docker-buildx-plugin || \
      warn "buildx non installato, la build potrebbe fallire"
  fi
}

# ═════════════════════════════════════════════════════════════════════════════
# RILEVAMENTO SISTEMA
# ═════════════════════════════════════════════════════════════════════════════

svc_present() { systemctl list-unit-files --no-legend "${1}.service" 2>/dev/null | grep -q .; }
svc_active()  { systemctl is-active --quiet "$1" 2>/dev/null; }

# Layout OpenHAB nativo: popola CONF/USERDATA/ADDONS/OH_SUFFIX/NAT_OPENHAB_UNIT.
NATIVE_OH=false
CONF=""; USERDATA=""; ADDONS=""; OH_SUFFIX=""; NAT_OPENHAB_UNIT=""; OH_VERSION=""
detect_native_layout() {
  if [[ -d /etc/openhab2 ]]; then
    OH_SUFFIX="2"; CONF="/etc/openhab2"; USERDATA="/var/lib/openhab2"; ADDONS="/usr/share/openhab2/addons"
  elif [[ -d /etc/openhab ]]; then
    OH_SUFFIX="";  CONF="/etc/openhab";  USERDATA="/var/lib/openhab";  ADDONS="/usr/share/openhab/addons"
  fi
  # Unità systemd (conferma ulteriore anche se le cartelle sono state spostate)
  if svc_present openhab2;   then NAT_OPENHAB_UNIT="openhab2"; [[ -z "$CONF" ]] && OH_SUFFIX="2"
  elif svc_present openhab;  then NAT_OPENHAB_UNIT="openhab";  [[ -z "$CONF" ]] && OH_SUFFIX=""
  fi
  if [[ -n "$CONF" || -n "$NAT_OPENHAB_UNIT" ]]; then
    NATIVE_OH=true
    [[ -z "$CONF"     ]] && CONF="/etc/openhab${OH_SUFFIX}"
    [[ -z "$USERDATA" ]] && USERDATA="/var/lib/openhab${OH_SUFFIX}"
    [[ -z "$ADDONS"   ]] && ADDONS="/usr/share/openhab${OH_SUFFIX}/addons"
    OH_VERSION="$(dpkg-query -f '${Version}' -W "openhab${OH_SUFFIX}" 2>/dev/null || true)"
  fi
}

# Servizi companion nativi da migrare/fermare/disabilitare.
#   STOP_UNITS  : unità systemd da fermare (early) e disabilitare (a fine ok)
#   KILL_PROCS  : pattern di processi da fermare quando NON c'è unità systemd
#   ENABLE_CTRL : servizi da abilitare in arfea.yml
STOP_UNITS=(); KILL_PROCS=(); ENABLE_CTRL=()
NAT_HABAPP=false; NAT_MOSQUITTO=false; NAT_SAMBA=false; NAT_FRONTAIL=false
NAT_HABAPP_UNIT=""
detect_native_services() {
  # OpenHAB core: sempre presente in modalità native
  [[ -n "$NAT_OPENHAB_UNIT" ]] && STOP_UNITS+=("$NAT_OPENHAB_UNIT")

  # HABApp -> abilitato sul controller
  if svc_present habapp; then
    NAT_HABAPP=true; NAT_HABAPP_UNIT="habapp"; STOP_UNITS+=("habapp"); ENABLE_CTRL+=("habapp")
  elif pgrep -f 'HABApp' >/dev/null 2>&1; then
    NAT_HABAPP=true; KILL_PROCS+=("HABApp"); ENABLE_CTRL+=("habapp")
  fi

  # Mosquitto -> abilitato sul controller
  if svc_present mosquitto; then
    NAT_MOSQUITTO=true; STOP_UNITS+=("mosquitto"); ENABLE_CTRL+=("mosquitto")
  fi

  # Samba (smbd/nmbd o unità "samba") -> abilitato sul controller
  local sfound=false
  for u in smbd nmbd samba smb; do
    if svc_present "$u"; then STOP_UNITS+=("$u"); sfound=true; fi
  done
  if $sfound; then NAT_SAMBA=true; ENABLE_CTRL+=("samba"); fi

  # Frontail -> solo stop/disable (non più necessario, nessun servizio controller)
  if svc_present frontail; then
    NAT_FRONTAIL=true; STOP_UNITS+=("frontail")
  elif pgrep -f 'frontail' >/dev/null 2>&1; then
    NAT_FRONTAIL=true; KILL_PROCS+=("frontail")
  fi
}

# ── Rilevamento porte seriali usate dall'OpenHAB nativo ─────────────────────
# Union di: EXTRA_JAVA_OPTS (rxtx), riferimenti /dev/tty* e /dev/serial/by-id
# in conf + jsondb, e device fisicamente presenti (ttyUSB*/ttyACM*).
SERIAL_DEVICES=()   # path così come referenziati (usati per il mapping 1:1)
detect_native_serial() {
  local -A seen=(); local d
  local raw=""

  # 1) da /etc/default/openhab(2): gnu.io.rxtx.SerialPorts=/dev/a:/dev/b
  for f in /etc/default/openhab /etc/default/openhab2; do
    [[ -f "$f" ]] || continue
    local v
    v=$(grep -hoE 'gnu\.io\.rxtx\.SerialPorts=[^"[:space:]]*' "$f" 2>/dev/null | head -1 || true)
    v="${v#*=}"
    [[ -n "$v" ]] && raw+=$'\n'"${v//:/$'\n'}"
  done

  # 2) da conf (things/*.things, ecc.) e jsondb
  raw+=$'\n'"$(grep -rhoE '/dev/serial/by-id/[A-Za-z0-9_.:-]+' "$CONF" "$USERDATA/jsondb" 2>/dev/null || true)"
  raw+=$'\n'"$(grep -rhoE '/dev/tty(USB|ACM|AML|S)[0-9]+' "$CONF" "$USERDATA/jsondb" 2>/dev/null || true)"

  # 3) device fisicamente presenti
  for d in /dev/ttyUSB* /dev/ttyACM*; do [[ -e "$d" ]] && raw+=$'\n'"$d"; done

  # dedup preservando l'ordine
  while IFS= read -r d; do
    [[ -z "$d" ]] && continue
    [[ -n "${seen[$d]:-}" ]] && continue
    seen[$d]=1; SERIAL_DEVICES+=("$d")
  done <<< "$raw"
}

# ═════════════════════════════════════════════════════════════════════════════
# GARANZIA DOCKER (per il caso native senza Docker)
# ═════════════════════════════════════════════════════════════════════════════
ensure_docker() {
  if command -v docker &>/dev/null && docker info &>/dev/null; then
    return 0
  fi
  if command -v docker &>/dev/null; then
    log "Docker presente ma non attivo: avvio il servizio..."
    systemctl enable --now docker 2>/dev/null || true
    docker info &>/dev/null && return 0
  else
    log "Docker non installato: procedo all'installazione..."
    local OS_ID OS_CODENAME
    # shellcheck disable=SC1091
    . /etc/os-release 2>/dev/null || true
    OS_CODENAME="${VERSION_CODENAME:-$(command -v lsb_release >/dev/null && lsb_release -cs 2>/dev/null || echo stable)}"
    case "${ID:-debian}" in
      ubuntu) OS_ID="ubuntu" ;;
      *)      OS_ID="debian" ;;   # debian/armbian/raspbian -> repo debian
    esac
    apt-get update -qq || true
    apt-get install -y -qq ca-certificates curl gnupg
    install -m 0755 -d /etc/apt/keyrings
    rm -f /etc/apt/keyrings/docker.asc
    curl -fsSL "https://download.docker.com/linux/${OS_ID}/gpg" -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/${OS_ID} ${OS_CODENAME} stable" \
      > /etc/apt/sources.list.d/docker.list
    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    systemctl enable --now docker 2>/dev/null || true
    docker info &>/dev/null && return 0
  fi

  # Non è partito: su alcuni sistemi (kernel/moduli) serve un reboot.
  echo ""
  warn "Docker è installato ma il daemon non è ancora attivo."
  echo "  Probabilmente serve un RIAVVIO. Dopo il reboot ri-esegui:"
  echo "      sudo bash $0"
  echo "  Le cartelle native non sono state toccate: la migrazione riprenderà da qui."
  exit 2
}

# ═════════════════════════════════════════════════════════════════════════════
# BACKUP (comune, best-effort su /opt/docker_store se già esistente)
# ═════════════════════════════════════════════════════════════════════════════
BACKUP_FILE=""
backup_docker_store() {
  if [[ -d "$DATA_PATH" ]] && [[ -n "$(ls -A "$DATA_PATH" 2>/dev/null)" ]]; then
    BACKUP_FILE="/opt/docker_store-backup-$(date +%Y%m%d_%H%M%S).tar.gz"
    log "Backup di $DATA_PATH in $BACKUP_FILE (può richiedere alcuni minuti)..."
    tar --warning=no-file-changed -czf "$BACKUP_FILE" -C /opt docker_store 2>/dev/null || true
    [[ -f "$BACKUP_FILE" ]] && log "Backup completato ($(du -h "$BACKUP_FILE" | cut -f1))"
  else
    log "Nessun /opt/docker_store preesistente da backuppare."
  fi
}

extract_tarball() {
  log "Estrazione tarball arfea-controller..."
  mkdir -p "$DATA_PATH/arfea-controller"/{config,backups}
  tar -xJf "$TARBALL_PATH" --strip-components=1 -C "$DATA_PATH/arfea-controller/"
  [[ -f "$DATA_PATH/arfea-controller/config/arfea.yml" ]] || {
    echo "Contenuto del tarball:"; tar -tJf "$TARBALL_PATH" | head -20
    die "arfea.yml non trovato dopo l'estrazione del tarball"
  }
}

# ═════════════════════════════════════════════════════════════════════════════
# CREDENZIALI + OUTPUT FINALE (comuni)
# ═════════════════════════════════════════════════════════════════════════════
save_credentials() {
  local CRED_FILE="/root/arfea-credentials.txt"
  cat > "$CRED_FILE" <<EOF
ARFEA - migrazione completata il $(timestamp)

API Key arfea-controller:
  $ARFEA_API_KEY

Backup pre-migrazione:
  ${BACKUP_FILE:-(nessuno)}
EOF
  chmod 600 "$CRED_FILE"
  echo "$CRED_FILE"
}

# ═════════════════════════════════════════════════════════════════════════════
# FLUSSO A — MIGRAZIONE DA DOCKER-COMPOSE (comportamento storico)
# ═════════════════════════════════════════════════════════════════════════════
run_docker_migration() {
  command -v docker &>/dev/null || die "docker non installato"
  docker info &>/dev/null || die "Docker non attivo. systemctl start docker.service"

  # ── Rileva il vecchio compose ──
  if [[ -z "$OLD_COMPOSE_PATH" ]]; then
    for candidate in \
      /opt/docker_store/docker-compose-arfea-2.yml \
      /opt/docker_store/docker-compose.yml \
      /opt/docker-compose-arfea-2.yml \
      /home/openhab/docker-compose-arfea-2.yml \
      /root/docker-compose-arfea-2.yml
    do
      [[ -f "$candidate" ]] && { OLD_COMPOSE_PATH="$candidate"; break; }
    done
  fi
  if [[ -z "$OLD_COMPOSE_PATH" || ! -f "$OLD_COMPOSE_PATH" ]]; then
    echo "ATTENZIONE: vecchio docker-compose non trovato."
    read -r -p "Procedere comunque (solo stop container + setup controller)? (s/n): " yn
    [[ "$yn" =~ ^[SsYy] ]] || exit 0
    OLD_COMPOSE_PATH=""
  fi

  echo ""
  echo "════════════════════════════════════════════════════════════"
  echo "   MIGRAZIONE verso arfea-controller  (sorgente: DOCKER)"
  echo "════════════════════════════════════════════════════════════"
  echo ""
  echo "Vecchio compose: ${OLD_COMPOSE_PATH:-(non trovato)}"
  echo "Tarball:         $TARBALL_PATH"
  echo ""

  local MANAGED_NAMES="openhab habapp zwave-js-ui zigbee2mqtt node-red mosquitto samba docker-socket-proxy"
  local ACTIVE=""
  for name in $MANAGED_NAMES; do
    if docker ps --format '{{.Names}}' | grep -qx "$name"; then ACTIVE="$ACTIVE $name"; fi
  done

  inspect_devices_running() {
    docker inspect "$1" 2>/dev/null | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    if d:
        for dev in (d[0]['HostConfig'].get('Devices') or []):
            print(f\"{dev['PathOnHost']}:{dev['PathInContainer']}\")
except Exception:
    pass
" || true
  }
  parse_compose_services() {
    python3 -c "
import yaml, sys
try:
    with open('$1') as f:
        cfg = yaml.safe_load(f)
    for name, svc in (cfg.get('services') or {}).items():
        print(svc.get('container_name') or name)
except Exception as e:
    sys.stderr.write(str(e))
"
  }
  parse_compose_devices() {
    python3 -c "
import yaml, sys
try:
    with open('$1') as f:
        cfg = yaml.safe_load(f)
    for name, svc in (cfg.get('services') or {}).items():
        cn = svc.get('container_name') or name
        for d in (svc.get('devices') or []):
            if isinstance(d, str):
                parts = d.split(':')
                if len(parts) >= 2:
                    print(f\"{cn}|{parts[0]}:{parts[1]}\")
except Exception as e:
    sys.stderr.write(str(e))
"
  }

  if [[ -z "$ACTIVE" && -n "$OLD_COMPOSE_PATH" ]]; then
    echo "Nessun container attivo. Analizzo il vecchio compose..."
    for cn in $(parse_compose_services "$OLD_COMPOSE_PATH"); do
      if echo "$MANAGED_NAMES" | tr ' ' '\n' | grep -qx "$cn"; then ACTIVE="$ACTIVE $cn"; fi
    done
  fi
  echo "Container rilevati:$ACTIVE"
  echo ""

  # ── Device paths ──
  local ZWAVE_DEVICE="" ZIGBEE_DEVICE=""
  local OPENHAB_DEVICES=()
  if echo " $ACTIVE " | grep -q " zwave-js-ui "; then
    ZWAVE_DEVICE=$(inspect_devices_running zwave-js-ui | head -1 | cut -d: -f1)
  fi
  if echo " $ACTIVE " | grep -q " zigbee2mqtt "; then
    ZIGBEE_DEVICE=$(inspect_devices_running zigbee2mqtt | head -1 | cut -d: -f1)
  fi
  if echo " $ACTIVE " | grep -q " openhab "; then
    while IFS= read -r line; do
      [[ -n "$line" ]] && OPENHAB_DEVICES+=("${line}:rwm")
    done < <(inspect_devices_running openhab)
  fi
  if [[ -n "$OLD_COMPOSE_PATH" ]]; then
    while IFS= read -r line; do
      [[ -z "$line" ]] && continue
      local cn="${line%%|*}" dev="${line##*|}"
      case "$cn" in
        zwave-js-ui|zwave) [[ -z "$ZWAVE_DEVICE" ]] && ZWAVE_DEVICE="${dev%%:*}" ;;
        zigbee2mqtt) [[ -z "$ZIGBEE_DEVICE" ]] && ZIGBEE_DEVICE="${dev%%:*}" ;;
        openhab)
          if ! printf '%s\n' "${OPENHAB_DEVICES[@]}" | grep -q "^${dev}:rwm$"; then
            OPENHAB_DEVICES+=("${dev}:rwm")
          fi ;;
      esac
    done < <(parse_compose_devices "$OLD_COMPOSE_PATH")
  fi

  echo "Device paths rilevati:"
  echo "  Z-Wave:   ${ZWAVE_DEVICE:-(nessuno)}"
  echo "  Zigbee:   ${ZIGBEE_DEVICE:-(nessuno)}"
  echo "  OpenHAB:  ${OPENHAB_DEVICES[*]:-(nessuno)}"
  echo ""

  local OPENHAB_IMAGE_DETECTED
  OPENHAB_IMAGE_DETECTED=$(docker inspect openhab --format '{{.Config.Image}}' 2>/dev/null || echo "")
  [[ -n "$OPENHAB_IMAGE_DETECTED" ]] && echo "Immagine OpenHAB corrente: $OPENHAB_IMAGE_DETECTED"
  echo ""

  echo "OPERAZIONI CHE VERRANNO ESEGUITE:"
  echo "  1) Backup completo di /opt/docker_store/"
  echo "  2) Stop dei container attivi (i dati NON vengono toccati)"
  echo "  3) Estrazione tarball arfea-controller"
  echo "  4) Configurazione arfea.yml con servizi rilevati + API key generata"
  echo "  5) Avvio del nuovo stack"
  echo ""
  read -r -p "Procedere? (s/n): " confirm
  [[ "$confirm" =~ ^[SsYy] ]] || { echo "Annullato."; exit 0; }

  # 1. Backup
  echo ""
  log "[1/5] Backup..."
  backup_docker_store
  [[ -f "$BACKUP_FILE" ]] || die "backup fallito"

  # 2. Stop container vecchi
  echo ""
  log "[2/5] Stop container vecchi..."
  if [[ -n "$OLD_COMPOSE_PATH" ]]; then
    ( cd "$(dirname "$OLD_COMPOSE_PATH")" && docker compose -f "$OLD_COMPOSE_PATH" down 2>/dev/null ) || true
  fi
  for c in $ACTIVE; do
    docker stop "$c" 2>/dev/null || true
    docker rm -f "$c" 2>/dev/null || true
  done
  log "Container vecchi rimossi"

  # 3. Estrai tarball
  echo ""
  log "[3/5] Estrazione tarball arfea-controller..."
  extract_tarball

  # 4. Configura arfea.yml
  echo ""
  log "[4/5] Configurazione arfea.yml..."
  local YML="$DATA_PATH/arfea-controller/config/arfea.yml"
  configure_yml_base "$YML"

  for name in $ACTIVE; do
    case "$name" in
      docker-socket-proxy) ;;
      habapp|node-red|samba) enable_service "$name" "$YML" ;;
      zwave-js-ui)  enable_service "zwave-js-ui" "$YML" ;;
      zigbee2mqtt)  enable_service "zigbee2mqtt" "$YML" ;;
      mosquitto)    enable_service "mosquitto" "$YML" ;;
    esac
  done

  [[ -n "$ZWAVE_DEVICE" ]]  && sed -i "s|/dev/ttyACM0:/dev/zwave|${ZWAVE_DEVICE}:/dev/zwave|" "$YML"
  [[ -n "$ZIGBEE_DEVICE" ]] && sed -i "s|/dev/serial/by-id/usb-ITEAD_SONOFF_Zigbee_3.0_USB_Dongle_Plus_V2_20231031184237-if00:/dev/zigbee|${ZIGBEE_DEVICE}:/dev/zigbee|" "$YML"

  if [[ ${#OPENHAB_DEVICES[@]} -gt 0 ]]; then
    local devices_block="    devices:"
    for dev in "${OPENHAB_DEVICES[@]}"; do
      devices_block="${devices_block}\n      - \"${dev}\""
    done
    awk -v devs="$devices_block" '
      /network_mode: host/ && !done { print; printf "%s\n", devs; done=1; next }
      { print }
    ' "$YML" > "${YML}.tmp" && mv "${YML}.tmp" "$YML"
  fi

  if [[ -n "$OPENHAB_IMAGE_DETECTED" && "$OPENHAB_IMAGE_DETECTED" != "openhab/openhab:5.1.4" ]]; then
    log "Aggiorno image openhab in arfea.yml: $OPENHAB_IMAGE_DETECTED"
    sed -i "s|image: \"openhab/openhab:5.1.4\"|image: \"${OPENHAB_IMAGE_DETECTED}\"|" "$YML"
  fi
  log "arfea.yml configurato (API key: $ARFEA_API_KEY)"

  # 5. Avvia stack
  echo ""
  log "[5/5] Build e avvio arfea-controller..."
  ensure_buildx
  ( cd "$DATA_PATH/arfea-controller" && docker compose build && docker compose up -d )
  wait_openhab_running
  import_arfea_ui

  local CRED_FILE; CRED_FILE=$(save_credentials)
  echo ""
  echo "═══════════════════════════════════════════════════════════════"
  echo "   MIGRAZIONE COMPLETATA (sorgente: DOCKER)"
  echo "═══════════════════════════════════════════════════════════════"
  echo ""
  echo "  API Key arfea-controller:  $ARFEA_API_KEY"
  echo "  Backup pre-migrazione:     ${BACKUP_FILE:-(nessuno)}"
  echo "  Credenziali salvate in:    $CRED_FILE"
  echo ""
  echo "  Web UI arfea-controller:   http://<IP>:8888"
  echo "  OpenHAB:                   http://<IP>:8080"
  echo ""
  echo "  Stato container:"
  docker ps --format '  {{.Names}}: {{.Status}}'
  echo ""
  echo "  Vecchio compose: ${OLD_COMPOSE_PATH:-N/A}"
  echo "    (puoi rinominarlo/spostarlo per evitare avvii accidentali)"
  echo ""
  if [[ -n "$BACKUP_FILE" ]]; then
    echo "  In caso di problemi, ripristina con:"
    echo "    cd $DATA_PATH/arfea-controller && docker compose down"
    echo "    sudo rm -rf $DATA_PATH"
    echo "    sudo tar -xzf $BACKUP_FILE -C /opt"
    echo "    cd <dir-vecchio-compose> && docker compose -f ${OLD_COMPOSE_PATH:-<compose>} up -d"
  fi
  echo ""
}

# ═════════════════════════════════════════════════════════════════════════════
# FLUSSO B — MIGRAZIONE DA OPENHAB NATIVO (nuovo)
# ═════════════════════════════════════════════════════════════════════════════

# Ferma i servizi nativi (prima della copia dati e del boot Docker).
stop_native_services() {
  local u
  for u in "${STOP_UNITS[@]}"; do
    log "  systemctl stop $u"
    systemctl stop "$u" 2>/dev/null || true
  done
  local p
  for p in "${KILL_PROCS[@]}"; do
    log "  pkill -f $p"
    pkill -f "$p" 2>/dev/null || true
  done
}

# Disabilita i servizi nativi (solo a migrazione riuscita): niente autostart.
disable_native_services() {
  local u
  for u in "${STOP_UNITS[@]}"; do
    log "  systemctl disable $u"
    systemctl disable "$u" 2>/dev/null || true
    systemctl stop "$u" 2>/dev/null || true
  done
}

# Rimuove i banner/MOTD lasciati da openhabian che, senza openHAB nativo, danno
# errore al login SSH (es. "FireMotD: command not found",
# "sed: can't read /var/lib/openhab2/etc/version.properties", welcome openHAB).
# Tutto reversibile: i file toccati vengono copiati nel backup prima di agire.
cleanup_login_banners() {
  local ts bak
  ts=$(date +%Y%m%d_%H%M%S)
  bak="$DATA_PATH/arfea-controller/backups/login-banners-$ts"
  mkdir -p "$bak"
  local touched=false

  # 1) script di login openhabian in /etc/profile.d e /etc/update-motd.d
  #    (FireMotD, lettura version.properties, welcome openHAB, ...)
  local pat='FireMotD|version\.properties|openhabian|openhab'
  local dir f
  for dir in /etc/profile.d /etc/update-motd.d; do
    [[ -d "$dir" ]] || continue
    while IFS= read -r f; do
      [[ -f "$f" ]] || continue
      touched=true
      mkdir -p "$bak$dir"
      cp -a "$f" "$bak$dir/" 2>/dev/null || true
      if [[ "$dir" == /etc/update-motd.d ]]; then
        chmod -x "$f" 2>/dev/null || true            # basta togliere l'eseguibile
        log "  banner: disattivato $f"
      else
        mv "$f" "$f.disabled-arfea"                  # profile.d: rinomino
        log "  banner: disattivato $f"
      fi
    done < <(grep -rilE "$pat" "$dir" 2>/dev/null || true)
  done

  # 2) /etc/motd statico con il banner openHAB
  if [[ -f /etc/motd ]] && grep -qiE 'openhab|openhabian' /etc/motd; then
    cp -a /etc/motd "$bak/etc-motd" 2>/dev/null || true
    : > /etc/motd
    touched=true
    log "  banner: svuotato /etc/motd"
  fi

  # 3) righe che invocano FireMotD / version.properties nei bashrc/profile
  local files=(/etc/bash.bashrc /etc/profile) h
  for h in /root /home/*; do
    [[ -d "$h" ]] || continue
    files+=("$h/.bashrc" "$h/.profile" "$h/.bash_profile")
  done
  for f in "${files[@]}"; do
    [[ -f "$f" ]] || continue
    if grep -qE 'FireMotD|version\.properties' "$f"; then
      cp -a "$f" "$bak/$(echo "$f" | tr / _)" 2>/dev/null || true
      sed -i -E '/FireMotD/d; /version\.properties/d' "$f"
      touched=true
      log "  banner: ripulite righe in $f"
    fi
  done

  if $touched; then
    log "Banner di login openhabian rimossi (backup in $bak)"
  else
    log "Nessun banner di login openhabian da rimuovere."
  fi
}

# Individua la cartella di config HABApp nativa (da ExecStart o path comuni).
detect_habapp_config() {
  local dir="" exec_line
  if [[ -n "$NAT_HABAPP_UNIT" ]]; then
    exec_line=$(systemctl cat "$NAT_HABAPP_UNIT" 2>/dev/null | grep -E '^\s*ExecStart=' | head -1 || true)
    dir=$(echo "$exec_line" | grep -oE '(-c|--config)[= ]+[^ ]+' | grep -oE '/[^ ]+' | head -1 || true)
  fi
  if [[ -z "$dir" ]]; then
    for c in "$CONF/habapp" /etc/openhab/habapp /etc/openhab2/habapp /opt/habapp/config /opt/habapp /etc/habapp; do
      if [[ -f "$c/config.yml" || -d "$c/rules" ]]; then dir="$c"; break; fi
    done
  fi
  echo "$dir"
}

# Copia i dati OpenHAB nativi nella struttura Docker (owner 9001:9001).
copy_native_data() {
  mkdir -p "$DEST"/{conf,userdata,addons,cont-init.d}

  local have_rsync=false; command -v rsync &>/dev/null && have_rsync=true

  # conf -> /openhab/conf
  if [[ -d "$CONF" ]]; then
    log "  conf:     $CONF -> $DEST/conf"
    if $have_rsync; then rsync -a "$CONF"/ "$DEST/conf"/
    else cp -a "$CONF"/. "$DEST/conf"/; fi
  fi

  # userdata -> /openhab/userdata (escludo cache/tmp/logs: rigenerati e legati
  # alla versione; vanno ripuliti in fase di upgrade)
  if [[ -d "$USERDATA" ]]; then
    log "  userdata: $USERDATA -> $DEST/userdata (escludo cache/tmp/logs)"
    if $have_rsync; then
      rsync -a --exclude 'cache' --exclude 'tmp' --exclude 'logs' "$USERDATA"/ "$DEST/userdata"/
    else
      cp -a "$USERDATA"/. "$DEST/userdata"/
      rm -rf "$DEST/userdata/cache" "$DEST/userdata/tmp" "$DEST/userdata/logs"
    fi
  fi

  # addons manuali (kar/jar)
  if [[ -d "$ADDONS" ]] && [[ -n "$(ls -A "$ADDONS" 2>/dev/null)" ]]; then
    log "  addons:   $ADDONS -> $DEST/addons"
    if $have_rsync; then rsync -a "$ADDONS"/ "$DEST/addons"/
    else cp -a "$ADDONS"/. "$DEST/addons"/; fi
  fi

  # HABApp: config nativa -> $DEST/conf/habapp
  if $NAT_HABAPP; then
    local hcfg; hcfg=$(detect_habapp_config)
    if [[ -n "$hcfg" && -d "$hcfg" ]]; then
      log "  habapp:   $hcfg -> $DEST/conf/habapp"
      mkdir -p "$DEST/conf/habapp"
      if $have_rsync; then rsync -a "$hcfg"/ "$DEST/conf/habapp"/
      else cp -a "$hcfg"/. "$DEST/conf/habapp"/; fi
    else
      warn "HABApp attivo ma config non trovata: migra a mano le regole in $DEST/conf/habapp"
    fi
  fi

  chown -R "$OH_UID:$OH_GID" "$DEST"
}

# Aggiunge i file skeleton arfea (integrazione controller) SENZA sovrascrivere
# i file del cliente (no-clobber) + cont-init.d (necessario al container).
deploy_arfea_skeleton() {
  local SK="$DATA_PATH/arfea-controller/skeleton-openhab"
  [[ -d "$SK" ]] || { warn "skeleton-openhab assente nel tarball, salto"; return 0; }
  if [[ -d "$SK/conf" ]]; then
    log "  skeleton conf (no-clobber) -> $DEST/conf"
    cp -rn "$SK/conf/." "$DEST/conf/" 2>/dev/null || true
  fi
  if [[ -d "$SK/cont-init.d" ]]; then
    log "  cont-init.d -> $DEST/cont-init.d"
    cp -r "$SK/cont-init.d/." "$DEST/cont-init.d/"
    chmod +x "$DEST/cont-init.d/"* 2>/dev/null || true
  fi
  chown -R "$OH_UID:$OH_GID" "$DEST/conf" "$DEST/cont-init.d"
}

# mosquitto: assicura una config minima (il container deve poter partire).
ensure_mosquitto_config() {
  $NAT_MOSQUITTO || return 0
  if [[ ! -f "$DATA_PATH/mosquitto/config/mosquitto.conf" ]]; then
    log "  mosquitto: creo config minima ($DATA_PATH/mosquitto/config/mosquitto.conf)"
    mkdir -p "$DATA_PATH/mosquitto"/{config,data,log}
    printf 'allow_anonymous true\nlistener 1883 0.0.0.0\n' > "$DATA_PATH/mosquitto/config/mosquitto.conf"
  fi
}

configure_yml_native() {
  local YML="$DATA_PATH/arfea-controller/config/arfea.yml"
  configure_yml_base "$YML"

  # Abilita i servizi companion che erano nativi
  local dedup=" "
  for svc in "${ENABLE_CTRL[@]}"; do
    [[ "$dedup" == *" $svc "* ]] && continue
    dedup+="$svc "
    log "  abilito servizio controller: $svc"
    enable_service "$svc" "$YML"
  done

  # Porte seriali: mapping 1:1 (le config dei binding nativi referenziano il
  # path reale, quindi NON rimappiamo su /dev/zwave). rxtx = nomi tty reali.
  # Mappo SOLO i device fisicamente presenti: un device referenziato ma assente
  # farebbe fallire "docker compose up". Quelli mancanti vengono segnalati.
  local devlist="" rxtx="" gid="" missing=""
  local -A rxseen=()
  for d in "${SERIAL_DEVICES[@]}"; do
    local real; real=$(readlink -f "$d" 2>/dev/null || echo "$d")
    if [[ ! -e "$real" ]]; then
      missing+="  - $d"$'\n'
      continue
    fi
    devlist+="${d}:${d}"$'\n'
    if [[ "$real" == /dev/tty* && -z "${rxseen[$real]:-}" ]]; then
      rxseen[$real]=1
      rxtx+="${rxtx:+:}$real"
    fi
    [[ -z "$gid" ]] && gid=$(stat -c '%g' "$real" 2>/dev/null || true)
  done

  if [[ -n "$missing" ]]; then
    warn "device seriali referenziati ma NON presenti (non mappati nel container):"
    printf '%s' "$missing" >&2
    echo "         Collegali e aggiungili a mano in arfea.yml (blocco openhab: devices + rxtx)." >&2
  fi

  set_openhab_devices "$YML" "$devlist"
  set_rxtx_ports "$YML" "$rxtx"
  [[ -n "$gid" ]] && set_dialout_gid "$YML" "$gid"

  log "arfea.yml configurato (API key: $ARFEA_API_KEY)"
}

run_native_migration() {
  detect_native_services
  detect_native_serial

  echo ""
  echo "════════════════════════════════════════════════════════════"
  echo "   MIGRAZIONE verso arfea-controller  (sorgente: NATIVO)"
  echo "════════════════════════════════════════════════════════════"
  echo ""
  echo "OpenHAB nativo rilevato:"
  echo "  Versione pacchetto: ${OH_VERSION:-sconosciuta}   (unità: ${NAT_OPENHAB_UNIT:-n/d})"
  echo "  conf:     $CONF"
  echo "  userdata: $USERDATA"
  echo "  addons:   $ADDONS"
  echo ""
  echo "Servizi nativi rilevati (verrà fatto stop + disable):"
  printf '  - OpenHAB (%s)\n' "${NAT_OPENHAB_UNIT:-processo}"
  $NAT_HABAPP    && echo "  - HABApp      -> abilitato su controller"
  $NAT_MOSQUITTO && echo "  - Mosquitto   -> abilitato su controller"
  $NAT_SAMBA     && echo "  - Samba       -> abilitato su controller"
  $NAT_FRONTAIL  && echo "  - Frontail    -> solo disattivato (non più necessario)"
  echo ""
  echo "Porte seriali rilevate (mapping 1:1 nel container openhab):"
  if [[ ${#SERIAL_DEVICES[@]} -gt 0 ]]; then
    for d in "${SERIAL_DEVICES[@]}"; do echo "  - $d"; done
  else
    echo "  (nessuna) — se usi zwave/modbus verifica manualmente in arfea.yml"
  fi
  echo ""

  # Avviso versione: salto di major (2.x -> 5.x) può richiedere interventi manuali
  local major="${OH_VERSION%%.*}"
  if [[ -n "$major" && "$major" =~ ^[0-9]+$ && "$major" -lt 4 ]]; then
    warn "OpenHAB nativo major=$major: il salto diretto all'immagine 5.x può"
    echo "         richiedere una revisione manuale di things/binding (soprattutto da 2.x)."
    echo "         I dati vengono comunque copiati; verifica il funzionamento dopo l'avvio."
  fi

  echo "OPERAZIONI:"
  echo "  1) (se assente) installazione Docker"
  echo "  2) Backup /opt/docker_store (se presente) — le cartelle native NON vengono cancellate"
  echo "  3) Stop dei servizi nativi (openhab/habapp/mosquitto/samba/frontail)"
  echo "  4) Copia conf/userdata/addons (+ habapp) in $DEST (owner 9001:9001)"
  echo "  5) Estrazione tarball + arfea.yml (servizi + porte seriali)"
  echo "  6) Build e avvio dello stack Docker"
  echo "  7) Se tutto ok: systemctl disable dei servizi nativi + pulizia banner openhabian"
  echo ""
  read -r -p "Procedere? (s/n): " confirm
  [[ "$confirm" =~ ^[SsYy] ]] || { echo "Annullato."; exit 0; }

  echo ""; log "[1/7] Verifica/installazione Docker..."
  ensure_docker

  echo ""; log "[2/7] Backup..."
  backup_docker_store

  echo ""; log "[3/7] Stop servizi nativi..."
  stop_native_services

  echo ""; log "[4/7] Copia dati OpenHAB nativi in $DEST..."
  copy_native_data

  echo ""; log "[5/7] Estrazione tarball + configurazione arfea.yml..."
  extract_tarball
  deploy_arfea_skeleton
  ensure_mosquitto_config
  configure_yml_native

  echo ""; log "[6/7] Build e avvio arfea-controller..."
  ensure_buildx
  # Non hard-fail: i servizi nativi sono già fermi; in caso di errore proseguo
  # fino al controllo di stato che stampa le istruzioni di rollback.
  ( cd "$DATA_PATH/arfea-controller" && docker compose build && docker compose up -d ) \
    || warn "build/avvio stack terminato con errore, verifico lo stato..."
  wait_openhab_running

  # Verifica minima di successo: container openhab in esecuzione.
  if [[ "$(docker inspect -f '{{.State.Running}}' openhab 2>/dev/null)" != "true" ]]; then
    echo ""
    warn "openhab non risulta in esecuzione: NON disabilito i servizi nativi."
    echo "  Controlla:  cd $DATA_PATH/arfea-controller && docker compose logs -f openhab"
    echo "  Rollback:   docker compose down; poi riavvia i servizi nativi con"
    echo "              systemctl start ${NAT_OPENHAB_UNIT:-openhab}"
    exit 1
  fi

  import_arfea_ui

  echo ""; log "[7/7] Disabilito i servizi nativi (autostart solo Docker)..."
  disable_native_services

  log "Pulizia banner di login openhabian (FireMotD, welcome openHAB, ...)..."
  cleanup_login_banners

  local CRED_FILE; CRED_FILE=$(save_credentials)
  echo ""
  echo "═══════════════════════════════════════════════════════════════"
  echo "   MIGRAZIONE COMPLETATA (sorgente: NATIVO)"
  echo "═══════════════════════════════════════════════════════════════"
  echo ""
  echo "  API Key arfea-controller:  $ARFEA_API_KEY"
  echo "  Backup pre-migrazione:     ${BACKUP_FILE:-(nessuno)}"
  echo "  Credenziali salvate in:    $CRED_FILE"
  echo ""
  echo "  Cartelle native preservate come backup (NON cancellate):"
  echo "    $CONF"
  echo "    $USERDATA"
  echo "    $ADDONS"
  echo ""
  echo "  Servizi nativi: fermati e disabilitati (partono solo i container Docker)."
  echo "  Web UI arfea-controller:   http://<IP>:8888"
  echo "  OpenHAB:                   http://<IP>:8080"
  echo ""
  echo "  Stato container:"
  docker ps --format '  {{.Names}}: {{.Status}}'
  echo ""
  if $NAT_HABAPP; then
    echo "  NB HABApp: verifica in $DEST/conf/habapp/config.yml i parametri di"
    echo "     connessione (URL OpenHAB / MQTT) per l'ambiente containerizzato."
  fi
  echo ""
}

# ═════════════════════════════════════════════════════════════════════════════
# DISPATCHER
# ═════════════════════════════════════════════════════════════════════════════
main() {
  detect_native_layout

  local DOCKER_OH=false
  if command -v docker &>/dev/null && docker info &>/dev/null; then
    docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx openhab && DOCKER_OH=true
  fi

  local MODE=""
  case "$FORCE_MODE" in
    native) MODE="native" ;;
    docker) MODE="docker" ;;
    *)
      if $DOCKER_OH; then MODE="docker"
      elif $NATIVE_OH; then MODE="native"
      elif command -v docker &>/dev/null && docker info &>/dev/null; then MODE="docker"
      else
        die "nessun OpenHAB rilevato (né container Docker né installazione nativa in /etc/openhab(2)). Forza con MIGRATE_MODE=native|docker."
      fi
      ;;
  esac

  log "Modalità di migrazione: $MODE"
  if [[ "$MODE" == "native" ]]; then
    $NATIVE_OH || detect_native_layout
    $NATIVE_OH || die "modalità native forzata ma nessuna installazione nativa trovata in /etc/openhab(2)."
    run_native_migration
  else
    run_docker_migration
  fi
}

main "$@"
