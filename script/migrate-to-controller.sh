#!/bin/bash

###############################################################################
# migrate-to-controller.sh
#
# Migra una centralina esistente con docker-compose-arfea-2.yml
# verso la struttura arfea-controller.
#
# Cosa fa:
# 1. Backup completo di /opt/docker_store (tar.gz)
# 2. Rileva container attivi e device paths
# 3. Stop dei container vecchi (volumi preservati)
# 4. Estrazione tarball arfea-controller
# 5. Configurazione automatica arfea.yml in base a cosa era attivo
# 6. Avvio nuovo stack (controller + servizi)
#
# Cosa NON tocca:
# - I dati in /opt/docker_store/{openhab,mosquitto,zwave-js-ui,node-red,...}
# - Le configurazioni interne dei servizi
# - L'utente admin di OpenHAB
#
# Uso:
#   sudo bash migrate-to-controller.sh
#   sudo bash migrate-to-controller.sh /path/old-compose.yml /path/tarball.tar.xz
###############################################################################

set -e

# ── Parametri ──────────────────────────────────────────────────────────────
OLD_COMPOSE_PATH="${1:-}"
TARBALL_PATH="${2:-$(dirname "$(readlink -f "$0")")/arfea-controller.tar.xz}"

timestamp() { date +"%F_%T_%Z"; }

# ── Pre-check ──────────────────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
  echo "ERRORE: esegui come root (sudo)"
  exit 1
fi

if ! command -v docker &>/dev/null; then
  echo "ERRORE: docker non installato"
  exit 1
fi

if ! docker info &>/dev/null; then
  echo "ERRORE: Docker non attivo. systemctl start docker.service"
  exit 1
fi

if [[ ! -f "$TARBALL_PATH" ]]; then
  echo "ERRORE: tarball non trovato in $TARBALL_PATH"
  echo "Usa: sudo bash $0 [OLD_COMPOSE] [TARBALL_PATH]"
  exit 1
fi

# ── Rileva il vecchio compose ──────────────────────────────────────────────
if [[ -z "$OLD_COMPOSE_PATH" ]]; then
  # Cerca docker-compose-arfea-2.yml in posti tipici
  for candidate in \
    /opt/docker_store/docker-compose-arfea-2.yml \
    /opt/docker_store/docker-compose.yml \
    /opt/docker-compose-arfea-2.yml \
    /home/openhab/docker-compose-arfea-2.yml \
    /root/docker-compose-arfea-2.yml
  do
    if [[ -f "$candidate" ]]; then
      OLD_COMPOSE_PATH="$candidate"
      break
    fi
  done
fi

if [[ -z "$OLD_COMPOSE_PATH" || ! -f "$OLD_COMPOSE_PATH" ]]; then
  echo "ATTENZIONE: vecchio docker-compose non trovato."
  read -r -p "Procedere comunque (solo stop container + setup controller)? (s/n): " yn
  [[ "$yn" =~ ^[SsYy] ]] || exit 0
  OLD_COMPOSE_PATH=""
fi

# ── Rileva container attivi ────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════════"
echo "   MIGRAZIONE verso arfea-controller"
echo "════════════════════════════════════════════════════════════"
echo ""
echo "Vecchio compose: ${OLD_COMPOSE_PATH:-(non trovato)}"
echo "Tarball:         $TARBALL_PATH"
echo ""

MANAGED_NAMES="openhab habapp zwave-js-ui zigbee2mqtt node-red mosquitto samba docker-socket-proxy"
ACTIVE=""
for name in $MANAGED_NAMES; do
  if docker ps --format '{{.Names}}' | grep -qx "$name"; then
    ACTIVE="$ACTIVE $name"
  fi
done

# ── Helpers di rilevamento (da container in esecuzione OPPURE dal YAML) ───
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
        cn = svc.get('container_name') or name
        print(cn)
except Exception as e:
    sys.stderr.write(str(e))
"
}

parse_compose_devices() {
  # Stampa "container_name|dev_host:dev_container" per ogni device
  python3 -c "
import yaml, sys
try:
    with open('$1') as f:
        cfg = yaml.safe_load(f)
    for name, svc in (cfg.get('services') or {}).items():
        cn = svc.get('container_name') or name
        for d in (svc.get('devices') or []):
            if isinstance(d, str):
                # toglie :rwm finale se presente
                parts = d.split(':')
                if len(parts) >= 2:
                    print(f\"{cn}|{parts[0]}:{parts[1]}\")
except Exception as e:
    sys.stderr.write(str(e))
"
}

# Se nessun container attivo ma c'e' il compose, prendi i nomi da li'
if [[ -z "$ACTIVE" && -n "$OLD_COMPOSE_PATH" ]]; then
  echo "Nessun container attivo. Analizzo il vecchio compose..."
  for cn in $(parse_compose_services "$OLD_COMPOSE_PATH"); do
    if echo "$MANAGED_NAMES" | tr ' ' '\n' | grep -qx "$cn"; then
      ACTIVE="$ACTIVE $cn"
    fi
  done
fi

echo "Container rilevati:$ACTIVE"
echo ""

# ── Rileva device paths ───────────────────────────────────────────────────
ZWAVE_DEVICE=""
ZIGBEE_DEVICE=""
OPENHAB_DEVICES=()

# Prima prova: container in esecuzione
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

# Fallback: leggi dal vecchio compose YAML
if [[ -n "$OLD_COMPOSE_PATH" ]]; then
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    cn="${line%%|*}"
    dev="${line##*|}"
    case "$cn" in
      zwave-js-ui|zwave) [[ -z "$ZWAVE_DEVICE" ]] && ZWAVE_DEVICE="${dev%%:*}" ;;
      zigbee2mqtt) [[ -z "$ZIGBEE_DEVICE" ]] && ZIGBEE_DEVICE="${dev%%:*}" ;;
      openhab)
        if ! printf '%s\n' "${OPENHAB_DEVICES[@]}" | grep -q "^${dev}:rwm$"; then
          OPENHAB_DEVICES+=("${dev}:rwm")
        fi
        ;;
    esac
  done < <(parse_compose_devices "$OLD_COMPOSE_PATH")
fi

echo "Device paths rilevati:"
echo "  Z-Wave:   ${ZWAVE_DEVICE:-(nessuno)}"
echo "  Zigbee:   ${ZIGBEE_DEVICE:-(nessuno)}"
echo "  OpenHAB:  ${OPENHAB_DEVICES[*]:-(nessuno)}"
echo ""

# ── Rileva immagine OpenHAB corrente ──────────────────────────────────────
OPENHAB_IMAGE_DETECTED=$(docker inspect openhab --format '{{.Config.Image}}' 2>/dev/null || echo "")
if [[ -n "$OPENHAB_IMAGE_DETECTED" ]]; then
  echo "Immagine OpenHAB corrente: $OPENHAB_IMAGE_DETECTED"
fi
echo ""

# ── Conferma utente ────────────────────────────────────────────────────────
echo "OPERAZIONI CHE VERRANNO ESEGUITE:"
echo "  1) Backup completo di /opt/docker_store/ in /opt/docker_store-backup-*.tar.gz"
echo "  2) Stop dei container attivi (i dati NON vengono toccati)"
echo "  3) Estrazione tarball arfea-controller in /opt/docker_store/arfea-controller/"
echo "  4) Configurazione arfea.yml con servizi rilevati + API key generata"
echo "  5) Avvio del nuovo stack"
echo ""
read -r -p "Procedere? (s/n): " confirm
[[ "$confirm" =~ ^[SsYy] ]] || { echo "Annullato."; exit 0; }

# ═════════════════════════════════════════════════════════════════════════════
# EXECUTION
# ═════════════════════════════════════════════════════════════════════════════

# 1. Backup
BACKUP_FILE="/opt/docker_store-backup-$(date +%Y%m%d_%H%M%S).tar.gz"
echo ""
echo "$(timestamp) [1/5] Backup in $BACKUP_FILE (può richiedere alcuni minuti)..."
tar --warning=no-file-changed -czf "$BACKUP_FILE" -C /opt docker_store 2>/dev/null || true
if [[ -f "$BACKUP_FILE" ]]; then
  size=$(du -h "$BACKUP_FILE" | cut -f1)
  echo "$(timestamp) Backup completato ($size)"
else
  echo "ERRORE: backup fallito"
  exit 1
fi

# 2. Stop container vecchi
echo ""
echo "$(timestamp) [2/5] Stop container vecchi..."
if [[ -n "$OLD_COMPOSE_PATH" ]]; then
  cd "$(dirname "$OLD_COMPOSE_PATH")"
  docker compose -f "$OLD_COMPOSE_PATH" down 2>/dev/null || true
  cd - >/dev/null
fi

# Stop e remove manuale (fallback / pulizia)
for c in $ACTIVE; do
  docker stop "$c" 2>/dev/null || true
  docker rm -f "$c" 2>/dev/null || true
done
echo "$(timestamp) Container vecchi rimossi"

# 3. Estrai tarball
echo ""
echo "$(timestamp) [3/5] Estrazione tarball arfea-controller..."
mkdir -p /opt/docker_store/arfea-controller/{config,backups}
tar -xJf "$TARBALL_PATH" --strip-components=1 -C /opt/docker_store/arfea-controller/
echo "$(timestamp) Tarball estratto"

# 4. Configura arfea.yml
echo ""
echo "$(timestamp) [4/5] Configurazione arfea.yml..."
YML="/opt/docker_store/arfea-controller/config/arfea.yml"

if [[ ! -f "$YML" ]]; then
  echo "ERRORE: arfea.yml non trovato dopo l'estrazione del tarball!"
  echo "Contenuto del tarball:"
  tar -tJf "$TARBALL_PATH" | head -20
  exit 1
fi

# Genera API key
ARFEA_API_KEY=$(openssl rand -hex 16)
sed -i "s|CAMBIARE-CON-CHIAVE-UNICA|${ARFEA_API_KEY}|" "$YML"

# Disabilita self-update automatico al boot — riduce il rischio che il
# controller scarichi un tarball remoto e applichi un update non desiderato
# subito dopo la migrazione. L'utente puo' riabilitarlo manualmente in arfea.yml.
sed -i 's|^  update_url:.*|  update_url: ""|' "$YML"

# Helper per abilitare un servizio
enable_in_yml() {
  awk -v svc="  ${1}:" '
    $0 == svc { in_svc=1 }
    in_svc && /enabled:/ { sub(/false/, "true"); in_svc=0 }
    { print }
  ' "$YML" > "${YML}.tmp" && mv "${YML}.tmp" "$YML"
}

# Abilita servizi che erano attivi
for name in $ACTIVE; do
  case "$name" in
    docker-socket-proxy) ;;  # non gestito dal controller
    habapp|node-red|samba) enable_in_yml "$name" ;;
    zwave-js-ui) enable_in_yml "zwave-js-ui" ;;
    zigbee2mqtt) enable_in_yml "zigbee2mqtt" ;;
    mosquitto) enable_in_yml "mosquitto" ;;
  esac
done

# Sostituisci device path Z-Wave
if [[ -n "$ZWAVE_DEVICE" ]]; then
  sed -i "s|/dev/ttyACM0:/dev/zwave|${ZWAVE_DEVICE}:/dev/zwave|" "$YML"
fi

# Sostituisci device path Zigbee
if [[ -n "$ZIGBEE_DEVICE" ]]; then
  sed -i "s|/dev/serial/by-id/usb-ITEAD_SONOFF_Zigbee_3.0_USB_Dongle_Plus_V2_20231031184237-if00:/dev/zigbee|${ZIGBEE_DEVICE}:/dev/zigbee|" "$YML"
fi

# Aggiungi devices block a openhab se ce ne sono
if [[ ${#OPENHAB_DEVICES[@]} -gt 0 ]]; then
  devices_block="    devices:"
  for dev in "${OPENHAB_DEVICES[@]}"; do
    devices_block="${devices_block}\n      - \"${dev}\""
  done
  awk -v devs="$devices_block" '
    /network_mode: host/ && !done { print; printf "%s\n", devs; done=1; next }
    { print }
  ' "$YML" > "${YML}.tmp" && mv "${YML}.tmp" "$YML"
fi

# Usa la stessa immagine OpenHAB di prima se rilevata
if [[ -n "$OPENHAB_IMAGE_DETECTED" && "$OPENHAB_IMAGE_DETECTED" != "openhab/openhab:5.1.3" ]]; then
  echo "$(timestamp) Aggiorno image openhab in arfea.yml: $OPENHAB_IMAGE_DETECTED"
  sed -i "s|image: \"openhab/openhab:5.1.3\"|image: \"${OPENHAB_IMAGE_DETECTED}\"|" "$YML"
fi

echo "$(timestamp) arfea.yml configurato (API key: $ARFEA_API_KEY)"

# 5. Avvia stack
echo ""
echo "$(timestamp) [5/5] Build e avvio arfea-controller..."

# Verifica buildx (necessario per multi-arch su ARM)
if ! docker buildx version &>/dev/null; then
  echo "$(timestamp) Installazione docker-buildx-plugin..."
  apt-get update -qq
  apt-get install -y -qq docker-buildx-plugin || \
    echo "$(timestamp) ATTENZIONE: buildx non installato, build potrebbe fallire"
fi

cd /opt/docker_store/arfea-controller
docker compose build
docker compose up -d

echo "$(timestamp) Attendo che openhab parta..."
tries=0
while [[ "$(docker inspect -f '{{.State.Running}}' openhab 2>/dev/null)" != "true" ]]; do
  tries=$((tries+1))
  if [[ $tries -ge 60 ]]; then
    echo "$(timestamp) ATTENZIONE: openhab non partito dopo 2 minuti."
    break
  fi
  sleep 2
done

# ── Salva credenziali in /root/ ────────────────────────────────────────────
CRED_FILE="/root/arfea-credentials.txt"
cat > "$CRED_FILE" <<EOF
ARFEA - migrazione completata il $(timestamp)

API Key arfea-controller:
  $ARFEA_API_KEY

Backup pre-migrazione:
  $BACKUP_FILE

EOF
chmod 600 "$CRED_FILE"

# ── Output finale ──────────────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "   MIGRAZIONE COMPLETATA"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "  API Key arfea-controller:  $ARFEA_API_KEY"
echo "  Backup pre-migrazione:     $BACKUP_FILE"
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
echo "  In caso di problemi, ripristina con:"
echo "    cd /opt/docker_store/arfea-controller && docker compose down"
echo "    sudo rm -rf /opt/docker_store"
echo "    sudo tar -xzf $BACKUP_FILE -C /opt"
echo "    cd <dir-vecchio-compose> && docker compose -f $OLD_COMPOSE_PATH up -d"
echo ""
