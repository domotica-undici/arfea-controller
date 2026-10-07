#!/bin/bash
###############################################################################
# Guardiano del controller ARFEA, sull'host (controller >= 1.8.16, Redmine #355).
#
# Il controller e' l'unico che crea i container, quindi se manca lui non lo
# ricrea nessuno. Su un impianto una serie di interruzioni di corrente subito
# dopo un self-update ha lasciato a 0 byte i file appena installati e l'immagine
# appena costruita, e Docker ha scartato in silenzio il container nuovo
# ("failed to load container mount"): niente controller per un giorno e mezzo,
# senza che niente lo segnalasse (Redmine #354).
#
#   arfea-controller-guard rebuild   rebuild del self-update: lo lancia il
#                                    controller in un'unita' systemd transitoria
#   arfea-controller-guard check     controllo periodico (timer ogni 5 minuti)
#   arfea-controller-guard status    stato, senza toccare nulla
#
# rebuild: sync; controlla i file installati col MANIFEST.sha256 del tarball e
#   lo spazio (1500 MB, se no pulisce o non parte); tiene l'immagine del
#   controller in esecuzione come arfea-controller:prev (se e' sano); build e
#   recreate; sync; controlla che nell'immagine nuova app/main.py sia quello del
#   MANIFEST (se no, build senza cache); aspetta /api/health fino a 10 minuti. Sano: toglie immagini e cache orfane. In crash
#   loop: torna a :prev. Ancora in avvio: lascia stare, :prev resta.
# check: se il container manca, o resta "created", lo ricrea dall'immagine (da
#   :prev se l'immagine e' vuota; con una build se i file su disco sono quelli
#   del MANIFEST). Se va in crash loop torna a :prev.
#   Un container fermato a mano (exited senza crash) non si tocca.
#
# Per tenere giu' il controller durante una manutenzione:
#   systemctl stop arfea-controller-guard.timer          (fino al riavvio)
#   systemctl disable --now arfea-controller-guard.timer (sempre)
#   touch /run/arfea-controller-guard.off                (fino al riavvio)
#
# Log: journalctl -t arfea-guard (e -t arfea-update per la build). Quello che
# fa lo annota anche in .guard-events: il controller, al suo avvio, lo porta
# nelle riparazioni automatiche (GET /api/system/repairs).
#
# Lo installa il controller in /usr/local/sbin/arfea-controller-guard, con le
# unita' arfea-controller-guard.service e .timer: non va copiato a mano.
###############################################################################
set -u
export LC_ALL=C

DIR=${ARFEA_DIR:-/opt/docker_store/arfea-controller}
# le altre si cambiano solo per provarlo su un PC, accanto a un controller vero
IMG=${ARFEA_GUARD_IMAGE:-arfea-controller}
NAME=${ARFEA_GUARD_NAME:-arfea-controller}
HEALTH=${ARFEA_GUARD_HEALTH:-http://127.0.0.1:8888/api/health}
RUNSTATE=${ARFEA_GUARD_RUN:-/run/arfea-controller-guard}
SPACE_MB=${ARFEA_GUARD_SPACE_MB:-1500}
EVENTS=$DIR/.guard-events

log() { echo "$*"; logger -t arfea-guard -- "$*" 2>/dev/null || true; }
event() {
  log "$1"
  printf '%s\t%s\t%s\n' "$(date +%Y-%m-%dT%H:%M:%S)" controller "$1" >>"$EVENTS" 2>/dev/null || true
}

image_id() { docker image inspect -f '{{.Id}}' "$1" 2>/dev/null; }
container_field() { docker inspect -f "$1" "$NAME" 2>/dev/null; }
healthy() { curl -sf -m 5 -o /dev/null "$HEALTH"; }

# Righe del MANIFEST che descrivono file installati in $DIR: lo skeleton di
# OpenHAB finisce in openhab/, e config/ non lo tocca nessun aggiornamento.
manifest_lines() {
  grep -vE '^[0-9a-f]{64}  (config/|skeleton-openhab/(conf|cont-init\.d)/)' "$DIR/MANIFEST.sha256"
}

# I file del controller su disco sono interi? Con il MANIFEST si confronta lo
# sha256 di ognuno; senza (tarball vecchi) almeno nessun .py vuoto in app/.
files_ok() {
  if [[ -s "$DIR/MANIFEST.sha256" ]]; then
    local bad
    bad=$(cd "$DIR" && manifest_lines | sha256sum -c --quiet 2>&1 | grep -v '^sha256sum:' | head -5)
    [[ -z "$bad" ]] && return 0
    log "file del controller diversi dal MANIFEST: $(echo "$bad" | tr '\n' ' ')"
    return 1
  fi
  local empty
  empty=$(find "$DIR/app" -name '*.py' ! -name '__init__.py' -size 0 2>/dev/null | head -5)
  [[ -z "$empty" && -s "$DIR/app/main.py" ]] && return 0
  log "file del controller vuoti: ${empty:-app/main.py} (manca il MANIFEST per controllarli tutti)"
  return 1
}

# Nell'immagine c'e' il codice? Sempre: app/main.py non vuoto. Con "appena
# costruita" anche lo sha256 del MANIFEST: solo subito dopo una build, perche'
# fuori da li' i file su disco possono essere piu' nuovi dell'immagine (dopo un
# ritorno a :prev), e un'immagine vecchia ma buona va bene per ripartire.
image_ok() {
  local img=$1 want="" got
  [[ "${2:-}" == appena-costruita && -s "$DIR/MANIFEST.sha256" ]] \
    && want=$(awk '$2 == "app/main.py" {print $1}' "$DIR/MANIFEST.sha256")
  got=$(docker run --rm --network none --entrypoint sha256sum "$img" /app/app/main.py 2>/dev/null | awk '{print $1}')
  [[ -n "$got" && "$got" != e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855 ]] || return 1
  [[ -z "$want" || "$got" == "$want" ]]
}

# Spazio per una build da zero (Redmine #365): sulla centralina di test, con 762
# MB liberi, l'esportazione dell'immagine e' morta con «no space left on
# device». Se manca si tolgono immagini orfane e cache di build; se ancora non
# basta la build non parte, e il controller che gira resta acceso.
free_mb() {
  local m="" v p
  for p in "$DIR" "$(docker info -f '{{.DockerRootDir}}' 2>/dev/null)" /var/lib/docker /var/lib/containerd; do
    [[ -n "$p" && -d "$p" ]] || continue
    v=$(df -Pm "$p" 2>/dev/null | awk 'NR == 2 {print $4}')
    [[ -n "$v" ]] && { [[ -z "$m" || "$v" -lt "$m" ]] && m=$v; }
  done
  echo "${m:-0}"
}

space_ok() {
  local f
  f=$(free_mb); [[ "$f" -ge "$SPACE_MB" ]] && return 0
  log "spazio per la build: $f MB liberi, ne servono $SPACE_MB: tolgo immagini orfane e cache di build"
  # a gradini: la cache ancora valida fa una build di pochi secondi, e si toglie
  # solo se senza non c'e' posto
  local step
  for step in "image prune -f" "builder prune -f" "builder prune -af"; do
    # shellcheck disable=SC2086
    docker $step 2>&1 | logger -t arfea-update
    f=$(free_mb); [[ "$f" -ge "$SPACE_MB" ]] && return 0
  done
  event "build del controller non avviata: $f MB liberi anche dopo la pulizia, ne servono $SPACE_MB (resta acceso quello di prima)"
  return 1
}

compose() { (cd "$DIR" && docker compose "$@" 2>&1 | logger -t arfea-update; exit "${PIPESTATUS[0]}"); }

restarts() { container_field '{{.RestartCount}}'; }

rollback() {
  local why=$1 prev cur
  prev=$(image_id "$IMG:prev"); cur=$(container_field '{{.Image}}')
  if [[ -z "$prev" ]]; then
    event "$why; nessuna immagine precedente ($IMG:prev): serve script/ripara-controller.sh"
    return 1
  fi
  if [[ "$prev" == "$cur" ]]; then
    event "$why, ma gira gia' l'immagine precedente: serve script/ripara-controller.sh"
    return 1
  fi
  # Si torna solo indietro, mai avanti: se anche :prev non parte, al giro dopo
  # gira gia' lei e qui ci si ferma.
  docker tag "$IMG:prev" "$IMG:latest" || return 1
  compose up -d --no-build --force-recreate
  # l'immagine rotta ora e' orfana: sulla centralina di test erano 1,1 GB
  docker image prune -f 2>&1 | logger -t arfea-update
  sync
  event "$why: tornato all'immagine precedente ($IMG:prev)"
}

# ----------------------------------------------------------------------------- rebuild
do_rebuild() {
  [[ "${1:-}" == --delay ]] && sleep 2       # la risposta HTTP del controller esce prima del recreate
  cd "$DIR" || { log "cartella $DIR assente"; exit 1; }
  sync
  if ! files_ok; then
    event "aggiornamento fermato prima della build: i file installati non sono interi (il controller attuale resta acceso)"
    exit 1
  fi
  # :prev e' l'immagine del controller che gira adesso, se e' sano: non per
  # forza :latest, che puo' essere una build fallita a meta' (Redmine #365). Va
  # etichettata PRIMA della pulizia dello spazio: rimasta senza nome (dopo una
  # build fallita :latest punta altrove) image prune la toglie anche se il
  # container la sta usando, e poi non si torna piu' indietro.
  local cur
  cur=$(container_field '{{.Image}}')
  if [[ -n "$cur" ]] && healthy; then
    if docker tag "$cur" "$IMG:prev" 2>/dev/null; then log "immagine in uso tenuta come $IMG:prev"
    else log "immagine in uso ($cur) non etichettabile come $IMG:prev: niente ritorno indietro"; fi
  fi
  space_ok || exit 1

  compose up -d --build --force-recreate
  local rc=$?
  sync
  if [[ $rc -eq 0 ]] && ! image_ok "$IMG:latest" appena-costruita; then
    log "l'immagine nuova non ha il codice giusto: build senza cache"
    compose build --no-cache && compose up -d --force-recreate
    rc=$?
    sync
  fi
  if [[ $rc -ne 0 ]]; then
    if [[ -z "$(container_field '{{.Id}}')" ]] || [[ "$(container_field '{{.State.Status}}')" != running ]]; then
      rollback "build o avvio del controller nuovo falliti (docker compose rc=$rc)"
    else
      event "build del controller nuovo fallita (docker compose rc=$rc): resta acceso quello di prima"
    fi
    exit 1
  fi

  local i
  for i in $(seq 1 60); do
    if healthy; then
      docker image prune -f 2>&1 | logger -t arfea-update
      docker builder prune -f 2>&1 | logger -t arfea-update
      sync
      log "controller nuovo sano"
      exit 0
    fi
    if [[ "$(restarts)" -ge 3 ]]; then
      rollback "il controller nuovo non parte (riavviato $(restarts) volte)"
      exit 1
    fi
    sleep 10
  done
  log "controller nuovo non ancora sano dopo 10 minuti: lascio stare, l'immagine precedente resta come $IMG:prev"
}

# ----------------------------------------------------------------------------- check
# Una situazione strana deve durare due giri di fila prima di toccare qualcosa:
# un docker compose up --force-recreate lascia per qualche secondo il controller
# senza container, e un controllo che cade li' in mezzo non deve intervenire.
seen_twice() {
  mkdir -p "$RUNSTATE"
  if [[ -f "$RUNSTATE/$1" ]]; then return 0; fi
  touch "$RUNSTATE/$1"; return 1
}

recreate() {
  local why=$1 lat prev
  lat=$(image_id "$IMG:latest"); prev=$(image_id "$IMG:prev")
  if [[ -n "$lat" ]] && image_ok "$IMG:latest"; then
    compose up -d --no-build && { sync; event "$why: ricreato dall'immagine attuale"; return 0; }
  fi
  if [[ -n "$prev" && "$prev" != "$lat" ]] && image_ok "$IMG:prev"; then
    docker tag "$IMG:prev" "$IMG:latest" && compose up -d --no-build --force-recreate \
      && { sync; event "$why e immagine attuale rotta: ricreato dall'immagine precedente ($IMG:prev)"; return 0; }
  fi
  if files_ok && space_ok; then
    compose up -d --build && { sync; event "$why e nessuna immagine buona: ricostruito dai file su disco"; return 0; }
  fi
  event "$why e niente da cui ricrearlo (immagini e file rotti): serve script/ripara-controller.sh"
  return 1
}

# Un docker compose che lavora sul controller (cartella corrente o -f dentro
# $DIR): un recreate in corso, o qualcuno che lo sta facendo a mano. Quelli di
# altri progetti (deasy) e i "logs -f" lasciati aperti altrove non contano.
compose_busy() {
  local p
  for p in $(pgrep -f 'docker[ -]compose'); do
    [[ "$(readlink "/proc/$p/cwd" 2>/dev/null)" == "$DIR" ]] && return 0
    tr '\0' ' ' <"/proc/$p/cmdline" 2>/dev/null | grep -qF "$DIR/" && return 0
  done
  return 1
}

do_check() {
  [[ -e "$RUNSTATE.off" ]] && exit 0
  [[ -s "$DIR/docker-compose.yml" ]] || exit 0                  # controller non installato
  for u in arfea-selfupdate arfea-ripristino; do
    systemctl is-active --quiet "$u" && exit 0                 # un aggiornamento e' in corso
  done
  compose_busy && exit 0                                       # qualcuno sta lavorando col compose
  docker info >/dev/null 2>&1 || exit 0                        # Docker giu': non e' compito nostro

  # Quando non c'e' piu' niente da fare (nessuna immagine buona, :prev che
  # non parte) lo si dice una volta sola fino al prossimo riavvio, non ogni 5
  # minuti: il marcatore sta in /run.
  local st rc code cid
  st=$(container_field '{{.State.Status}}'); cid=$(container_field '{{.Id}}')
  case "$st" in
    ""|created)
      [[ -e "$RUNSTATE/gaveup-${cid:-missing}" ]] && exit 0
      seen_twice "${st:-missing}" || exit 0
      rm -f "$RUNSTATE/${st:-missing}"
      if [[ -z "$st" ]]; then recreate "container del controller assente"
      else recreate "container del controller rimasto in 'created' senza partire"; fi \
        || touch "$RUNSTATE/gaveup-${cid:-missing}"
      ;;
    restarting|exited)
      rc=$(restarts); code=$(container_field '{{.State.ExitCode}}')
      if [[ "${rc:-0}" -ge 5 && ( "$st" == restarting || "${code:-0}" != 0 ) ]]; then
        [[ -e "$RUNSTATE/gaveup-$cid" ]] && exit 0
        seen_twice crashloop || exit 0
        rm -f "$RUNSTATE/crashloop"
        rollback "controller in crash loop (riavviato $rc volte)" || touch "$RUNSTATE/gaveup-$cid"
      fi
      ;;
    *)
      rm -f "$RUNSTATE"/missing "$RUNSTATE"/created "$RUNSTATE"/crashloop "$RUNSTATE"/gaveup-missing
      ;;
  esac
}

# ----------------------------------------------------------------------------- status
do_status() {
  local c
  c=$(container_field '{{.State.Status}} (riavvii {{.RestartCount}}, immagine {{.Image}})')
  echo "cartella:   $DIR"
  echo "container:  ${c:-ASSENTE}"
  echo "latest:     $(image_id "$IMG:latest" || echo assente)"
  echo "prev:       $(image_id "$IMG:prev" || echo assente)"
  if image_ok "$IMG:latest"; then echo "immagine:   codice a posto"; else echo "immagine:   codice ROTTO o assente"; fi
  if files_ok >/dev/null; then echo "file:       a posto"; else echo "file:       ROTTI"; fi
  if healthy; then echo "health:     ok"; else echo "health:     non risponde"; fi
}

case "${1:-}" in
  rebuild) shift; do_rebuild "$@" ;;
  check)   do_check ;;
  status)  do_status ;;
  *) echo "uso: $0 rebuild [--delay] | check | status"; exit 2 ;;
esac
# arfea-controller-guard: fine
