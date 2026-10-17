#!/bin/bash
###############################################################################
# Seriale ttyAML1 delle ODROID-C4 con Armbian: controllo e ripristino (Redmine #397).
#
#   ./script/ripara-ttyaml1.sh --centralina <alias> [--centralina <alias> ...] [--apply] [--senza-riavvio]
#   ./script/ripara-ttyaml1.sh --ip 10.0.0.2-70 [--inventario FILE] [--apply] [--senza-riavvio]
#   sudo bash ripara-ttyaml1.sh --qui [--apply] [--senza-riavvio]
#
# La UART A della C4 (pin 8 e 10: modulo Z-Wave, XBee di Undici) con Armbian
# esiste come /dev/ttyAML1 solo con l'overlay che mette install.sh.
# Fino all'ottobre 2026 lo script lo copiava in /boot/dtb/amlogic/overlay/, cioe'
# nella cartella del pacchetto linux-dtb: un apt upgrade col kernel nuovo porta
# una /boot/dtb-<versione> senza quel file, overlays= in armbianEnv.txt resta, e
# al riavvio la seriale non c'e' piu' (zwave-js-ui senza controller Z-Wave).
#
# Senza --apply e' una prova a vuoto: per ogni centralina dice chi usa ttyAML1,
# dov'e' l'overlay, se la seriale c'e' e cosa farebbe. Non tocca niente.
#
# Con --apply, solo dove armbianEnv.txt nomina l'overlay ARFEA:
#   1. prova l'overlay sul dtb del kernel installato (fdtoverlay): se non si
#      applica si ferma senza toccare niente;
#   2. lo mette in /boot/overlay-user/ e lo sposta da overlays= a user_overlays=
#      (copia di armbianEnv.txt in /root): e' il posto di Armbian per gli
#      overlay propri, e un aggiornamento del kernel non lo tocca;
#   3. riavvia dove /dev/ttyAML1 manca (non con --senza-riavvio, e non se il
#      controller sta facendo backup, release o self-update), aspetta che la
#      centralina torni e controlla seriale e container che la usano.
# Dove la seriale c'e' ancora (kernel nuovo installato ma non ancora avviato)
# non riavvia: l'overlay vale dal prossimo avvio.
#
# --ip prende dall'inventario di arfea-manager (default ../arfea-manager/hosts.json
# accanto al repo) le ODROID-C4 di quell'intervallo, con utente e chiave ssh
# dell'inventario. --centralina usa un alias di ~/.ssh/config.
###############################################################################
set -u

REPO=$(cd "$(dirname "$0")/.." && pwd)
INV="$REPO/../arfea-manager/hosts.json"
NAMES=(); HERE=false; APPLY=false; NOREBOOT=false; RANGE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --centralina) [[ -n "${2:-}" ]] || { echo "--centralina vuole l'alias ssh"; exit 1; }; NAMES+=("$2"); shift ;;
    --ip) [[ "${2:-}" =~ ^([0-9]+\.[0-9]+\.[0-9]+)\.([0-9]+)-([0-9]+)$ ]] || { echo "--ip vuole un intervallo come 10.0.0.2-70"; exit 1; }; RANGE=$2; shift ;;
    --inventario) [[ -f "${2:-}" ]] || { echo "--inventario vuole il file hosts.json di arfea-manager"; exit 1; }; INV=$2; shift ;;
    --qui) HERE=true ;;
    --apply) APPLY=true ;;
    --senza-riavvio) NOREBOOT=true ;;
    *) echo "opzione sconosciuta: $1"; exit 1 ;;
  esac
  shift
done
if [[ ${#NAMES[@]} -eq 0 && -z "$RANGE" ]] && ! $HERE; then
  echo "uso: $0 --centralina <alias> [--centralina <alias> ...] [--apply] [--senza-riavvio]"
  echo "     $0 --ip 10.0.0.2-70 [--inventario hosts.json] [--apply] [--senza-riavvio]"
  echo "     sudo bash $0 --qui [--apply] [--senza-riavvio]"
  exit 1
fi

# --------------------------------------------------------------------------- sulla centralina
# Argomenti: rilievo|apply|verifica  true|false (senza riavvio).
# L'ultima riga e' sempre "ESITO|<stato>|<azione>". Il testo arriva a bash da
# stdin: niente comandi che leggono stdin (si mangerebbero il resto).
REMOTE=$(mktemp)
trap 'rm -f "$REMOTE"' EXIT
cat >"$REMOTE" <<'REMOTE_EOF'
set -u
MODE=$1; NOREBOOT=$2
F=meson-g12b-odroid-uart-ee-a      # nome del file .dtbo
T=g12b-odroid-uart-ee-a            # come compare in overlays= (senza il prefisso meson-)
ENV=/boot/armbianEnv.txt
FU=/boot/overlay-user/$F.dtbo
FC=/boot/dtb/amlogic/overlay/$F.dtbo
log() { echo "   $*"; }
esito() { echo "ESITO|$1|$2"; exit 0; }
envval() { grep -E "^$1=" "$ENV" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '"'; }
inlist() { envval "$1" | tr ' ' '\n' | grep -qxF "$2"; }

write_dtso() {
  cat <<'DTSO_EOF'
/dts-v1/;
/plugin/;

/ {
    compatible = "amlogic,g12a";

    fragment@0 {
        target = <&uart_A>;
        __overlay__ {
            status = "okay";
            pinctrl-0 = <&uart_a_pins>;
            pinctrl-names = "default";
        };
    };
    fragment@1 {
        target-path = "/aliases";
        __overlay__ {
            serial1 = "/soc/bus@ffd00000/serial@24000";
        };
    };
};
DTSO_EOF
}

model=$(tr -d '\0' </proc/device-tree/model 2>/dev/null)
os=$(. /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-?}")
log "${model:-modello ?} - $os - kernel $(uname -r), acceso da $(uptime -s)"

# chi usa ttyAML1: container (anche fermi), config di controller e Undici
users=""
if command -v docker >/dev/null 2>&1; then
  users=$(docker ps -aq 2>/dev/null | xargs -r docker inspect --format \
    '{{.Name}} ({{.State.Status}}) {{range .HostConfig.Devices}}{{.PathOnHost}}:{{.PathInContainer}} {{end}}' 2>/dev/null \
    | grep ttyAML1 | sed 's#^/##; s/ *$//')
fi
YML=/opt/docker_store/arfea-controller/config/arfea.yml
cfg=$(grep -ls ttyAML1 "$YML" /opt/docker_store/deasy/*.y*ml /etc/undici/* 2>/dev/null | tr '\n' ' ')
[[ -n "$users" ]] && while IFS= read -r u; do log "usa ttyAML1: $u"; done <<<"$users"
[[ -n "$cfg" ]] && log "ttyAML1 nominata in: $cfg"
used=false; [[ -n "$users$cfg" ]] && used=true
dev=false; [[ -e /dev/ttyAML1 ]] && dev=true
if $dev; then log "/dev/ttyAML1 c'e'"; else log "/dev/ttyAML1 NON c'e'"; fi

# I servizi che in arfea.yml mappano ttyAML1 la hanno davvero nel container? Il
# controller toglie i device assenti quando crea un container, e i device si
# fissano solo alla creazione: un container nato mentre la seriale mancava resta
# senza anche dopo, e un riavvio non basta (Redmine #388). Serve un recreate.
missing=""
if [[ -f $YML ]] && command -v docker >/dev/null 2>&1; then
  while read -r s cn; do
    [[ -n "$s" ]] || continue
    if ! m=$(docker inspect "$cn" --format '{{range .HostConfig.Devices}}{{.PathOnHost}} {{end}}' 2>/dev/null); then
      log "servizio $s: nessun container (spento?)"
    elif grep -q ttyAML1 <<<"$m"; then
      log "servizio $s: il container ha ttyAML1"
    else
      log "servizio $s: il container NON ha ttyAML1 (creato mentre la seriale mancava, Redmine #388)"
      missing="$missing $s"
    fi
  done < <(awk '/^services:/ { s = 1; next }
                s && /^[^[:space:]#]/ { s = 0 }
                s && /^  [A-Za-z0-9_.-]+:/ { svc = $1; sub(/:$/, "", svc) }
                s && /^    container_name:/ { v = $2; gsub(/"/, "", v); cn[svc] = v }
                s && /ttyAML1/ && !/^[[:space:]]*#/ { use[svc] = 1 }
                END { for (k in use) print k, (k in cn ? cn[k] : k) }' "$YML")
fi
missing=${missing# }

busy() {
  for p in system/update/status system/releases/status backup/status; do
    curl -s -m 5 "http://127.0.0.1:8888/api/$p" </dev/null; echo
  done | grep -oE '"state" *: *"(downloading|installing|rebuilding|starting|backup|awaiting_decision|migrating_pre|pulling|recreating|waiting_healthy|migrating_post|stopping_containers|creating_archive|uploading|restarting_containers)"' | head -1
}
recreate() {  # i servizi di $missing, dal controller; 1 se uno non riesce
  local s r rc=0
  for s in $missing; do
    r=$(curl -s -m 300 -X POST "http://127.0.0.1:8888/api/services/$s/recreate" </dev/null)
    log "ricreo $s dal controller: ${r:-nessuna risposta}"
    grep -qE '"success" *: *true' <<<"$r" || rc=1
  done
  logger -t arfea-ttyaml1 "ricreati per ttyAML1: $missing (rc=$rc)" 2>/dev/null
  return $rc
}
# Niente (piu') da fare sull'overlay: restano i container senza la seriale.
containers_only() {  # stato, azione gia' fatta (vuota se nessuna)
  local fatto=${2:+$2, }
  if ! $dev || [[ -z "$missing" ]]; then esito "$1" "${2:-niente}"; fi
  [[ $MODE == rilievo ]] && esito "$1, container senza ttyAML1: $missing" "con --apply: ricrea $missing"
  local b; b=$(busy)
  [[ -n "$b" ]] && esito "$1" "${fatto}container da ricreare ($missing) NON ricreati: controller occupato ($b)"
  recreate || esito "$1" "${fatto}ricreazione di $missing fallita: da vedere a mano"
  esito "$1" "${fatto}ricreato $missing"
}

case "$model" in
  *ODROID-C4*) ;;
  *) esito "non e' una ODROID-C4" "niente" ;;
esac
if [[ ! -f $ENV ]]; then
  if $used && ! $dev; then esito "non Armbian, ttyAML1 usata ma assente" "da vedere a mano: la patch ARFEA e' per Armbian"; fi
  containers_only "non Armbian"
fi

classic=false; inlist overlays "$T" && classic=true
user=false; inlist user_overlays "$F" && user=true
log "armbianEnv: overlays=$(envval overlays) user_overlays=$(envval user_overlays) fdtfile=$(envval fdtfile)"
log "overlay in $(dirname "$FC"): $([[ -s $FC ]] && echo si || echo NO); in $(dirname "$FU"): $([[ -s $FU ]] && echo si || echo no)"
dtblink=$(readlink /boot/dtb 2>/dev/null)
log "/boot/dtb -> ${dtblink:-?}"
[[ -n "$dtblink" && "$dtblink" != "dtb-$(uname -r)" ]] && log "kernel nuovo installato ma non ancora avviato ($dtblink)"

if ! $classic && ! $user; then
  if $used && ! $dev; then esito "ttyAML1 usata, overlay ARFEA non nominato" "da vedere a mano: armbianEnv.txt non nomina $T"; fi
  if $dev; then containers_only "ttyAML1 c'e', overlay non ARFEA"; fi
  containers_only "non usa ttyAML1"
fi

stable=false; $user && ! $classic && [[ -s $FU ]] && stable=true
if $stable; then stato="overlay in overlay-user"
elif $classic && [[ -s $FC ]]; then stato="overlay nel dtb, si perde al prossimo kernel"
else stato="overlay PERSO con l'aggiornamento"; fi
$dev || stato="$stato, ttyAML1 manca"

# ---- dopo il riavvio
if [[ $MODE == verifica ]]; then
  $dev || esito "ttyAML1 manca dopo il riavvio" "da vedere a mano: dmesg | grep -i ttyAML"
  curl -s -m 5 http://127.0.0.1:8888/api/health </dev/null | grep -q . \
    || esito "ttyAML1 c'e', controller non ancora su" "attesa"
  if [[ -n "$missing" ]]; then
    b=$(busy); [[ -n "$b" ]] && esito "ttyAML1 c'e', controller occupato ($b)" "attesa"
    recreate || esito "ttyAML1 c'e', ricreazione di $missing fallita" "da vedere a mano"
    esito "ttyAML1 c'e', ricreato $missing" "attesa"
  fi
  down=$(grep -v '(running)' <<<"$users" | grep . | cut -d' ' -f1 | paste -sd' ')
  [[ -n "$down" ]] && esito "ttyAML1 c'e', non ancora su: $down" "attesa"
  # chi la usa riesce ad aprirla? (log dall'avvio del container)
  bad=""
  for c in $(cut -d' ' -f1 <<<"$users"); do
    st=$(docker inspect -f '{{.State.StartedAt}}' "$c" 2>/dev/null) || continue
    lg=$(docker logs --since "$st" "$c" 2>&1)
    grep -iE 'serial|driver.*ready|error' <<<"$lg" | tail -2 | sed "s/^/   $c: /"
    grep -qiE 'failed to open the serial port|cannot open /dev' <<<"$lg" && bad="$bad $c"
  done
  [[ -n "$bad" ]] && esito "ttyAML1 c'e', ma non si apre in:$bad" "da vedere a mano"
  esito "ok" "ttyAML1 c'e' e chi la usa e' su"
fi

if $stable; then
  $dev && containers_only "$stato"
  azione="riavvio"
else
  azione="overlay in overlay-user"; $dev || azione="$azione + riavvio"
  $dev && [[ -n "$missing" ]] && azione="$azione + ricrea $missing"
fi
if [[ $MODE == rilievo ]]; then
  if [[ $NOREBOOT == true ]]; then
    azione=${azione/ + riavvio/}; [[ $azione == riavvio ]] && azione="riavvio (escluso da --senza-riavvio)"
  fi
  [[ -n "$missing" ]] && ! $dev && azione="$azione (poi ricrea $missing)"
  esito "$stato" "con --apply: $azione"
fi

# ---- apply
if ! $stable; then
  tmpd=$(mktemp -d)
  trap 'rm -rf "$tmpd"' EXIT
  src=""
  for c in "$FU" "/opt/$F.dtbo" "$FC"; do [[ -s $c ]] && { src=$c; break; }; done
  if [[ -z "$src" ]]; then
    command -v dtc >/dev/null || esito "ERRORE" "manca dtc (apt install device-tree-compiler) e non c'e' un $F.dtbo da copiare"
    if [[ -s /opt/$F.dtso ]]; then cp /opt/$F.dtso "$tmpd/$F.dtso"; else write_dtso >"$tmpd/$F.dtso"; fi
    dtc -q -O dtb -o "$tmpd/$F.dtbo" "$tmpd/$F.dtso" || esito "ERRORE" "compilazione dell'overlay fallita"
    src=$tmpd/$F.dtbo
  fi
  log "overlay preso da $src"

  # si applica al dtb del kernel installato (quello del prossimo avvio)?
  # Senza fdtfile in armbianEnv.txt u-boot usa quello della C4.
  fdt=$(envval fdtfile); base=/boot/dtb/${fdt:-amlogic/meson-sm1-odroid-c4.dtb}
  if ! command -v fdtoverlay >/dev/null; then
    log "prova dell'overlay saltata: manca fdtoverlay"
  elif [[ ! -s $base ]]; then
    log "prova dell'overlay saltata: manca $base"
  elif fdtoverlay -i "$base" -o "$tmpd/prova.dtb" "$src" >"$tmpd/fdtoverlay.log" 2>&1; then
    log "prova su $base: l'overlay si applica"
  else
    sed 's/^/   /' "$tmpd/fdtoverlay.log"
    esito "ERRORE" "l'overlay non si applica a $base: non tocco niente"
  fi

  bak=/root/armbianEnv.txt.prima-ttyaml1-$(date +%Y%m%d_%H%M%S)
  cp -p "$ENV" "$bak"
  if grep -aqs user_overlays /boot/boot.scr /boot/boot.cmd; then
    mkdir -p "$(dirname "$FU")"
    if [[ "$src" != "$FU" ]]; then cp "$src" "$FU.new" && mv -f "$FU.new" "$FU"; fi
    awk -v t="$T" -v f="$F" '
      /^overlays=/ { n = split(substr($0, 10), a, " "); s = ""
                     for (i = 1; i <= n; i++) if (a[i] != t) s = s (s == "" ? "" : " ") a[i]
                     if (s != "") print "overlays=" s
                     next }
      /^user_overlays=/ { u = 1; v = substr($0, 15); n = split(v, a, " "); k = 0
                          for (i = 1; i <= n; i++) if (a[i] == f) k = 1
                          if (!k) v = v (v == "" ? "" : " ") f
                          print "user_overlays=" v
                          next }
      { print }
      END { if (!u) print "user_overlays=" f }' "$ENV" >"$ENV.new"
    if [[ -s "$ENV.new" ]] && grep -q "^user_overlays=.*$F" "$ENV.new"; then
      mv -f "$ENV.new" "$ENV"
    else
      rm -f "$ENV.new"; esito "ERRORE" "armbianEnv.txt nuovo non valido: non toccato"
    fi
    sync
    if ! inlist user_overlays "$F" || inlist overlays "$T" || [[ ! -s $FU ]]; then
      cp -p "$bak" "$ENV"; sync
      esito "ERRORE" "controllo dopo la scrittura fallito: armbianEnv.txt rimesso com'era"
    fi
    log "overlay in $FU, user_overlays=$(envval user_overlays), overlays=$(envval overlays) (prima: $bak)"
  else
    # boot.scr senza overlay utente: si rimette dov'era, e si riperdera'
    cp "$src" "$FC.new" && mv -f "$FC.new" "$FC"
    inlist overlays "$T" || echo "overlays=$T" >>"$ENV"
    sync
    log "ATTENZIONE: boot.scr non conosce user_overlays: overlay rimesso in $FC, si perdera' al prossimo kernel"
  fi
  logger -t arfea-ttyaml1 "overlay ttyAML1 sistemato ($stato)" 2>/dev/null
  $dev && containers_only "$stato" "overlay sistemato, nessun riavvio (l'overlay vale dal prossimo avvio)"
fi

# ---- riavvio (dopo, la verifica dal PC ricrea i container rimasti senza seriale)
[[ $NOREBOOT == true ]] && esito "$stato" "overlay sistemato, riavvio da fare (--senza-riavvio), poi rilanciare --apply"
b=$(busy)
[[ -n "$b" ]] && esito "$stato" "overlay sistemato, riavvio NON fatto: il controller e' occupato ($b), rilanciare dopo"
logger -t arfea-ttyaml1 "riavvio per l'overlay ttyAML1" 2>/dev/null
sync
systemd-run --quiet --on-active=5 --unit="arfea-ttyaml1-riavvio-$(date +%s)" /bin/systemctl reboot
esito "$stato" "overlay sistemato, RIAVVIO"
REMOTE_EOF

if $HERE; then
  [[ $EUID -eq 0 ]] || { echo "con --qui serve root: sudo bash $0 --qui ..."; exit 1; }
  mode=rilievo; $APPLY && mode=apply
  bash "$REMOTE" "$mode" "$NOREBOOT"
  exit $?
fi

# --------------------------------------------------------------------------- dal PC
# T_NAME, e per chi viene dall'inventario utente, IP e chiave
T_NAME=(); T_USER=(); T_IP=(); T_KEY=()
for n in "${NAMES[@]}"; do T_NAME+=("$n"); T_USER+=(""); T_IP+=(""); T_KEY+=(""); done
SKIPPED=(); OTHERS=0
if [[ -n "$RANGE" ]]; then
  PYINV=$(mktemp)
  trap 'rm -f "$REMOTE" "$PYINV"' EXIT
  cat >"$PYINV" <<'PY'
import json, sys
inv, rng = sys.argv[1], sys.argv[2]
net, span = rng.rsplit(".", 1)
lo, hi = (int(x) for x in span.split("-"))
rows = []
for h in json.load(open(inv)).get("hosts", []):
    ip = h.get("ip", "")
    pre, _, last = ip.rpartition(".")
    if pre != net or not last.isdigit() or not lo <= int(last) <= hi:
        continue
    model = (h.get("system_info") or {}).get("hardware_model") or ""
    if "ODROID-C4" not in model.upper():
        kind = "SKIP" if not model else "ALTRO"
    elif h.get("identity_file"):
        kind = "C4"
    else:
        kind, model = "SKIP", model + ", senza chiave ssh nell'inventario"
    rows.append((int(last), kind, h.get("id", ""), h.get("user", ""), ip, h.get("identity_file", ""), model))
for r in sorted(rows):
    print("|".join(str(x) for x in r[1:]))
PY
  [[ -f "$INV" ]] || { echo "inventario non trovato: $INV (usa --inventario)"; exit 1; }
  while IFS='|' read -r kind name user ip key model; do
    case $kind in
      C4) T_NAME+=("$name"); T_USER+=("$user"); T_IP+=("$ip"); T_KEY+=("$key") ;;
      SKIP) SKIPPED+=("$name ($ip): ${model:-modello non rilevato}") ;;
      *) OTHERS=$((OTHERS + 1)) ;;
    esac
  done < <(python3 -I "$PYINV" "$INV" "$RANGE")
fi
[[ ${#T_NAME[@]} -gt 0 ]] || { echo "nessuna ODROID-C4 da controllare"; exit 1; }

run_on() {  # indice, modo, timeout di connessione
  local i=$1 o=(-o BatchMode=yes -o ConnectTimeout="$3" -o ServerAliveInterval=20 -o StrictHostKeyChecking=accept-new)
  if [[ -n "${T_IP[i]}" ]]; then
    o+=(-o IdentitiesOnly=yes -o PubkeyAuthentication=yes -i "${T_KEY[i]}" -l "${T_USER[i]}" "${T_IP[i]}")
  else
    o+=("${T_NAME[i]}")
  fi
  ssh "${o[@]}" "sudo -n bash -s -- $2 $NOREBOOT" <"$REMOTE" 2>&1 \
    | grep -vE 'post-quantum|store now, decrypt later|openssh.com/pq.html'
}
label() { echo "${T_NAME[$1]}${T_IP[$1]:+ (${T_IP[$1]})}"; }

mode=rilievo; $APPLY && mode=apply
$APPLY || echo "PROVA A VUOTO: non tocco niente (--apply per sistemare)"
RES_STATO=(); RES_AZIONE=(); WAIT=(); REBOOTED=0
for i in "${!T_NAME[@]}"; do
  echo; echo "== $(label "$i")"
  out=$(run_on "$i" "$mode" 15)
  grep -v '^ESITO|' <<<"$out"
  e=$(grep '^ESITO|' <<<"$out" | tail -1)
  if [[ -z "$e" ]]; then
    RES_STATO[i]="non raggiungibile o errore"; RES_AZIONE[i]=$(grep . <<<"$out" | tail -1 | cut -c1-70)
  else
    RES_STATO[i]=$(cut -d'|' -f2 <<<"$e"); RES_AZIONE[i]=$(cut -d'|' -f3 <<<"$e")
    [[ ${RES_AZIONE[i]} == *RIAVVIO ]] && { WAIT+=("$i"); REBOOTED=$((REBOOTED + 1)); }
    [[ ${RES_AZIONE[i]} == *ricreato* ]] && WAIT+=("$i")
  fi
done

# ---- attesa e verifica delle centraline riavviate o coi container ricreati
if [[ ${#WAIT[@]} -gt 0 ]]; then
  echo
  if [[ $REBOOTED -gt 0 ]]; then
    echo "== riavvio di $REBOOTED centraline: aspetto che tornino (al massimo 12 minuti)"
    sleep 60
  else
    echo "== container ricreati: controllo che aprano la seriale"
    sleep 20
  fi
  deadline=$(( $(date +%s) + 660 ))
  while [[ ${#WAIT[@]} -gt 0 && $(date +%s) -lt $deadline ]]; do
    left=()
    for i in "${WAIT[@]}"; do
      out=$(run_on "$i" verifica 8)
      e=$(grep '^ESITO|' <<<"$out" | tail -1)
      case "$(cut -d'|' -f3 <<<"$e")" in
        ""|attesa) left+=("$i"); [[ -n "$e" ]] && RES_AZIONE[i]="${RES_AZIONE[i]%%; dopo*}; dopo: $(cut -d'|' -f2 <<<"$e")" ;;
        *) echo; echo "== $(label "$i") dopo"; grep -v '^ESITO|' <<<"$out"
           RES_AZIONE[i]="${RES_AZIONE[i]%%; dopo*}; dopo: $(cut -d'|' -f2 <<<"$e")" ;;
      esac
    done
    WAIT=("${left[@]}")
    [[ ${#WAIT[@]} -gt 0 ]] && sleep 20
  done
  for i in "${WAIT[@]}"; do
    [[ ${RES_AZIONE[i]} == *"; dopo: "* ]] || RES_AZIONE[i]="${RES_AZIONE[i]}; NON tornata entro 12 minuti"
    RES_AZIONE[i]="${RES_AZIONE[i]} (tempo scaduto)"
  done
fi

# ---- riepilogo
echo; echo "== riepilogo"
for i in "${!T_NAME[@]}"; do
  printf '%-36s %-55s %s\n' "$(label "$i")" "${RES_STATO[i]}" "${RES_AZIONE[i]}"
done
for s in "${SKIPPED[@]}"; do printf '%-36s %s\n' "${s%%:*}" "saltata:${s#*:}"; done
[[ $OTHERS -gt 0 ]] && echo "(+ $OTHERS centraline dell'intervallo che non sono ODROID-C4)"
exit 0
