#!/bin/bash
###############################################################################
# arfea-network-nm.sh — passa la rete della centralina a NetworkManager
#
# Il controller gestisce LAN (DHCP/statico), wifi e access point di emergenza
# tramite NetworkManager. Armbian e Ubuntu Server nascono invece con netplan +
# systemd-networkd, che non sa né scansionare le reti wifi né fare da access
# point: questo script fa il passaggio, una volta sola per centralina.
#
# USO:
#   sudo ./script/arfea-network-nm.sh              # applica SUBITO, con rollback automatico
#   sudo ./script/arfea-network-nm.sh --boot       # prepara, diventa attivo al prossimo riavvio
#   sudo ./script/arfea-network-nm.sh --rollback   # torna a netplan/systemd-networkd
#
# Cosa fa:
#   1. installa network-manager, dnsmasq-base (DHCP dell'AP) e iw
#   2. scrive /etc/NetworkManager/conf.d/90-arfea.conf: NM non tocca le
#      interfacce di Docker e della VPN, e non fa NAT per l'AP
#   3. crea il profilo "arfea-lan" per la scheda cablata RIPRENDENDO la
#      configurazione in uso: stesso MAC e stesso client-id DHCP (il router
#      riassegna lo stesso IP), oppure lo stesso IP statico con gateway e DNS
#   4. salva i file netplan in /var/backups/arfea-network/<data>/, li toglie da
#      /etc/netplan e disattiva systemd-networkd
#   5. (modalità immediata) attiva NM sulla LAN e verifica che il gateway
#      risponda: se entro 90 s non risponde ripristina tutto da solo
#
# Il passaggio vero gira in un'unità systemd transitoria: se la sessione SSH
# cade mentre la rete si riconfigura, il lavoro (e l'eventuale rollback)
# prosegue lo stesso. Log in /var/log/arfea-network.log.
# Idempotente: se la LAN è già gestita da NetworkManager non fa nulla.
###############################################################################

set -euo pipefail

BACKUP_ROOT=/var/backups/arfea-network
NM_CONF=/etc/NetworkManager/conf.d/90-arfea.conf
LAN_CON=arfea-lan
LOG=/var/log/arfea-network.log
UNIT=arfea-network-migrate
VERIFY_SECONDS=90

log() { echo "$(date +%F_%T) [arfea-network] $*" | tee -a "$LOG"; }
die() { log "ERRORE: $*"; exit 1; }

usage() {
  sed -n '/^# USO:/,/^#$/p' "$0" | sed 's/^# \{0,1\}//'
  exit 1
}

[[ $EUID -eq 0 ]] || { echo "Va eseguito come root (sudo)" >&2; exit 1; }

MODE=now
case "${1:-}" in
  "")          MODE=now ;;
  --boot)      MODE=boot ;;
  --rollback)  MODE=rollback ;;
  --apply)     MODE=apply ;;     # interno: il passaggio vero, dentro l'unità transitoria
  --restore)   MODE=restore ;;   # interno: il ripristino, dentro l'unità transitoria
  *)           usage ;;
esac

# ── Scheda cablata ───────────────────────────────────────────────────────────
# Quella della route di default se è cablata, altrimenti la prima scheda fisica
# non wifi. Mai un'interfaccia virtuale (Docker, VPN, bridge).
detect_lan_if() {
  local dev
  dev=$(ip -4 route show default 2>/dev/null | awk '{for(i=1;i<NF;i++) if($i=="dev"){print $(i+1); exit}}')
  if [[ -n "$dev" && -e "/sys/class/net/$dev/device" && ! -e "/sys/class/net/$dev/wireless" ]]; then
    echo "$dev"; return
  fi
  for p in /sys/class/net/*; do
    dev=$(basename "$p")
    [[ -e "$p/device" && ! -e "$p/wireless" && "$(cat "$p/type")" == 1 ]] || continue
    echo "$dev"; return
  done
  return 0
}

# ── Config NetworkManager ────────────────────────────────────────────────────
# $1 = true: la LAN resta ancora a systemd-networkd (fase transitoria/rollback)
write_nm_conf() {
  local keep_lan_out=$1 extra=""
  $keep_lan_out && extra="interface-name:${LAN_IF};"
  mkdir -p "$(dirname "$NM_CONF")"
  cat > "$NM_CONF" <<EOF
# ARFEA — generato da arfea-network-nm.sh, non modificare a mano.
[main]
# Niente profili "Wired connection N" automatici: la LAN e' il profilo arfea-lan.
no-auto-default=*
# L'AP di emergenza serve solo a raggiungere la centralina: nessun NAT verso
# LAN/internet (e comunque la FORWARD di Docker e' DROP).
firewall-backend=none

[keyfile]
# Interfacce che non sono di NetworkManager: Docker e VPN.
unmanaged-devices=${extra}interface-name:docker*;interface-name:br-*;interface-name:veth*;interface-name:tun*;interface-name:wg*
EOF
}

# Restart e non `nmcli general reload conf`: il reload non applica tutte le
# chiavi di [main] (firewall-backend restava quello vecchio fino al reboot, e
# l'AP provava a mettere regole NAT). Le connessioni attive NM le riprende.
nm_reload_conf() {
  systemctl restart NetworkManager
  nm-online -s -q -t 30 2>/dev/null || true
  sleep 2
}

lan_managed_by_nm() {
  command -v nmcli >/dev/null || return 1
  systemctl is-active --quiet NetworkManager || return 1
  local state
  state=$(nmcli -g GENERAL.STATE device show "$LAN_IF" 2>/dev/null || true)
  [[ "$state" != *unmanaged* && -n "$state" ]] && nmcli -g NAME connection show | grep -qx "$LAN_CON"
}

# ── Fotografia della LAN in uso ──────────────────────────────────────────────
capture_lan() {
  LAN_MAC=$(cat "/sys/class/net/$LAN_IF/address")
  local idx lease
  idx=$(cat "/sys/class/net/$LAN_IF/ifindex")
  lease="/run/systemd/netif/leases/$idx"
  LAN_METHOD=""
  LAN_CID=""
  if [[ -f "$lease" ]]; then
    LAN_METHOD=dhcp
    # client-id usato da networkd (DUID+IAID): con lo stesso il router ridà lo stesso IP
    LAN_CID=$(sed -n 's/^CLIENTID=//p' "$lease" | sed 's/../&:/g; s/:$//')
  elif ip -4 -o addr show dev "$LAN_IF" | grep -q dynamic; then
    LAN_METHOD=dhcp
  fi
  LAN_ADDRS=$(ip -4 -o addr show dev "$LAN_IF" scope global | awk '{print $4}' | paste -sd, -)
  LAN_GW=$(ip -4 route show default dev "$LAN_IF" 2>/dev/null | awk '{print $3; exit}')
  LAN_DNS=$(resolvectl dns "$LAN_IF" 2>/dev/null | sed 's/^[^:]*: *//' | tr ' ' '\n' \
            | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' | paste -sd, - || true)
  if [[ -z "$LAN_METHOD" ]]; then
    [[ -n "$LAN_ADDRS" ]] || die "$LAN_IF non ha indirizzi IPv4: non so che configurazione riprendere"
    LAN_METHOD=static
  fi
}

create_lan_profile() {
  nmcli -g NAME connection show | grep -qx "$LAN_CON" && nmcli connection delete "$LAN_CON" >/dev/null
  local args=(
    type ethernet con-name "$LAN_CON" ifname "$LAN_IF" autoconnect yes
    connection.autoconnect-priority 100
    ethernet.cloned-mac-address "$LAN_MAC"
    # La LAN vince sempre sul wifi: metrica piu' bassa per route e DNS.
    ipv4.route-metric 100 ipv6.route-metric 100
    ipv4.dns-priority 50 ipv6.dns-priority 50
    # IPv6 come prima (serve a Thread/Matter): link-local EUI-64 invariato.
    ipv6.method auto ipv6.addr-gen-mode eui64 ipv6.ip6-privacy 2
  )
  if [[ "$LAN_METHOD" == dhcp ]]; then
    args+=(ipv4.method auto)
    [[ -n "$LAN_CID" ]] && args+=(ipv4.dhcp-client-id "$LAN_CID")
  else
    args+=(ipv4.method manual ipv4.addresses "$LAN_ADDRS")
    [[ -n "$LAN_GW" ]] && args+=(ipv4.gateway "$LAN_GW")
    [[ -n "$LAN_DNS" ]] && args+=(ipv4.dns "$LAN_DNS")
  fi
  nmcli connection add "${args[@]}" >/dev/null
}

# ── Passaggio (dentro l'unità transitoria) ──────────────────────────────────
apply_switch() {
  local bk=$1
  log "Passaggio della LAN ($LAN_IF) a NetworkManager..."
  # Da qui la LAN e' senza gestore per qualche secondo: networkd lascia gli
  # indirizzi dove sono, NM li riprende col profilo arfea-lan.
  rm -f /etc/netplan/*.yaml
  command -v netplan >/dev/null && netplan generate 2>/dev/null || true
  systemctl disable --now systemd-networkd.socket systemd-networkd.service >/dev/null 2>&1 || true
  systemctl disable systemd-networkd-wait-online.service >/dev/null 2>&1 || true
  write_nm_conf false
  nm_reload_conf
  nmcli --wait 45 connection up "$LAN_CON" >/dev/null 2>&1 || true

  local gw deadline=$((SECONDS + VERIFY_SECONDS))
  while (( SECONDS < deadline )); do
    gw=$(ip -4 route show default dev "$LAN_IF" 2>/dev/null | awk '{print $3; exit}')
    if [[ -n "$gw" ]] && ping -c1 -W2 -I "$LAN_IF" "$gw" >/dev/null 2>&1; then
      log "OK: LAN gestita da NetworkManager, IP $(ip -4 -o addr show dev "$LAN_IF" scope global | awk '{print $4}' | paste -sd' ' -), gateway $gw raggiungibile"
      echo ok > "$bk/result"
      return 0
    fi
    sleep 3
  done
  log "Il gateway non risponde dopo ${VERIFY_SECONDS}s: ROLLBACK"
  restore_from "$bk"
  echo rollback > "$bk/result"
  return 1
}

restore_from() {
  local bk=$1
  log "Ripristino netplan/systemd-networkd da $bk"
  # shellcheck disable=SC1091
  source "$bk/state"
  cp -a "$bk/netplan/." /etc/netplan/
  nmcli connection delete "$LAN_CON" >/dev/null 2>&1 || true
  write_nm_conf true
  nm_reload_conf
  systemctl enable systemd-networkd-wait-online.service >/dev/null 2>&1 || true
  systemctl enable --now systemd-networkd.socket systemd-networkd.service >/dev/null 2>&1 || true
  netplan apply 2>/dev/null || networkctl reconfigure "$LAN_IF" 2>/dev/null || true
  # netplan apply riconfigura la scheda: il DHCP riprende l'indirizzo dopo qualche secondo
  local i addr=""
  for i in $(seq 1 30); do
    addr=$(ip -4 -o addr show dev "$LAN_IF" scope global | awk '{print $4}' | paste -sd' ' -)
    [[ -n "$addr" ]] && break
    sleep 2
  done
  log "Ripristino completato: la LAN e' di nuovo di systemd-networkd (IP ${addr:-non ancora assegnato})"
}

# ═════════════════════════════════════════════════════════════════════════════

if [[ "$MODE" == apply ]]; then
  BK=${2:?}
  # shellcheck disable=SC1091
  source "$BK/state"
  apply_switch "$BK"
  exit $?
fi

if [[ "$MODE" == restore ]]; then
  restore_from "${2:?}"
  exit 0
fi

# Lancia "$@" di questo script in un'unità transitoria e ne aspetta la fine:
# la rete si riconfigura sotto i piedi della sessione SSH, che può cadere.
run_detached() {
  systemctl reset-failed "$UNIT" >/dev/null 2>&1 || true
  systemd-run --unit="$UNIT" --collect --quiet /bin/bash "$(readlink -f "$0")" "$@"
  log "Avviato nell'unita' $UNIT. Se la sessione cade, riconnettersi: l'IP resta lo stesso."
  while systemctl is-active --quiet "$UNIT"; do sleep 2; done
}

if [[ "$MODE" == rollback ]]; then
  BK=$(ls -1d "$BACKUP_ROOT"/*/ 2>/dev/null | tail -1)
  [[ -n "$BK" && -f "$BK/state" ]] || die "nessun backup in $BACKUP_ROOT"
  BK=${BK%/}
  run_detached --restore "$BK"
  exit 0
fi

LAN_IF=$(detect_lan_if)
[[ -n "$LAN_IF" ]] || die "nessuna scheda di rete cablata trovata"

if lan_managed_by_nm; then
  log "La LAN ($LAN_IF) e' gia' gestita da NetworkManager (profilo $LAN_CON): niente da fare"
  exit 0
fi

log "Scheda cablata: $LAN_IF"

# 1. Pacchetti. Prima dell'installazione la LAN va esclusa da NM, che altrimenti
#    appena partito ci farebbe DHCP in parallelo a systemd-networkd.
write_nm_conf true
missing=()
for p in network-manager dnsmasq-base iw; do
  dpkg -s "$p" >/dev/null 2>&1 || missing+=("$p")
done
if (( ${#missing[@]} )); then
  log "Installazione ${missing[*]}..."
  DEBIAN_FRONTEND=noninteractive apt-get -y install "${missing[@]}" >>"$LOG" 2>&1 \
    || die "installazione pacchetti fallita (vedi $LOG)"
fi
systemctl enable --now NetworkManager >/dev/null 2>&1
nm_reload_conf

# 2. Fotografia della configurazione in uso + backup
capture_lan
BK="$BACKUP_ROOT/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BK/netplan"
cp -a /etc/netplan/. "$BK/netplan/" 2>/dev/null || true
cat > "$BK/state" <<EOF
LAN_IF='$LAN_IF'
LAN_MAC='$LAN_MAC'
LAN_METHOD='$LAN_METHOD'
LAN_CID='$LAN_CID'
LAN_ADDRS='$LAN_ADDRS'
LAN_GW='$LAN_GW'
LAN_DNS='$LAN_DNS'
EOF
log "Configurazione in uso: $LAN_METHOD, MAC $LAN_MAC, IP ${LAN_ADDRS:-?}, gw ${LAN_GW:-?}${LAN_CID:+, client-id $LAN_CID}"
log "Backup netplan in $BK"

# 3. Profilo arfea-lan (salvato, non ancora attivo: la LAN e' ancora di networkd)
create_lan_profile
log "Profilo $LAN_CON creato"

if [[ "$MODE" == boot ]]; then
  # Attivo al prossimo riavvio: networkd resta vivo fino ad allora, NM rilegge
  # la config (con la LAN non piu' esclusa) solo quando riparte.
  rm -f /etc/netplan/*.yaml
  systemctl disable systemd-networkd.socket systemd-networkd.service systemd-networkd-wait-online.service >/dev/null 2>&1 || true
  write_nm_conf false
  log "Pronto: al prossimo riavvio la LAN sara' gestita da NetworkManager"
  exit 0
fi

# 4. Passaggio immediato in un'unità transitoria (sopravvive alla caduta di SSH)
run_detached --apply "$BK"
case "$(cat "$BK/result" 2>/dev/null)" in
  ok)       log "Fatto."; exit 0 ;;
  rollback) die "passaggio annullato: la rete e' tornata com'era (dettagli in $LOG)" ;;
  *)        die "esito sconosciuto, controllare $LOG e 'journalctl -u $UNIT'" ;;
esac
