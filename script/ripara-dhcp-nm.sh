#!/bin/bash
###############################################################################
# DHCP di NetworkManager 1.22 (Ubuntu 20.04) sulle ODROID-C4: dhclient al posto
# del client interno (Redmine #537).
#
#   ./script/ripara-dhcp-nm.sh --centralina <alias> [--centralina <alias> ...] [--apply]
#   ./script/ripara-dhcp-nm.sh --tutte [--inventario FILE] [--apply]
#   sudo bash ripara-dhcp-nm.sh --qui [--apply]
#
# Il client DHCP interno di NetworkManager 1.22 (n-dhcp4), se un rinnovo trova
# il router spento o riceve un NAK, tiene il lease vecchio e da li' ogni lease
# nuovo fallisce con "selecting lease failed: -131". La centralina resta senza
# IP finche' il cavo non si stacca o NetworkManager non riparte: un impianto e'
# rimasto fuori rete 15 giorni con la rete fisica sana. La correzione upstream
# (Red Hat bz 2105088) in Ubuntu 20.04 non c'e'; dhclient non ha il difetto.
#
# Senza --apply e' una prova a vuoto: per ogni centralina dice sistema, client
# DHCP in uso, LAN, se il guasto c'e' gia' stato e cosa farebbe.
#
# Con --apply, solo sulle ODROID-C4 con Ubuntu 20.04 dove NetworkManager da' la
# rete in DHCP:
#   1. scrive /etc/NetworkManager/conf.d/90-arfea-dhcp.conf con dhcp=dhclient e
#      ipv4.dhcp-client-id=mac di default (quello che mandava il client interno,
#      quindi il router di solito le ridà lo stesso IP; uno su 14 ne ha dato un
#      altro, e il vecchio resta sulla scheda fino alla scadenza del lease);
#   2. in un'unita' systemd (arfea-dhcp-nm, arriva in fondo anche se cade la
#      sessione ssh) riavvia NetworkManager e aspetta fino a 2 minuti indirizzo,
#      route di default e gateway;
#   3. se la rete non torna, o NetworkManager non usa dhclient, toglie il file e
#      riavvia NetworkManager com'era.
# Non parte se il controller sta facendo backup, release o self-update.
#
# --tutte prende dall'inventario di arfea-manager (default ../arfea-manager/hosts.json
# accanto al repo) le ODROID-C4 con Ubuntu 20.04, con utente e chiave ssh
# dell'inventario. --centralina usa un alias di ~/.ssh/config.
###############################################################################
set -u

REPO=$(cd "$(dirname "$0")/.." && pwd)
INV="$REPO/../arfea-manager/hosts.json"
NAMES=(); HERE=false; APPLY=false; ALL=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --centralina) [[ -n "${2:-}" ]] || { echo "--centralina vuole l'alias ssh"; exit 1; }; NAMES+=("$2"); shift ;;
    --tutte) ALL=true ;;
    --inventario) [[ -f "${2:-}" ]] || { echo "--inventario vuole il file hosts.json di arfea-manager"; exit 1; }; INV=$2; shift ;;
    --qui) HERE=true ;;
    --apply) APPLY=true ;;
    *) echo "opzione sconosciuta: $1"; exit 1 ;;
  esac
  shift
done
if [[ ${#NAMES[@]} -eq 0 ]] && ! $ALL && ! $HERE; then
  echo "uso: $0 --centralina <alias> [--centralina <alias> ...] [--apply]"
  echo "     $0 --tutte [--inventario hosts.json] [--apply]"
  echo "     sudo bash $0 --qui [--apply]"
  exit 1
fi

# --------------------------------------------------------------------------- sulla centralina
# Argomento: rilievo|apply|verifica. L'ultima riga e' sempre
# "ESITO|<stato>|<azione>". Il testo arriva a bash da stdin: niente comandi che
# leggono stdin (si mangerebbero il resto).
REMOTE=$(mktemp)
trap 'rm -f "$REMOTE"' EXIT
cat >"$REMOTE" <<'REMOTE_EOF'
set -u
MODE=$1
CONF=/etc/NetworkManager/conf.d/90-arfea-dhcp.conf
UNIT=arfea-dhcp-nm
SWAP=/run/arfea-dhcp-nm.sh          # lo scambio, eseguito dall'unita'
RES=/run/arfea-dhcp-nm.esito        # "ok|..." o "indietro|...", lo scrive l'unita'
RLOG=/run/arfea-dhcp-nm.log
log() { echo "   $*"; }
esito() { echo "ESITO|$1|$2"; exit 0; }

# ---- dopo l'apply: l'esito dello scambio
if [[ $MODE == verifica ]]; then
  systemctl is-active -q "$UNIT" && esito "scambio in corso" "attesa"
  [[ -s $RLOG ]] && sed 's/^/   /' "$RLOG"
  [[ -s $RES ]] || esito "unita' finita senza esito" "da vedere a mano: journalctl -t arfea-dhcp-nm"
  IFS='|' read -r r why ip <"$RES"
  [[ $r == ok ]] && esito "dhclient in uso, indirizzo $ip" "fatto"
  esito "tornata al client interno ($why), indirizzo ${ip:-NESSUNO}" "NON fatto: da vedere a mano"
fi

model=$(tr -d '\0' </proc/device-tree/model 2>/dev/null)
osinfo=$(. /etc/os-release 2>/dev/null; echo "${ID:-?} ${VERSION_ID:-?}|${PRETTY_NAME:-?}")
nmver=$(NetworkManager --version 2>/dev/null)
log "${model:-modello ?} - ${osinfo#*|} - NetworkManager ${nmver:-assente}, acceso da $(uptime -s)"

case "$model" in
  *ODROID-C4*) ;;
  *) esito "non e' una ODROID-C4" "niente" ;;
esac
[[ ${osinfo%%|*} == "ubuntu 20.04" ]] || esito "non e' Ubuntu 20.04" "niente"
systemctl is-active -q NetworkManager || esito "NetworkManager non attivo" "niente: la rete non e' sua"

hits=$(journalctl -u NetworkManager --no-pager -q 2>/dev/null | grep -c "selecting lease failed")
if [[ $hits -gt 0 ]]; then
  last=$(journalctl -u NetworkManager --no-pager -q -o short-iso 2>/dev/null | grep "selecting lease failed" | tail -1 | cut -c1-10)
  log "GUASTO GIA' VISTO: $hits volte 'selecting lease failed' nel journal (l'ultima il $last)"
fi

backend=$(NetworkManager --print-config 2>/dev/null | awk '/^\[/ { s = $0 } s == "[main]" && /^dhcp=/ { sub(/^dhcp=/, ""); v = $0 } END { print v }')
backend=${backend:-internal}
in_use=$(journalctl -u NetworkManager -b --no-pager -q 2>/dev/null | grep -o "Using DHCP client '[a-z]*'" | tail -1 | cut -d"'" -f2)
log "client DHCP: configurato $backend, in uso ${in_use:-?}"

# La scheda della rete: quella della route di default o, se manca (lease gia'
# perso), la prima cablata fisica di NetworkManager.
dev=$(ip -4 route show default 2>/dev/null | awk '{ for (i = 1; i < NF; i++) if ($i == "dev") { print $(i + 1); exit } }')
if [[ -z "$dev" ]]; then
  dev=$(nmcli -t -f DEVICE,TYPE,STATE device 2>/dev/null | awk -F: '$2 == "ethernet" && $3 != "unmanaged" { print $1 }' \
        | while read -r d; do [[ -e /sys/class/net/$d/device ]] && { echo "$d"; break; }; done)
fi
[[ -n "$dev" ]] || esito "nessuna scheda di rete di NetworkManager" "da vedere a mano"
st=$(nmcli -t -f DEVICE,STATE,CONNECTION device 2>/dev/null | awk -F: -v d="$dev" '$1 == d { print $2 "|" $3 }')
state=${st%%|*}; con=${st#*|}
method=""; [[ -n "$con" ]] && method=$(nmcli -g ipv4.method connection show "$con" 2>/dev/null)
ip4=$(ip -4 -o addr show dev "$dev" scope global 2>/dev/null | awk '{ print $4; exit }')
log "rete: $dev (${state:-?}), profilo '${con:-nessuno}', ipv4.method ${method:-?}, indirizzo ${ip4:-NESSUNO}"

[[ -z "$state" || $state == unmanaged ]] && esito "$dev non e' di NetworkManager" "niente"
[[ $method == auto ]] || esito "$dev non e' in DHCP" "niente"
if [[ $backend == dhclient && $in_use == dhclient ]]; then
  esito "dhclient gia' in uso" "niente"
fi
command -v dhclient >/dev/null || esito "client interno, manca dhclient" "da vedere a mano: apt install isc-dhcp-client"
stato="client DHCP interno"; [[ $backend == dhclient ]] && stato="dhclient configurato, NetworkManager non ancora riavviato"
[[ $hits -gt 0 ]] && stato="$stato, guasto gia' visto"
[[ $MODE == rilievo ]] && esito "$stato" "con --apply: dhclient e riavvio di NetworkManager"

# ---- apply
b=$(for p in system/update/status system/releases/status backup/status; do
      curl -s -m 5 "http://127.0.0.1:8888/api/$p" </dev/null; echo
    done | grep -oE '"state" *: *"(downloading|installing|rebuilding|starting|backup|awaiting_decision|migrating_pre|pulling|recreating|waiting_healthy|migrating_post|stopping_containers|creating_archive|uploading|restarting_containers)"' | head -1)
[[ -n "$b" ]] && esito "$stato" "NON fatto: il controller e' occupato ($b), rilanciare dopo"
systemctl is-active -q "$UNIT" && esito "$stato" "scambio gia' in corso"

cat >"$SWAP" <<'SWAP_EOF'
#!/bin/bash
# Scambio del client DHCP, in un'unita' systemd: riavvia NetworkManager e, se
# entro 2 minuti la rete non c'e' o il client non e' dhclient, torna indietro.
CONF=/etc/NetworkManager/conf.d/90-arfea-dhcp.conf
RES=/run/arfea-dhcp-nm.esito
RLOG=/run/arfea-dhcp-nm.log
dev=$1; prev=$2
say() { logger -t arfea-dhcp-nm "$*"; echo "$(date +%T) $*" >>"$RLOG"; }
addr() { ip -4 -o addr show dev "$dev" scope global 2>/dev/null | awk '{ print $4; exit }'; }
net_ok() {  # indirizzo, route di default e gateway che risponde (ping o ARP)
  local gw
  [[ -n "$(addr)" ]] || return 1
  gw=$(ip -4 route show default dev "$dev" 2>/dev/null | awk '{ print $3; exit }')
  [[ -n "$gw" ]] || return 1
  ping -c 1 -W 2 "$gw" >/dev/null 2>&1 && return 0
  ip neigh show "$gw" dev "$dev" 2>/dev/null | grep -qE 'REACHABLE|STALE|DELAY'
}
restart_wait() {  # riavvia NetworkManager e aspetta la rete fino a 2 minuti
  local i
  systemctl restart NetworkManager
  for i in $(seq 24); do sleep 5; net_ok && return 0; done
  return 1
}
: >"$RLOG"; rm -f "$RES"
since=$(date '+%Y-%m-%d %H:%M:%S')
say "riavvio NetworkManager con dhclient (prima: $dev $prev)"
if restart_wait; then
  used=$(journalctl -u NetworkManager --since "$since" --no-pager -q | grep -o "Using DHCP client '[a-z]*'" | tail -1 | cut -d"'" -f2)
  if [[ $used == dhclient ]]; then
    now=$(addr)
    if [[ $now == "$prev" ]]; then say "rete su con dhclient, stesso indirizzo $now"
    else say "rete su con dhclient, indirizzo cambiato: $prev -> $now"; fi
    echo "ok|dhclient|$now" >"$RES"
    exit 0
  fi
  why="NetworkManager usa ancora il client '${used:-?}'"
else
  why="rete non tornata in 2 minuti"
fi
say "$why: tolgo $CONF e riavvio NetworkManager com'era"
rm -f "$CONF"
if restart_wait; then say "rete su col client interno, indirizzo $(addr)"
else say "ATTENZIONE: rete non tornata nemmeno col client interno"; fi
echo "indietro|$why|$(addr)" >"$RES"
SWAP_EOF
chmod 700 "$SWAP"

cat >"$CONF.new" <<'CONF_EOF'
# ARFEA (Redmine #537): scritto da ripara-dhcp-nm.sh, non modificare a mano.
# Il client DHCP interno di NetworkManager 1.22, dopo un rinnovo fallito, resta
# senza indirizzo ("selecting lease failed: -131") finche' NetworkManager non
# riparte. dhclient non ha il difetto. Il client-id e' quello del client
# interno (01 + MAC): il router di solito le ridà lo stesso IP.
[main]
dhcp=dhclient

[connection]
ipv4.dhcp-client-id=mac
CONF_EOF
mv -f "$CONF.new" "$CONF"
logger -t arfea-dhcp-nm "scritto $CONF (era: client $in_use, $dev $ip4)"
systemctl reset-failed "$UNIT" 2>/dev/null
systemd-run --quiet --unit="$UNIT" --description="ARFEA: client DHCP di NetworkManager a dhclient (Redmine #537)" \
  /bin/bash "$SWAP" "$dev" "${ip4:-nessuno}" </dev/null \
  || { rm -f "$CONF"; esito "$stato" "NON fatto: systemd-run fallito, file tolto"; }
esito "$stato" "dhclient scritto, NetworkManager in riavvio"
REMOTE_EOF

if $HERE; then
  [[ $EUID -eq 0 ]] || { echo "con --qui serve root: sudo bash $0 --qui ..."; exit 1; }
  mode=rilievo; $APPLY && mode=apply
  bash "$REMOTE" "$mode"
  if $APPLY; then
    while systemctl is-active -q arfea-dhcp-nm; do sleep 5; done
    bash "$REMOTE" verifica
  fi
  exit 0
fi

# --------------------------------------------------------------------------- dal PC
# T_NAME, e per chi viene dall'inventario utente, IP e chiave
T_NAME=(); T_USER=(); T_IP=(); T_KEY=()
for n in "${NAMES[@]}"; do T_NAME+=("$n"); T_USER+=(""); T_IP+=(""); T_KEY+=(""); done
SKIPPED=()
if $ALL; then
  PYINV=$(mktemp)
  trap 'rm -f "$REMOTE" "$PYINV"' EXIT
  cat >"$PYINV" <<'PY'
import json, sys
rows = []
for h in json.load(open(sys.argv[1])).get("hosts", []):
    si = h.get("system_info") or {}
    if "ODROID-C4" not in (si.get("hardware_model") or "").upper():
        continue
    if not (si.get("os") or "").startswith("Ubuntu 20.04"):
        continue
    kind = "C4" if h.get("identity_file") else "SKIP"
    ip = h.get("ip", "")
    key = tuple(int(x) if x.isdigit() else 0 for x in ip.split("."))
    rows.append((key, kind, h.get("id", ""), h.get("user", ""), ip, h.get("identity_file", "")))
for r in sorted(rows):
    print("|".join(r[1:]))
PY
  [[ -f "$INV" ]] || { echo "inventario non trovato: $INV (usa --inventario)"; exit 1; }
  while IFS='|' read -r kind name user ip key; do
    case $kind in
      C4) T_NAME+=("$name"); T_USER+=("$user"); T_IP+=("$ip"); T_KEY+=("$key") ;;
      *) SKIPPED+=("$name ($ip): senza chiave ssh nell'inventario") ;;
    esac
  done < <(python3 -I "$PYINV" "$INV")
fi
[[ ${#T_NAME[@]} -gt 0 ]] || { echo "nessuna centralina da controllare"; exit 1; }

run_on() {  # indice, modo, timeout di connessione
  local i=$1 o=(-o BatchMode=yes -o ConnectTimeout="$3" -o ServerAliveInterval=20 -o StrictHostKeyChecking=accept-new)
  if [[ -n "${T_IP[i]}" ]]; then
    o+=(-o IdentitiesOnly=yes -o PubkeyAuthentication=yes -i "${T_KEY[i]}" -l "${T_USER[i]}" "${T_IP[i]}")
  else
    o+=("${T_NAME[i]}")
  fi
  timeout 120 ssh "${o[@]}" "sudo -n bash -s -- $2" <"$REMOTE" 2>&1 \
    | grep -vE 'post-quantum|store now, decrypt later|openssh.com/pq.html'
}
label() { echo "${T_NAME[$1]}${T_IP[$1]:+ (${T_IP[$1]})}"; }

mode=rilievo; $APPLY && mode=apply
$APPLY || echo "PROVA A VUOTO: non tocco niente (--apply per cambiare client DHCP)"
RES_STATO=(); RES_AZIONE=(); WAIT=()
for i in "${!T_NAME[@]}"; do
  echo; echo "== $(label "$i")"
  out=$(run_on "$i" "$mode" 15)
  grep -v '^ESITO|' <<<"$out"
  e=$(grep '^ESITO|' <<<"$out" | tail -1)
  if [[ -z "$e" ]]; then
    RES_STATO[i]="non raggiungibile o errore"; RES_AZIONE[i]=$(grep . <<<"$out" | tail -1 | cut -c1-70)
  else
    RES_STATO[i]=$(cut -d'|' -f2 <<<"$e"); RES_AZIONE[i]=$(cut -d'|' -f3 <<<"$e")
    [[ ${RES_AZIONE[i]} == *"in riavvio" ]] && WAIT+=("$i")
  fi
done

# ---- esito degli scambi: se l'IP cambia la VPN si riaggancia in 2-3 minuti
if [[ ${#WAIT[@]} -gt 0 ]]; then
  echo; echo "== NetworkManager in riavvio su ${#WAIT[@]} centraline: aspetto l'esito (al massimo 8 minuti)"
  sleep 30
  deadline=$(( $(date +%s) + 450 ))
  while [[ ${#WAIT[@]} -gt 0 && $(date +%s) -lt $deadline ]]; do
    left=()
    for i in "${WAIT[@]}"; do
      out=$(run_on "$i" verifica 8)
      e=$(grep '^ESITO|' <<<"$out" | tail -1)
      case "$(cut -d'|' -f3 <<<"$e")" in
        ""|attesa) left+=("$i") ;;
        *) echo; echo "== $(label "$i") dopo"; grep -v '^ESITO|' <<<"$out"
           RES_STATO[i]=$(cut -d'|' -f2 <<<"$e"); RES_AZIONE[i]=$(cut -d'|' -f3 <<<"$e") ;;
      esac
    done
    WAIT=("${left[@]}")
    [[ ${#WAIT[@]} -gt 0 ]] && sleep 20
  done
  for i in "${WAIT[@]}"; do
    RES_AZIONE[i]="NON raggiungibile dopo 8 minuti: riprovare la verifica (prova a vuoto) piu' tardi"
  done
fi

# ---- riepilogo
echo; echo "== riepilogo"
for i in "${!T_NAME[@]}"; do
  printf '%-36s %-60s %s\n' "$(label "$i")" "${RES_STATO[i]}" "${RES_AZIONE[i]}"
done
for s in "${SKIPPED[@]}"; do printf '%-36s %s\n' "${s%%:*}" "saltata:${s#*:}"; done
exit 0
