#!/bin/bash
###############################################################################
# prova-habapp.sh — fa girare il codice HABApp ARFEA su due motori, in locale
#
# Serve quando cambia l'immagine HABApp (o quella di OpenHAB): il codice nuovo
# deve girare sia sul motore di prima sia sul nuovo, perche' il tarball del
# controller lo porta a bordo e il controller lo rideploya all'avvio anche dove
# l'immagine e' ancora la vecchia (CLAUDE.md, riga habapp/).
#
# Tira su in docker OpenHAB (l'immagine del template, con lo skeleton), mosquitto
# e HABApp con regole e librerie del sottoinsieme distribuito e i params di prova
# di script/prova-habapp/params (un termostato per modello, carichi, irrigazione).
# Cinque casi:
#   A  motore vecchio + codice vecchio, OpenHAB appena nato    (la base)
#   B  motore nuovo   + codice vecchio                         (solo immagine nuova)
#   C  motore nuovo   + codice nuovo,   OpenHAB appena nato    (installazione nuova)
#   D  motore vecchio + codice nuovo                           (OTA del controller prima della release)
#   E  motore nuovo   + codice nuovo, dopo D                   (release applicata)
# Per ogni caso stampa le firme di errori e warning che la base A non ha. Gli
# errori uguali in tutti i casi vengono dai params di prova (item che sugli
# impianti stanno nei .items del cliente), non dal motore.
#
# Uso:
#   ./script/prova-habapp.sh [--vecchio <rev>] [--secondi N] [--tieni]
#     --vecchio  revisione git con il codice e l'immagine HABApp di partenza
#                (default HEAD: si prova la copia di lavoro contro l'ultimo commit)
#     --secondi  quanto gira ogni caso (default 200: tre giri delle regole a minuto)
#     --tieni    lascia accesi i container alla fine
# Codice e immagini nuovi sono quelli della copia di lavoro: habapp/$HABAPP_VER e
# il template arfea-controller/config/arfea.yml. Lavora in $PROVA_DIR (default
# /tmp/arfea-prova-habapp), OpenHAB su 127.0.0.1:18080. Non tocca centraline ne'
# servizi esterni; scarica le immagini che mancano.
###############################################################################

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
# shellcheck source=script/habapp-subset.sh
source "$SCRIPT_DIR/habapp-subset.sh"

REV=HEAD SECS=200 KEEP=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --vecchio) REV=$2; shift 2 ;;
    --secondi) SECS=$2; shift 2 ;;
    --tieni) KEEP=true; shift ;;
    -h|--help) sed -n '3,/^#####/p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Opzione sconosciuta: $1 (vedi --help)" >&2; exit 2 ;;
  esac
done

W="${PROVA_DIR:-/tmp/arfea-prova-habapp}"
NET=prova-habapp
OH=prova-habapp-openhab MQ=prova-habapp-mqtt HB=prova-habapp-habapp
IDS=(-e USER_ID="$(id -u)" -e GROUP_ID="$(id -g)" -e TZ=Europe/Rome)
TEMPLATE="$REPO_DIR/arfea-controller/config/arfea.yml"

die() { echo "ERRORE: $*" >&2; exit 1; }
img_of() { grep -oE "\"$1:[^\"]+\"" | tr -d '"' | head -1; }

OH_IMG=$(img_of openhab/openhab < "$TEMPLATE")
NEW_IMG=$(img_of spacemanspiff2007/habapp < "$TEMPLATE")
OLD_IMG=$(git -C "$REPO_DIR" show "$REV:arfea-controller/config/arfea.yml" | img_of spacemanspiff2007/habapp)
OLD_VER=$(git -C "$REPO_DIR" show "$REV:script/habapp-subset.sh" | sed -n 's/^HABAPP_VER="\(.*\)"/\1/p')
MQ_IMG=$(img_of eclipse-mosquitto < "$TEMPLATE")
[[ -n "$OH_IMG" && -n "$NEW_IMG" && -n "$OLD_IMG" && -n "$OLD_VER" && -n "$MQ_IMG" ]] \
  || die "immagini o versione del codice non trovate (template, $REV)"

echo "OpenHAB $OH_IMG, mosquitto $MQ_IMG"
echo "vecchio: codice $OLD_VER ($REV), $OLD_IMG"
echo "nuovo:   codice $HABAPP_VER (copia di lavoro), $NEW_IMG"

cleanup() {
  $KEEP && return 0
  docker rm -f "$HB" "$OH" "$MQ" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
}
trap cleanup EXIT

rm -rf "$W"; mkdir -p "$W/codice-vecchio" "$W/oh/conf" "$W/oh/userdata" "$W/oh/addons"
git -C "$REPO_DIR" archive "$REV" "habapp/$OLD_VER/habapp" | tar -x -C "$W/codice-vecchio"
OLD_SRC="$W/codice-vecchio/habapp/$OLD_VER/habapp"
NEW_SRC="$REPO_DIR/$HABAPP_SRC_REL"
cp -a "$REPO_DIR/skeleton-openhab/conf/." "$W/oh/conf/"
printf 'listener 1883\nallow_anonymous true\n' > "$W/mosquitto.conf"

karaf() { echo "$1" | docker exec -i "$OH" /openhab/runtime/bin/client -p habopen 2>&1 | sed 's/\x1b\[[0-9;]*m//g'; }

oh_start() {
  docker rm -f "$OH" >/dev/null 2>&1 || true
  docker run -d --name "$OH" --network "$NET" -p 127.0.0.1:18080:8080 "${IDS[@]}" \
    -v "$W/oh/conf:/openhab/conf" -v "$W/oh/userdata:/openhab/userdata" -v "$W/oh/addons:/openhab/addons" \
    "$OH_IMG" >/dev/null
  local i out
  for i in $(seq 1 120); do curl -sf -o /dev/null http://127.0.0.1:18080/rest/ && break; sleep 5; done
  # La REST risponde prima che la console abbia i comandi openhab:* (Karaf esce
  # sempre con rc=0: l'esito si legge nell'output)
  for i in $(seq 1 60); do
    out=$(karaf "openhab:users list" || true)
    [[ "$out" == *"openhab:users list"* && "$out" != *"Command not found"* \
       && "$out" != *"unexpected error"* ]] && return 0
    sleep 5
  done
  die "OpenHAB non pronto (docker logs $OH)"
}

oh_reset() {
  docker stop "$OH" >/dev/null
  rm -rf "$W/oh"; cp -a "$W/oh.base" "$W/oh"
  oh_start
}

# Un caso: config di HABApp col sottoinsieme distribuito del codice, params di prova
run_case() {
  local label=$1 img=$2 src=$3 d="$W/caso-$1" x
  rm -rf "$d"; mkdir -p "$d/rules" "$d/lib" "$d/params" "$d/log" "$d/config"
  for x in "${HABAPP_RULE_DIRS[@]}"; do cp -a "$src/rules/$x" "$d/rules/"; done
  for x in "${HABAPP_RULE_FILES[@]}"; do mkdir -p "$d/rules/$(dirname "$x")"; cp "$src/rules/$x" "$d/rules/$x"; done
  for x in "${HABAPP_LIB_DIRS[@]}"; do cp -a "$src/lib/$x" "$d/lib/"; done
  cp "$SCRIPT_DIR/prova-habapp/params/"*.yml "$d/params/"
  sed 's/level: WARN/level: INFO/' "$src/logging.yml" > "$d/logging.yml"
  sed -e "s|__ARFEA_OH_URL__|http://$OH:8080|" -e "s|__ARFEA_OH_TOKEN__|$TOKEN|" \
      -e "0,/^    host: ''/s//    host: '$MQ'/" "$src/config.yml" > "$d/config.yml"
  grep -q "host: '$MQ'" "$d/config.yml" || die "config.yml di $label: host mqtt non impostato"
  find "$d" -name __pycache__ -prune -exec rm -rf {} +
  docker rm -f "$HB" >/dev/null 2>&1 || true
  docker run -d --name "$HB" --network "$NET" "${IDS[@]}" -v "$d:/habapp/config" "$img" >/dev/null
  echo "caso $label: $img, codice $(basename "$(dirname "$src")") per ${SECS}s..."
  sleep "$SECS"
  docker logs "$HB" > "$d/docker.log" 2>&1 || true
  docker stop -t 20 "$HB" >/dev/null; docker rm "$HB" >/dev/null
  [[ -s "$d/log/HABApp.log" ]] || die "caso $label: HABApp non ha scritto il log (vedi $d/docker.log)"
}

for img in "$OH_IMG" "$OLD_IMG" "$NEW_IMG" "$MQ_IMG"; do
  docker image inspect "$img" >/dev/null 2>&1 || docker pull -q "$img" >/dev/null
done
docker rm -f "$HB" "$OH" "$MQ" >/dev/null 2>&1 || true
docker network create "$NET" >/dev/null 2>&1 || true
docker run -d --name "$MQ" --network "$NET" -v "$W/mosquitto.conf:/mosquitto/config/mosquitto.conf:ro" "$MQ_IMG" >/dev/null

echo "OpenHAB: primo avvio, utente admin e token..."
oh_start
# Al primo avvio, mentre OpenHAB installa gli addon dello skeleton, la console
# risponde ma per qualche minuto i comandi falliscono ("An unexpected error
# occurred"): si ritenta finche' l'output dice che e' andata
out="" TOKEN=""
for i in $(seq 1 30); do
  out=$(karaf "openhab:users add admin admin administrator" || true)
  grep -qE "User created|already exists" <<<"$out" && break
  out=""; sleep 10
done
[[ -n "$out" ]] || die "utente admin non creato"
for i in $(seq 1 30); do
  out=$(karaf "openhab:users addApiToken admin prova$i arfea" || true)
  TOKEN=$(grep -oE 'oh\.prova[A-Za-z0-9._-]+' <<<"$out" | tail -1 || true)
  [[ -n "$TOKEN" ]] && break
  sleep 10
done
[[ -n "$TOKEN" ]] || die "token admin non coniato; la console ha risposto: $(tr -s ' \n' ' ' <<<"$out" | tail -c 300)"
code=$(curl -s -o /dev/null -w '%{http_code}' -X PUT -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"type":"Switch","name":"provaToken"}' \
  http://127.0.0.1:18080/rest/items/provaToken)
[[ "$code" == 20[01] ]] || die "il token non scrive (HTTP $code)"
curl -s -o /dev/null -X DELETE -H "Authorization: Bearer $TOKEN" http://127.0.0.1:18080/rest/items/provaToken
sleep 60   # regole JS dello skeleton e addon finiscono di partire prima della fotografia
docker stop "$OH" >/dev/null
cp -a "$W/oh" "$W/oh.base"
oh_start

run_case A "$OLD_IMG" "$OLD_SRC"
run_case B "$NEW_IMG" "$OLD_SRC"
oh_reset
run_case C "$NEW_IMG" "$NEW_SRC"
run_case D "$OLD_IMG" "$NEW_SRC"
run_case E "$NEW_IMG" "$NEW_SRC"

echo
echo "=== base A ($OLD_IMG, codice $OLD_VER)"
python3 -I "$SCRIPT_DIR/prova-habapp/firme.py" "$W/caso-A/log/HABApp.log"
for c in B C D E; do
  echo
  echo "=== caso $c: firme che A non ha"
  python3 -I "$SCRIPT_DIR/prova-habapp/firme.py" --solo-nuove "$W/caso-A/log/HABApp.log" "$W/caso-$c/log/HABApp.log"
done
echo
echo "Log completi in $W/caso-*/log/"
