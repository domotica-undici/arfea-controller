#!/bin/bash
###############################################################################
# Diagnosi e ripristino del controller ARFEA su una centralina (Redmine #354, #355).
#
#   ./script/ripara-controller.sh --centralina <alias> [--apply] [--tarball FILE] [--forza]
#   sudo bash ripara-controller.sh --qui [--apply] [--tarball FILE] [--forza]
#
# Serve quando il controller non c'e' piu' o non parte dopo un aggiornamento, in
# particolare dopo un'interruzione di corrente: su un impianto i file appena
# installati dal self-update e l'immagine appena costruita sono rimasti a 0 byte,
# e Docker ha scartato il container nuovo («failed to load container mount»).
# Dal controller 1.8.16 il self-update regge l'interruzione e il guardiano
# sull'host ricrea il container; questo script resta per le centraline piu'
# vecchie e per quando non c'e' niente di buono da cui ripartire.
#
# Senza --apply e' una prova a vuoto: dice com'e' messo il controller
# (container, immagine, file vuoti o diversi dal MANIFEST, health, versione,
# guardiano, riavvii recenti) e non tocca nulla.
#
# Con --apply, se trova qualcosa da riparare (o con --forza):
#   1. prende il tarball OTA: quello di update_url di arfea.yml, scaricato dalla
#      centralina, oppure --tarball FILE copiato dal PC (centralina senza linea);
#   2. lo estrae in una cartella di appoggio accanto al controller, sync, e lo
#      controlla col MANIFEST.sha256 se c'e';
#   3. porta lo skeleton in OpenHAB (solo i file diversi, owner 9001), poi mette
#      le cartelle al loro posto con un rename (le vecchie in .ripristino-prev/),
#      config/arfea.yml non si tocca; sync; .update_hash = hash del tarball;
#   4. ricostruisce l'immagine in un'unita' systemd (arfea-ripristino), che
#      sopravvive alla caduta dell'ssh, e aspetta che finisca: col guardiano del
#      tarball se c'e' (controller >= 1.8.16), se no con docker compose;
#   5. controlla immagine (app/main.py non vuoto), health e versione.
#
# Il container perso resta come cartella in /var/lib/docker/containers: Docker
# la ignora, e si toglie solo a Docker fermo (cioe' con OpenHAB fermo).
###############################################################################
set -u

HOST=""; HERE=false; APPLY=false; FORZA=false; TB=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --centralina) [[ -n "${2:-}" ]] || { echo "--centralina vuole l'alias ssh"; exit 1; }; HOST=$2; shift ;;
    --qui) HERE=true ;;
    --apply) APPLY=true ;;
    --forza) FORZA=true ;;
    --tarball) [[ -f "${2:-}" ]] || { echo "--tarball vuole un file esistente"; exit 1; }; TB=$2; shift ;;
    *) echo "opzione sconosciuta: $1"; exit 1 ;;
  esac
  shift
done
if [[ -z "$HOST" ]] && ! $HERE; then
  echo "uso: $0 --centralina <alias> [--apply] [--tarball FILE] [--forza]"
  echo "     sudo bash $0 --qui [--apply] [--tarball FILE] [--forza]"
  exit 1
fi

# --------------------------------------------------------------------------- sulla centralina
REMOTE=$(mktemp)
trap 'rm -f "$REMOTE"' EXIT
cat >"$REMOTE" <<'REMOTE_EOF'
set -u
APPLY=$1; FORZA=$2; TBFILE=$3
trap '' HUP PIPE            # se l'ssh cade, lo script va avanti
export LC_ALL=C
log() { echo "$*"; }
EMPTY_SHA=e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
DEFAULT_URL=https://cloud.domoticaundici.it/ota/arfea-controller.tar.xz

DATA=/opt/docker_store
Y=$DATA/arfea-controller/config/arfea.yml
yml() { awk -v k="$1" '/^controller:/{c=1;next} c && /^[^[:space:]]/{c=0} c && $1 == k":" {v=$2; gsub(/["'\'']/, "", v); print v; exit}' "$Y" 2>/dev/null; }
[[ -f "$Y" ]] && DATA=$(yml data_path)
DATA=${DATA:-/opt/docker_store}
D=$DATA/arfea-controller
[[ -d "$D" ]] || { log "ERRORE: nessun controller in $D"; exit 2; }
command -v docker >/dev/null || { log "ERRORE: docker non c'e'"; exit 2; }

# I file del controller che devono essere vuoti: quelli che lo sono nel MANIFEST,
# o (tarball vecchi) gli __init__.py.
legit_empty() {
  if [[ -s "$1/MANIFEST.sha256" ]]; then
    awk -v e="$EMPTY_SHA" '$1 == e {print $2}' "$1/MANIFEST.sha256"
  fi
}
empty_files() {   # $1 = cartella; stampa i file vuoti che non dovrebbero esserlo
  local root=$1 allow
  allow=$(legit_empty "$root")
  find "$root" -type f -size 0 ! -path "$root/backups/*" ! -path "$root/config/*" \
       ! -path "$root/.ripristino-prev/*" ! -path "$root/.update-prev/*" ! -path "$root/.ripristino-staging/*" \
       ! -name '.*' -printf '%P\n' | sort | while read -r f; do
    if [[ -n "$allow" ]]; then grep -qxF "$f" <<<"$allow" && continue
    else [[ "$(basename "$f")" == __init__.py ]] && continue; fi
    echo "$f"
  done
}
manifest_bad() {   # $1 = cartella col MANIFEST; file installati che non tornano
  [[ -s "$1/MANIFEST.sha256" ]] || return 0
  (cd "$1" && grep -vE '^[0-9a-f]{64}  (config/|skeleton-openhab/(conf|cont-init\.d)/)' MANIFEST.sha256 \
     | sha256sum -c --quiet 2>/dev/null | sed 's/: .*//')
}
image_main_size() { docker run --rm --network none --entrypoint wc arfea-controller:latest -c /app/app/main.py 2>/dev/null | awk '{print $1}'; }
health() { curl -sf -m 5 -o /dev/null http://127.0.0.1:8888/api/health; }
version() { curl -s -m 5 http://127.0.0.1:8888/api/system/info 2>/dev/null | sed -n 's/.*"version":"\([^"]*\)".*/\1/p'; }

diagnose() {
  PROBLEMS=()
  local st lost em bad sz
  st=$(docker inspect -f '{{.State.Status}}, riavvii {{.RestartCount}}, creato {{.Created}}' arfea-controller 2>/dev/null) || st=""
  log "container:  ${st:-ASSENTE}"
  [[ "$st" == running* ]] || PROBLEMS+=("container del controller ${st:-assente}")
  lost=$(journalctl -b -u docker --no-pager 2>/dev/null | grep -c 'failed to load container mount')
  if [[ "$lost" -gt 0 ]]; then
    log "docker:     a questo avvio ha scartato $lost container (failed to load container mount)"
    [[ "$st" == running* ]] || PROBLEMS+=("Docker ha scartato un container: metadati persi in uno spegnimento brusco")
  fi
  if docker image inspect arfea-controller:latest >/dev/null 2>&1; then
    sz=$(image_main_size)
    log "immagine:   arfea-controller:latest, app/main.py ${sz:-?} byte$(docker image inspect arfea-controller:prev >/dev/null 2>&1 && echo ', e la precedente :prev')"
    [[ "${sz:-0}" -gt 0 ]] || PROBLEMS+=("immagine arfea-controller:latest con app/main.py vuoto")
  else
    log "immagine:   arfea-controller:latest ASSENTE"
    PROBLEMS+=("immagine del controller assente")
  fi
  em=$(empty_files "$D")
  if [[ -n "$em" ]]; then
    log "file vuoti: $(wc -l <<<"$em") ($(head -5 <<<"$em" | tr '\n' ' ')...)"
    PROBLEMS+=("$(wc -l <<<"$em") file del controller a 0 byte")
  else
    log "file vuoti: nessuno"
  fi
  if [[ -s "$D/MANIFEST.sha256" ]]; then
    bad=$(manifest_bad "$D")
    if [[ -n "$bad" ]]; then
      log "MANIFEST:   $(wc -l <<<"$bad") file diversi ($(head -5 <<<"$bad" | tr '\n' ' ')...)"
      PROBLEMS+=("$(wc -l <<<"$bad") file del controller diversi dal MANIFEST")
    else
      log "MANIFEST:   tutti i file tornano"
    fi
  else
    log "MANIFEST:   assente (controller fino alla 1.8.15)"
  fi
  local run disk
  disk=$(sed -n 's/^VERSION = "\(.*\)"/\1/p' "$D/app/main.py" 2>/dev/null | head -1)
  if health; then
    run=$(version)
    log "health:     ok, versione $run (su disco ${disk:-?})"
    # file nuovi e container vecchio: un aggiornamento che non e' arrivato in
    # fondo, per esempio a disco pieno (Redmine #365)
    [[ -n "$run" && -n "$disk" && "$run" != "$disk" ]] \
      && PROBLEMS+=("gira la $run ma su disco c'e' la $disk: aggiornamento non arrivato in fondo")
  else
    log "health:     non risponde (su disco ${disk:-?})"; PROBLEMS+=("il controller non risponde su 127.0.0.1:8888")
  fi
  log "spazio:     $(df -Pm "$D" | awk 'NR == 2 {print $4}') MB liberi (per una build da zero ne servono 1500)"
  local ge ga
  ge=$(systemctl is-enabled arfea-controller-guard.timer 2>/dev/null); ga=$(systemctl is-active arfea-controller-guard.timer 2>/dev/null)
  log "guardiano:  timer ${ge:-assente}${ge:+, $ga} (dal controller 1.8.16)"
  log "avvii:      $(journalctl --list-boots --no-pager 2>/dev/null | tail -4 | awk '{print $4, $5}' | tr '\n' ' ')"
  log "            (piu' avvii ravvicinati senza spegnimento nel log = corrente mancata)"
}

log "== $(hostname): controller in $D"
diagnose
if [[ ${#PROBLEMS[@]} -eq 0 ]]; then
  log "== il controller e' a posto"
  $FORZA || exit 0
  log "   --forza: reinstallo comunque"
else
  log "== da riparare:"
  for p in "${PROBLEMS[@]}"; do log "   - $p"; done
fi
if ! $APPLY; then
  log "== prova a vuoto: niente e' stato toccato. Per riparare: --apply"
  exit 0
fi

die() { log "ERRORE: $*"; exit 1; }

# ---- 1. tarball
TB=/var/tmp/arfea-ripristino.tar.xz
if [[ "$TBFILE" != - ]]; then
  [[ -s "$TBFILE" ]] || die "tarball copiato dal PC non trovato ($TBFILE)"
  mv -f "$TBFILE" "$TB"
  log "== tarball dal PC"
else
  url=$(yml update_url); url=${url:-$DEFAULT_URL}
  log "== scarico il tarball da $url"
  curl -fsSL -m 300 -o "$TB" "$url" || die "download fallito: rilancia dal PC con --tarball ota/arfea-controller.tar.xz"
fi
xz -t "$TB" 2>/dev/null || die "tarball rovinato"
ver=$(tar -xJOf "$TB" arfea-controller/app/main.py 2>/dev/null | sed -n 's/^VERSION = "\(.*\)"/\1/p' | head -1)
sha=$(sha256sum "$TB" | awk '{print $1}')
log "   versione ${ver:-?}, sha256 ${sha:0:12}..."
[[ -n "$ver" ]] || die "nel tarball non c'e' arfea-controller/app/main.py con la VERSION"

# ---- 2. estrazione e controllo
W=$D/.ripristino-staging; P=$D/.ripristino-prev
rm -rf "$W"; mkdir -p "$W"
tar -xJf "$TB" -C "$W" --strip-components=1 || die "estrazione fallita"
rm -rf "$W/config"
sync
if [[ -s "$W/MANIFEST.sha256" ]]; then
  bad=$(cd "$W" && grep -vE '^[0-9a-f]{64}  config/' MANIFEST.sha256 | sha256sum -c --quiet 2>&1 | head -5)
  [[ -z "$bad" ]] || die "file estratti diversi dal MANIFEST: $bad"
  log "   estratto e controllato col MANIFEST"
else
  em=$(empty_files "$W")
  [[ -z "$em" ]] || die "file estratti vuoti: $em"
  log "   estratto (senza MANIFEST: controllati solo i file vuoti)"
fi

# ---- 3. skeleton in OpenHAB, poi le cartelle al loro posto
OH=$DATA/openhab
mkdir_oh() {   # mkdir -p che da' 9001:9001 alle cartelle che crea
  local d=$1 miss=()
  while [[ ! -d "$d" ]]; do miss=("$d" "${miss[@]}"); d=$(dirname "$d"); done
  for d in "${miss[@]}"; do mkdir "$d" && chown 9001:9001 "$d"; done
}
deploy() {   # $1 sorgente, $2 destinazione, $3 permessi o ""
  cmp -s "$1" "$2" && return 1
  mkdir_oh "$(dirname "$2")"
  local t; t="$(dirname "$2")/.$(basename "$2").arfea-new"
  cp -p "$1" "$t" && chown 9001:9001 "$t" && { [[ -z "$3" ]] || chmod "$3" "$t"; } && mv -f "$t" "$2"
}
n=0
if [[ -d "$OH/conf" && -d "$W/skeleton-openhab" ]]; then
  while IFS= read -r f; do deploy "$W/skeleton-openhab/conf/$f" "$OH/conf/$f" "" && n=$((n + 1)); done \
    < <(cd "$W/skeleton-openhab/conf" 2>/dev/null && find . -type f -printf '%P\n')
  while IFS= read -r f; do deploy "$W/skeleton-openhab/cont-init.d/$f" "$OH/cont-init.d/$f" 755 && n=$((n + 1)); done \
    < <(cd "$W/skeleton-openhab/cont-init.d" 2>/dev/null && find . -maxdepth 1 -type f -printf '%P\n')
fi
log "== skeleton in OpenHAB: $n file aggiornati"

rm -rf "$P"; mkdir -p "$P"
if [[ -d "$W/skeleton-openhab/ui" ]]; then
  mkdir -p "$D/skeleton-openhab"
  [[ -e "$D/skeleton-openhab/ui" ]] && mv "$D/skeleton-openhab/ui" "$P/skeleton-openhab-ui"
  mv "$W/skeleton-openhab/ui" "$D/skeleton-openhab/ui"
fi
rm -rf "$W/skeleton-openhab"
for src in "$W"/* "$W"/.[!.]*; do
  [[ -e "$src" ]] || continue
  name=$(basename "$src")
  [[ "$name" == config ]] && continue
  [[ -e "$D/$name" ]] && mv "$D/$name" "$P/$name"
  mv "$src" "$D/$name"
done
rmdir "$W" 2>/dev/null || rm -rf "$W"
echo "$sha" >"$D/.update_hash.new" && mv -f "$D/.update_hash.new" "$D/.update_hash"
rm -f "$D/.update-pending.json"
sync
em=$(empty_files "$D")
[[ -z "$em" ]] || die "dopo l'installazione restano file vuoti: $em"
log "== file installati (i vecchi in $P), sync fatto"

# ---- 4. rebuild in un'unita' systemd
systemctl reset-failed arfea-ripristino 2>/dev/null
log "== rebuild dell'immagine (unita' arfea-ripristino; log: journalctl -t arfea-update -t arfea-guard)"
since=$(date '+%Y-%m-%d %H:%M:%S')
if [[ -s "$D/script/arfea-controller-guard.sh" ]] && grep -q 'arfea-controller-guard: fine' "$D/script/arfea-controller-guard.sh"; then
  systemd-run --wait --collect --quiet --unit=arfea-ripristino --setenv=ARFEA_DIR="$D" \
    /bin/bash "$D/script/arfea-controller-guard.sh" rebuild
else
  systemd-run --wait --collect --quiet --unit=arfea-ripristino -p WorkingDirectory="$D" /bin/bash -c \
    'sync; docker compose up -d --build --force-recreate 2>&1 | logger -t arfea-update; rc=${PIPESTATUS[0]}; sync; exit $rc'
fi
rc=$?
journalctl -t arfea-update -t arfea-guard --since "$since" --no-pager -o cat 2>/dev/null \
  | grep -vE '^#[0-9]+ (sha256|transferring|DONE|CACHED|\[internal\])' | tail -15 | sed 's/^/   /'
[[ $rc -eq 0 ]] || log "   il rebuild e' finito con rc=$rc"

# ---- 5. controlli
for _ in $(seq 1 30); do health && break; sleep 10; done
docker image prune -f >/dev/null 2>&1; sync
rm -f "$TB"
log "== dopo il ripristino"
diagnose
if [[ ${#PROBLEMS[@]} -eq 0 ]]; then
  log "== controller ripristinato: $(version)"
  exit 0
fi
log "== restano problemi:"
for p in "${PROBLEMS[@]}"; do log "   - $p"; done
exit 1
REMOTE_EOF

if $HERE; then
  [[ $EUID -eq 0 ]] || { echo "con --qui serve root: sudo bash $0 --qui ..."; exit 1; }
  tbarg=-
  if [[ -n "$TB" ]]; then cp "$TB" /var/tmp/arfea-ripristino-in.tar.xz && tbarg=/var/tmp/arfea-ripristino-in.tar.xz; fi
  bash "$REMOTE" "$APPLY" "$FORZA" "$tbarg"
  exit $?
fi

SSH=(ssh -o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=30 "$HOST")
tbarg=-
if [[ -n "$TB" ]] && $APPLY; then
  echo "copio $TB su $HOST..."
  "${SSH[@]}" "sudo tee /var/tmp/arfea-ripristino-in.tar.xz >/dev/null" <"$TB" 2>/dev/null \
    || { echo "ERRORE: copia del tarball non riuscita"; exit 1; }
  tbarg=/var/tmp/arfea-ripristino-in.tar.xz
fi
"${SSH[@]}" "sudo bash -s -- $APPLY $FORZA $tbarg" <"$REMOTE" 2>&1 \
  | grep -vE 'post-quantum|store now, decrypt later|openssh.com/pq.html'
exit "${PIPESTATUS[0]}"
