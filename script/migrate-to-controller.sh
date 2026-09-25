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
#   - Controlla che ci sia spazio su disco PRIMA di fermare qualunque cosa.
#   - Backup dei dati esistenti (le cartelle native NON vengono eliminate:
#     restano come backup).
#   - Estrae il tarball arfea-controller e configura arfea.yml, con update_url
#     e releases_url SEMPRE valorizzati (la centralina resta sotto OTA).
#   - Avvia il nuovo stack (controller + servizi rilevati).
#
# Cosa fa in più per il caso DOCKER:
#   - Conserva la versione di OpenHAB che girava (immagine del container, del
#     vecchio compose o, se il tag è mobile, quella scritta nell'userdata): la
#     migrazione cambia la struttura, non la versione. L'upgrade si fa dopo, con
#     la release certificata dalla Web UI, che prima fa il backup.
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
#
# Variabili:
#   MIGRATE_MODE=native|docker     forza il flusso
#   MIGRATE_SKIP_SPACE_CHECK=1     prosegue anche se la stima dello spazio non basta
#   ARFEA_UPDATE_URL=…             URL OTA del controller (default: cloud domoticaundici)
###############################################################################

set -e

# ── Parametri ──────────────────────────────────────────────────────────────
OLD_COMPOSE_PATH="${1:-}"
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
TARBALL_PATH="${2:-$SCRIPT_DIR/arfea-controller.tar.xz}"
FORCE_MODE="${MIGRATE_MODE:-}"        # "native" | "docker" per forzare
SKIP_SPACE_CHECK="${MIGRATE_SKIP_SPACE_CHECK:-}"
# update_url non resta mai vuoto: senza, la centralina non riceve più l'OTA e
# nessuno se ne accorge (Redmine #192).
ARFEA_UPDATE_URL="${ARFEA_UPDATE_URL:-https://cloud.domoticaundici.it/ota/arfea-controller.tar.xz}"

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

# API key + canali OTA (comune ai due flussi).
configure_yml_base() {
  local YML="$1"
  ARFEA_API_KEY=$(openssl rand -hex 16)
  sed -i "s|CAMBIARE-CON-CHIAVE-UNICA|${ARFEA_API_KEY}|" "$YML"
  # update_url SEMPRE valorizzato (Redmine #192). Prima qui si svuotava, per non
  # auto-aggiornare il controller al primo avvio: la centralina migrata restava
  # fuori dall'OTA per sempre. Se il controller pubblicato è più nuovo del
  # tarball usato qui, al primo avvio si aggiorna da solo: è voluto.
  sed -i "s|^  update_url:.*|  update_url: \"${ARFEA_UPDATE_URL}\"|" "$YML"
  sed -i "s|^  releases_url:.*|  releases_url: \"${ARFEA_UPDATE_URL%/*}/releases.json\"|" "$YML"
  grep -qE '^  update_url: "https?://' "$YML" || die "update_url non impostato in $YML"
}

# Scrive l'immagine del SOLO servizio openhab (la prima riga image: del blocco).
# Non dipende dal tag del template, che cambia a ogni release. Ritorna 0 solo se
# la riga risulta scritta.
set_openhab_image() {
  local f="$1" img="$2"
  awk -v img="$img" '
    /^  openhab:[[:space:]]*$/ { inoh = 1 }
    inoh && /^  [A-Za-z0-9_-]+:[[:space:]]*$/ && $0 !~ /^  openhab:/ { inoh = 0 }
    inoh && !done && /^    image:/ { print "    image: \"" img "\""; done = 1; next }
    { print }
  ' "$f" > "$f.t" && mv "$f.t" "$f"
  grep -qF "    image: \"$img\"" "$f"
}

# Versione di OpenHAB scritta nell'userdata (quella con cui i dati sono allineati).
openhab_userdata_version() {
  local vp="$DEST/userdata/etc/version.properties"
  [[ -f "$vp" ]] || return 0
  awk -F: '/openhab-distro/ { gsub(/[[:space:]]/, "", $2); print $2; exit }' "$vp"
}

# ── Spazio libero, prima di toccare qualunque cosa (Redmine #191) ───────────
# Stima per eccesso di cosa scrive la migrazione: il backup di /opt/docker_store
# (tar.gz in /opt: nel caso peggiore non comprime), nel caso nativo la copia di
# conf/userdata/addons, il pacchetto addon di OpenHAB (~600 MB scaricati che
# Karaf estrae in ~1,2 GB) e le immagini Docker. Su una eMMC da 16 GB quasi
# piena la migrazione si fermerebbe a metà, coi servizi vecchi già fermi.
KAR_MB=1800
MARGIN_MB=500
mb_of()           { du -sm "$@" 2>/dev/null | awk '{ s += $1 } END { print s + 0 }'; }
existing_parent() { local d="$1"; while [[ ! -e "$d" ]]; do d=$(dirname "$d"); done; echo "$d"; }
free_mb_of()      { df -Pm "$1" | awk 'NR == 2 { print $4 }'; }
mount_of()        { df -P "$1" | awk 'NR == 2 { print $6 }'; }

check_disk_space() {
  local mode="$1" backup_mb=0 native_mb=0 images_mb=1024
  [[ -d "$DATA_PATH" ]] && backup_mb=$(mb_of "$DATA_PATH")
  if [[ "$mode" == native ]]; then
    native_mb=$(du -sm --exclude=cache --exclude=tmp --exclude=logs --exclude='openhab-addons-*.kar' \
                  "$CONF" "$USERDATA" "$ADDONS" 2>/dev/null \
                | awk '{ s += $1 } END { print s + 0 }')
    images_mb=2048        # anche OpenHAB e i servizi, non solo il controller
  fi
  local opt_dir docker_dir
  opt_dir=$(existing_parent "$DATA_PATH")
  docker_dir=$(docker info -f '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker)
  docker_dir=$(existing_parent "$docker_dir")
  local opt_need=$((backup_mb + native_mb + KAR_MB + MARGIN_MB)) ok=true

  echo "Spazio su disco (stima per eccesso):"
  echo "  backup di $DATA_PATH:    ${backup_mb} MB"
  [[ "$mode" == native ]] && echo "  dati OpenHAB nativi:      ${native_mb} MB"
  echo "  pacchetto addon OpenHAB:  ${KAR_MB} MB"
  echo "  immagini Docker:          ${images_mb} MB"
  echo "  margine:                  ${MARGIN_MB} MB"
  if [[ "$(mount_of "$opt_dir")" == "$(mount_of "$docker_dir")" ]]; then
    local need=$((opt_need + images_mb)) free
    free=$(free_mb_of "$opt_dir")
    echo "  servono ${need} MB su $(mount_of "$opt_dir"), liberi ${free} MB"
    (( free >= need )) || ok=false
  else
    local f1 f2
    f1=$(free_mb_of "$opt_dir"); f2=$(free_mb_of "$docker_dir")
    echo "  servono ${opt_need} MB su $(mount_of "$opt_dir") (liberi ${f1}) e ${images_mb} MB su $(mount_of "$docker_dir") (liberi ${f2})"
    (( f1 >= opt_need && f2 >= images_mb )) || ok=false
  fi
  echo ""
  if ! $ok; then
    if [[ -n "$SKIP_SPACE_CHECK" ]]; then
      warn "spazio insufficiente secondo la stima: proseguo perché MIGRATE_SKIP_SPACE_CHECK è impostato."
    else
      die "spazio insufficiente, nessun servizio è stato toccato. Libera spazio (docker image prune -a, log e backup vecchi in /opt) oppure, se la stima è troppo prudente, rilancia con MIGRATE_SKIP_SPACE_CHECK=1"
    fi
  fi
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

# docker compose + buildx (la build del controller li vuole entrambi su ARM),
# garantiti PRIMA di fermare qualunque servizio (Redmine #232). Il docker.io di
# Ubuntu non porta né l'uno né l'altro, e i pacchetti docker-*-plugin esistono
# solo nel repo Docker: prima la build falliva all'avvio del nuovo stack, con i
# servizi vecchi già fermi. Con docker.io si usano i pacchetti Ubuntu
# (docker-compose-v2, docker-buildx) e si aggiorna docker.io insieme: il daemon
# riparte e con lui i container già presenti.
ensure_compose_buildx() {
  if docker compose version &>/dev/null && docker buildx version &>/dev/null; then
    return 0
  fi
  log "Installazione di docker compose e buildx..."
  apt-get update -qq || true
  if dpkg -s docker.io &>/dev/null; then
    apt-get install -y -qq docker.io docker-compose-v2 docker-buildx || true
  else
    apt-get install -y -qq docker-compose-plugin docker-buildx-plugin || true
  fi
  local tries=0
  until docker info &>/dev/null || (( ++tries >= 30 )); do sleep 2; done
  docker compose version &>/dev/null \
    || die "docker compose non disponibile: nessun servizio è stato toccato. Installalo e rilancia."
  docker buildx version &>/dev/null \
    || die "docker buildx non disponibile: nessun servizio è stato toccato. Installalo e rilancia."
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

# Container già in esecuzione accanto all'OpenHAB nativo (es. un zwavejs2mqtt
# messo a mano): restano fuori dal controller e si tengono i loro device. Una
# seriale già aperta da un container non va mappata anche in openhab (Redmine #234).
FOREIGN_CONTAINERS=()      # "nome (immagine)"
declare -A FOREIGN_DEVICES=()   # path reale sull'host -> nome del container
detect_foreign_containers() {
  command -v docker &>/dev/null && docker info &>/dev/null || return 0
  local name img dev
  while IFS=' ' read -r name img; do
    [[ -z "$name" ]] && continue
    FOREIGN_CONTAINERS+=("$name ($img)")
    while IFS= read -r dev; do
      [[ -z "$dev" ]] && continue
      FOREIGN_DEVICES[$(readlink -f "$dev" 2>/dev/null || echo "$dev")]="$name"
    done < <(docker inspect -f '{{range .HostConfig.Devices}}{{println .PathOnHost}}{{end}}' "$name" 2>/dev/null || true)
  done < <(docker ps --format '{{.Names}} {{.Image}}')
}

detect_native_serial() {
  local -A seen=(); local d
  local raw=""

  # 1) da /etc/default/openhab(2): gnu.io.rxtx.SerialPorts=/dev/a:/dev/b
  #    Solo righe attive: il file del pacchetto ha un esempio commentato con
  #    /dev/ttyS0, che su una ODROID-C4 esiste (console seriale, gruppo tty) e
  #    finiva mappata in openhab al posto del gruppo dialout (Redmine #235).
  for f in /etc/default/openhab /etc/default/openhab2; do
    [[ -f "$f" ]] || continue
    local v
    v=$(grep -hvE '^[[:space:]]*#' "$f" 2>/dev/null \
        | grep -oE 'gnu\.io\.rxtx\.SerialPorts=[^"[:space:]]*' | head -1 || true)
    v="${v#*=}"
    [[ -n "$v" ]] && raw+=$'\n'"${v//:/$'\n'}"
  done

  # 2) da conf (things/*.things, ecc.) e jsondb
  raw+=$'\n'"$(grep -rhoE '/dev/serial/by-id/[A-Za-z0-9_.:-]+' "$CONF" "$USERDATA/jsondb" 2>/dev/null || true)"
  raw+=$'\n'"$(grep -rhoE '/dev/tty(USB|ACM|AML|S)[0-9]+' "$CONF" "$USERDATA/jsondb" 2>/dev/null || true)"

  # 3) device fisicamente presenti
  for d in /dev/ttyUSB* /dev/ttyACM*; do [[ -e "$d" ]] && raw+=$'\n'"$d"; done

  # dedup preservando l'ordine, saltando i device di altri container
  local real
  while IFS= read -r d; do
    [[ -z "$d" ]] && continue
    [[ -n "${seen[$d]:-}" ]] && continue
    seen[$d]=1
    real=$(readlink -f "$d" 2>/dev/null || echo "$d")
    if [[ -n "${FOREIGN_DEVICES[$real]:-}" ]]; then
      log "  $d è usato dal container ${FOREIGN_DEVICES[$real]}: non lo mappo in openhab"
      continue
    fi
    SERIAL_DEVICES+=("$d")
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

  parse_compose_openhab_image() {
    python3 -c "
import yaml, sys
try:
    with open('$1') as f:
        cfg = yaml.safe_load(f)
    for name, svc in (cfg.get('services') or {}).items():
        if (svc.get('container_name') or name) == 'openhab' and svc.get('image'):
            print(svc['image'])
            break
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

  # Immagine OpenHAB da conservare (Redmine #190): container, poi vecchio compose.
  # Un tag mobile (latest, milestone, snapshot) o assente non dice che versione
  # giri, e al primo pull porterebbe una versione diversa da quella dell'userdata:
  # in quel caso vale la versione scritta nell'userdata.
  local OPENHAB_IMAGE_DETECTED
  OPENHAB_IMAGE_DETECTED=$(docker inspect openhab --format '{{.Config.Image}}' 2>/dev/null || echo "")
  if [[ -z "$OPENHAB_IMAGE_DETECTED" && -n "$OLD_COMPOSE_PATH" ]]; then
    OPENHAB_IMAGE_DETECTED=$(parse_compose_openhab_image "$OLD_COMPOSE_PATH")
  fi
  local oh_tag=""
  [[ "$OPENHAB_IMAGE_DETECTED" == *:* ]] && oh_tag="${OPENHAB_IMAGE_DETECTED##*:}"
  if [[ ! "$oh_tag" =~ ^[0-9]+\.[0-9]+ ]]; then
    local oh_ver; oh_ver=$(openhab_userdata_version)
    if [[ -n "$oh_ver" ]]; then
      [[ -n "$OPENHAB_IMAGE_DETECTED" ]] && echo "Tag mobile ($OPENHAB_IMAGE_DETECTED): uso la versione dell'userdata, $oh_ver"
      OPENHAB_IMAGE_DETECTED="openhab/openhab:$oh_ver"
    fi
  fi
  if [[ -n "$OPENHAB_IMAGE_DETECTED" ]]; then
    echo "Immagine OpenHAB conservata: $OPENHAB_IMAGE_DETECTED"
  else
    warn "versione di OpenHAB non rilevata: resterà l'immagine del template e OpenHAB aggiornerà l'userdata al primo avvio."
  fi
  echo ""

  check_disk_space docker

  echo "OPERAZIONI CHE VERRANNO ESEGUITE:"
  echo "  1) Backup completo di /opt/docker_store/"
  echo "  2) Stop dei container attivi (i dati NON vengono toccati)"
  echo "  3) Estrazione tarball arfea-controller"
  echo "  4) Configurazione arfea.yml con servizi rilevati + API key generata"
  echo "  5) Avvio del nuovo stack"
  echo ""
  read -r -p "Procedere? (s/n): " confirm
  [[ "$confirm" =~ ^[SsYy] ]] || { echo "Annullato."; exit 0; }

  ensure_compose_buildx

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

  if [[ -n "$OPENHAB_IMAGE_DETECTED" ]]; then
    if set_openhab_image "$YML" "$OPENHAB_IMAGE_DETECTED"; then
      log "Immagine openhab conservata in arfea.yml: $OPENHAB_IMAGE_DETECTED (l'upgrade si fa dopo, dalla Web UI)"
    else
      warn "non sono riuscito a scrivere l'immagine openhab in arfea.yml: controlla il blocco openhab."
    fi
  fi
  log "arfea.yml configurato (API key: $ARFEA_API_KEY)"

  # 5. Avvia stack
  echo ""
  log "[5/5] Build e avvio arfea-controller..."
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

  # Jython: da OpenHAB 4.2 le librerie si cercano in automation/jython/lib e non
  # più in automation/lib/python, dove stanno le helper library di OpenHAB 3:
  # senza spostarle ogni "from core.rules import rule" fallisce. Gli script in
  # automation/jsr223 invece si caricano ancora (percorso deprecato).
  if [[ -d "$DEST/conf/automation/lib/python" && ! -e "$DEST/conf/automation/jython/lib" ]]; then
    log "  jython:   automation/lib/python -> automation/jython/lib (percorso di OpenHAB 4.2+)"
    mkdir -p "$DEST/conf/automation/jython"
    cp -a "$DEST/conf/automation/lib/python" "$DEST/conf/automation/jython/lib"
  fi
  # ...e gli script: il 5.x li carica SOLO da automation/jython. In
  # automation/jsr223/python restavano lì senza un errore nel log, e con loro le
  # regole di allagamento, allarmi e gas di un impianto (Redmine #237).
  if [[ -d "$DEST/conf/automation/jsr223/python" ]]; then
    local py rel
    while IFS= read -r py; do
      rel="${py#"$DEST/conf/automation/jsr223/python/"}"
      mkdir -p "$(dirname "$DEST/conf/automation/jython/$rel")"
      mv "$py" "$DEST/conf/automation/jython/$rel"
      log "  jython:   script jsr223/python/$rel -> automation/jython/$rel"
    done < <(find "$DEST/conf/automation/jsr223/python" -type f -name '*.py')
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

  # userdata/logs deve esistere PRIMA del primo avvio. L'entrypoint openhab lo
  # crea (copiando dist/userdata) solo se la userdata e' vuota, e dopo la
  # migrazione non lo e' mai. Con userdata a versione diversa dall'immagine
  # parte l'upgrade, che come prima cosa fa "... | tee $OPENHAB_LOGDIR/update.log":
  # senza la dir tee fallisce e, con "set -eu -o pipefail" attivo nell'entrypoint,
  # il container muore prima di avviare openhab.
  mkdir -p "$DEST/userdata/logs"

  # Log di crash JVM lasciati dall'installazione nativa: inutili e finirebbero
  # nel tar di backup che l'upgrade crea in userdata/backup.
  rm -f "$DEST/userdata"/hs_err_pid*.log

  # addons manuali (kar/jar). NON il pacchetto della distribuzione
  # (openhab-addons-X.Y.Z.kar del pacchetto deb openhab-addons, ~360 MB): è
  # della versione nativa, e il container con un OpenHAB più nuovo se lo
  # troverebbe accanto al suo. Quello giusto lo scarica il controller
  # (app/addons.py) per la versione in uso (Redmine #233).
  if [[ -d "$ADDONS" ]] && [[ -n "$(ls -A "$ADDONS" 2>/dev/null)" ]]; then
    log "  addons:   $ADDONS -> $DEST/addons (escluso il pacchetto openhab-addons-*.kar)"
    if $have_rsync; then rsync -a --exclude 'openhab-addons-*.kar' "$ADDONS"/ "$DEST/addons"/
    else
      cp -a "$ADDONS"/. "$DEST/addons"/
      rm -f "$DEST/addons"/openhab-addons-*.kar
    fi
  fi

  # HABApp: config nativa -> $DEST/conf/habapp
  if $NAT_HABAPP; then
    local hcfg; hcfg=$(detect_habapp_config)
    if [[ -n "$hcfg" && -d "$hcfg" ]]; then
      log "  habapp:   $hcfg -> $DEST/conf/habapp"
      mkdir -p "$DEST/conf/habapp"
      if $have_rsync; then rsync -a "$hcfg"/ "$DEST/conf/habapp"/
      else cp -a "$hcfg"/. "$DEST/conf/habapp"/; fi
      # Log con percorsi dell'host (/var/log/openhab/HABApp.log): nel container
      # non esistono e HABApp esce subito, in loop (Redmine #236). Un nome
      # relativo finisce in conf/habapp/log.
      if [[ -f "$DEST/conf/habapp/logging.yml" ]] \
         && grep -qE "^[[:space:]]*filename:[[:space:]]*['\"]?/" "$DEST/conf/habapp/logging.yml"; then
        sed -i -E "s#^([[:space:]]*filename:[[:space:]]*['\"]?)/[^'\"[:space:]]*/#\1#" "$DEST/conf/habapp/logging.yml"
        log "  habapp:   logging.yml, file di log relativi (in conf/habapp/log)"
      fi
      # Regole di sistema del vecchio HABApp ARFEA: nel mondo controller le fanno
      # arfea_system.js + arfea.items e aasystem/tools.py, e tenerle vuol dire
      # averle in doppio (Redmine #237). Messe da parte, non cancellate.
      local old oldbak="$DATA_PATH/arfea-controller/backups/habapp-regole-native-$(date +%Y%m%d_%H%M%S)"
      for old in rules/system/arfea.py rules/system/time.py rules/tools/tools.py; do
        [[ -f "$DEST/conf/habapp/$old" ]] || continue
        mkdir -p "$(dirname "$oldbak/$old")"
        mv "$DEST/conf/habapp/$old" "$oldbak/$old"
        log "  habapp:   $old messa da parte in $oldbak (la sostituisce il controller)"
      done
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

# ── Ritocchi alla COPIA dei dati nativi, prima del primo avvio (Redmine #237) ──
# Emersi migrando un impianto da OpenHAB 3.3: senza, l'impianto parte ma con
# pezzi fermi. Toccano solo /opt/docker_store/openhab: l'originale nativo resta.

# Item che il vecchio HABApp ARFEA creava via REST (users_list, send_message,
# send_broadcastmessage, timeSlot, ...) e che ora definisce arfea.items: con
# entrambi, a ogni avvio OpenHAB scarta quelli managed con un warning.
dedupe_managed_items() {
  local items_file="$DEST/conf/items/arfea.items"
  local db="$DEST/userdata/jsondb/org.openhab.core.items.Item.json"
  [[ -f "$items_file" && -f "$db" ]] || return 0
  python3 - "$items_file" "$db" <<'PY' || warn "item doppioni non tolti dal JSONDB: restano, con un warning a ogni avvio"
import json, re, sys
items_file, db = sys.argv[1:3]
names = set(re.findall(r'^[ \t]*[A-Z][A-Za-z]*(?::\S+)?[ \t]+([A-Za-z_][A-Za-z0-9_]*)',
                       open(items_file).read(), re.M))
data = json.load(open(db))
dup = sorted(k for k in data if k in names)
if dup:
    for k in dup:
        del data[k]
    with open(db, "w") as f:          # stesso inode: owner 9001 conservato
        json.dump(data, f, indent=2)
    print("  item:     tolti dal JSONDB i doppioni di arfea.items: " + " ".join(dup))
PY
}

# JS Scripting: senza, arfea_controller.js e arfea_system.js (widget, fasce
# orarie, festivi) non girano, e nemmeno le trasformazioni JS. Un OpenHAB 3.x
# nativo non ce l'ha: la sua trasformazione "javascript" (Nashorn) sparisce col
# 4.0 e nessuno installa il sostituto.
ensure_jsscripting_addon() {
  local cfg="$DEST/userdata/config/org/openhab/addons.config"
  local cfgfile="$DEST/conf/services/addons.cfg"
  # addons.cfg vince sulla config della UI per le chiavi che definisce
  if [[ -f "$cfgfile" ]] && grep -qE '^[[:space:]]*automation[[:space:]]*=' "$cfgfile"; then
    if ! grep -qE '^[[:space:]]*automation[[:space:]]*=.*jsscripting' "$cfgfile"; then
      sed -i -E 's/^([[:space:]]*automation[[:space:]]*=[[:space:]]*)(.*[^[:space:]])[[:space:]]*$/\1\2,jsscripting/' "$cfgfile"
      log "  addon:    JS Scripting aggiunto in services/addons.cfg"
    fi
    return 0
  fi
  [[ -f "$cfg" ]] || return 0
  grep -qE '^automation="[^"]*jsscripting' "$cfg" && return 0
  if grep -qE '^automation="' "$cfg"; then
    sed -i -E 's/^automation="([^"]*)"/automation="\1,jsscripting"/; s/^automation=",/automation="/' "$cfg"
  else
    echo 'automation="jsscripting"' >> "$cfg"
  fi
  log "  addon:    JS Scripting aggiunto agli addon (serve alle regole ARFEA)"
}

# OpenHAB 5.1 non ha più le strategie di default: "default = ..." in Strategies
# rende il .persist illeggibile e la persistenza si ferma. Si toglie solo se ogni
# voce in Items dichiara già le sue strategie; altrimenti lo si segnala.
PERSIST_TODO=()
fix_persist_default() {
  local f
  for f in "$DEST"/conf/persistence/*.persist; do
    [[ -f "$f" ]] || continue
    grep -qE '^[[:space:]]*default[[:space:]]*=' "$f" || continue
    if python3 - "$f" <<'PY'
import re, sys
text = re.sub(r'/\*.*?\*/|//[^\n]*', '', open(sys.argv[1]).read(), flags=re.S)
m = re.search(r'\bItems\s*\{(.*?)\}', text, re.S)
entries = [l.strip() for l in (m.group(1) if m else '').splitlines() if l.strip()]
sys.exit(0 if all('strategy' in e for e in entries) else 1)
PY
    then
      sed -i -E '/^[[:space:]]*default[[:space:]]*=/d' "$f"
      log "  persist:  tolta la strategia default da $(basename "$f") (OpenHAB 5.1+)"
    else
      PERSIST_TODO+=("$(basename "$f")")
    fi
  done
}

# Cosa resta da guardare a mano dopo il salto di versione, da stampare alla fine.
UPGRADE_NOTES=()
collect_upgrade_notes() {
  local n line
  while IFS= read -r line; do
    [[ -n "$line" ]] && UPGRADE_NOTES+=("$line")
  done < <(python3 - "$DEST/userdata/jsondb" <<'PY' 2>/dev/null || true
import json, os, sys
jdb = sys.argv[1]
def load(name):
    p = os.path.join(jdb, name)
    return json.load(open(p)) if os.path.isfile(p) else {}
js = [r["value"].get("name") or uid for uid, r in load("automation_rules.json").items()
      if any((a.get("configuration") or {}).get("type") == "application/javascript"
             for a in r["value"].get("actions", []))]
if js:
    print(f"regole UI in JavaScript ({', '.join(js)}): dal 4.0 girano su GraalJS e non più "
          "su Nashorn (enum Java confrontati con stringhe, Java.type, ...): provale")
ha = [uid for uid in load("org.openhab.core.thing.Thing.json") if uid.startswith("mqtt:homeassistant_")]
if ha:
    print(f"{len(ha)} thing MQTT Home Assistant creati prima del 4.3: nel 5.x cambiano gli ID "
          "dei canali. Ricreali dall'inbox (homeassistant:device:...) e ricollega gli item")
PY
)
  for n in "${PERSIST_TODO[@]}"; do
    UPGRADE_NOTES+=("$n: togli 'default = ...' da Strategies e dai una strategia a ogni voce (OpenHAB 5.1+)")
  done
  UPGRADE_NOTES+=("se nel log di OpenHAB compare 'Graal JavaScript language not initialized', riavvia il container openhab")
}

# NB: le config di default dei servizi (mosquitto.conf, settings.json di
# zwave-js-ui, configuration.yaml di zigbee2mqtt) NON si installano qui.
# Le crea il controller alla creazione del container (_ensure_default_config in
# app/docker_manager.py), che copre anche i servizi accesi dalla Web UI dopo la
# migrazione — cosa che questo script, per definizione, non puo' fare.
# Non reintrodurle: erano duplicate qui e in install.sh e sono andate alla deriva
# (install.sh creava mosquitto.conf, questo script no → broker senza config).

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
  detect_foreign_containers
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
  if [[ ${#FOREIGN_CONTAINERS[@]} -gt 0 ]]; then
    echo "Container già presenti, che restano FUORI dal controller (non vengono toccati):"
    for d in "${FOREIGN_CONTAINERS[@]}"; do echo "  - $d"; done
    echo "  Tengono le loro porte e seriali: prima di abilitare dalla Web UI un servizio"
    echo "  equivalente (zwave-js-ui, zigbee2mqtt, node-red) vanno migrati o fermati a mano."
    echo ""
  fi

  # Avviso versione: salto di major (2.x -> 5.x) può richiedere interventi manuali
  local major="${OH_VERSION%%.*}"
  if [[ -n "$major" && "$major" =~ ^[0-9]+$ && "$major" -lt 4 ]]; then
    warn "OpenHAB nativo major=$major: il salto diretto all'immagine 5.x può"
    echo "         richiedere una revisione manuale di things/binding (soprattutto da 2.x)."
    echo "         I dati vengono comunque copiati; verifica il funzionamento dopo l'avvio."
  fi

  check_disk_space native

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
  ensure_compose_buildx

  echo ""; log "[2/7] Backup..."
  backup_docker_store

  echo ""; log "[3/7] Stop servizi nativi..."
  stop_native_services

  echo ""; log "[4/7] Copia dati OpenHAB nativi in $DEST..."
  copy_native_data

  echo ""; log "[5/7] Estrazione tarball + configurazione arfea.yml..."
  extract_tarball
  deploy_arfea_skeleton
  dedupe_managed_items
  ensure_jsscripting_addon
  fix_persist_default
  collect_upgrade_notes
  configure_yml_native

  echo ""; log "[6/7] Build e avvio arfea-controller..."
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
  if [[ ${#UPGRADE_NOTES[@]} -gt 0 ]]; then
    echo ""
    echo "  DA VERIFICARE A MANO (salto di versione di OpenHAB):"
    for n in "${UPGRADE_NOTES[@]}"; do echo "    - $n"; done
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
