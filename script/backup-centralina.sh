#!/bin/bash
###############################################################################
# Backup di una centralina via ssh, scaricato sul PC (Redmine #338).
# Gira sul PC, o dallo Script Hub, che a fine esecuzione fa scaricare il file
# al browser. Niente WebDAV: l'archivio arriva solo qui.
#
#   ./script/backup-centralina.sh --centralina <alias> [--centralina <alias>] [--ferma] [--dest DIR]
#
# Riconosce da solo com'e' fatta la centralina:
#   - col controller: l'archivio e' quello del controller (/opt/docker_store senza
#     il kar degli addon, tmp e cache di OpenHAB, i backup locali e gli
#     exclude_paths di arfea.yml, come il database di deasy), nello stesso formato:
#     si ripristina dalla sua Web UI;
#   - OpenHAB nativo (openHABian, 2.x o successivi) o vecchio docker-compose:
#     un tar.gz coi percorsi assoluti di /etc (configurazione di OpenHAB, rete,
#     VPN, samba, mosquitto, cron), userdata di OpenHAB senza tmp/cache/kar e
#     senza i backup vecchi di openhab-cli, i jar manuali degli addon, /opt (con
#     /opt/docker_store), home, /root, /usr/local, /var/www, i retained di
#     mosquitto, i crontab e la configurazione di avvio; i database MariaDB/MySQL
#     in esecuzione come dump. Dentro, arfea-backup/ ha LEGGIMI.txt (cosa c'e' e
#     come si ripristina), i pacchetti installati, servizi, rete e dischi.
#
# Per default e' a caldo: niente si ferma, e l'archivio passa direttamente
# nell'ssh senza occupare spazio sulla centralina (come openhab-cli backup).
# Con --ferma, come il backup del controller, i servizi restano fermi mentre si
# crea l'archivio (OpenHAB nativo, i container del vecchio compose, oppure il
# backup del controller stesso, via API), che si scarica dopo il riavvio: serve
# spazio sulla centralina, e se manca non si ferma niente.
#
# Il file va in --dest, oppure nella cartella che passa lo Script Hub
# (HUB_DOWNLOAD_DIR), oppure in ~/Scaricati/arfea-backup.
###############################################################################
set -u

HOSTS=(); FERMA=false; DEST=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --centralina) [[ -n "${2:-}" ]] || { echo "--centralina vuole l'alias ssh"; exit 1; }; HOSTS+=("$2"); shift ;;
    --ferma) FERMA=true ;;
    --dest) [[ -n "${2:-}" ]] || { echo "--dest vuole una cartella"; exit 1; }; DEST=$2; shift ;;
    *) echo "opzione sconosciuta: $1"; exit 1 ;;
  esac
  shift
done
[[ ${#HOSTS[@]} -gt 0 ]] || { echo "uso: $0 --centralina <alias> [--centralina <alias>] [--ferma] [--dest DIR]"; exit 1; }
if [[ -z "$DEST" ]]; then
  DEST=${HUB_DOWNLOAD_DIR:-$(xdg-user-dir DOWNLOAD 2>/dev/null || echo "$HOME")/arfea-backup}
fi
mkdir -p "$DEST" || exit 1

# --------------------------------------------------------------------------- sulla centralina
# Gira da root. Su stdout passa SOLO l'archivio: tutto il resto va su stderr, e una
# riga "@@nome <file>" dice al PC come chiamarlo.
REMOTE=$(mktemp); ERR=$(mktemp)
trap 'rm -f "$REMOTE" "$ERR"' EXIT
cat >"$REMOTE" <<'REMOTE_EOF'
set -u
FERMA=$1; ALIAS=$2; TS=$3
# se la connessione cade a meta', lo script va avanti e i servizi fermati ripartono
trap '' HUP PIPE
exec 3>&1 1>&2
export LC_ALL=C
log() { echo "$*"; }

main() {
DATA=/opt/docker_store
YML=$DATA/arfea-controller/config/arfea.yml
[[ -f "$YML" ]] && DATA=$(awk '/^controller:/{c=1} c && /^[[:space:]]+data_path:/{gsub(/["'\'']/, "", $2); print $2; exit}' "$YML")
DATA=${DATA:-/opt/docker_store}
Z=(-z); command -v pigz >/dev/null && Z=(-I pigz)
TAROPT=(--anchored --ignore-failed-read --warning=no-file-changed --warning=no-file-removed)

oh=""
for s in openhab openhab2; do systemctl is-active --quiet "$s" && { oh=$s; break; }; done
layout=altro
if curl -s -m 5 -o /dev/null http://127.0.0.1:8888/api/health; then layout=controller
elif [[ -n "$oh" ]]; then layout=nativo
elif [[ -d $DATA/openhab ]] && command -v docker >/dev/null; then layout=compose
elif dpkg -l openhab openhab2 2>/dev/null | grep -q '^ii'; then layout=nativo
fi
log "$(hostname), $(. /etc/os-release; echo "$PRETTY_NAME"), $(tr -d '\0' </proc/device-tree/model 2>/dev/null || cat /sys/class/dmi/id/product_name 2>/dev/null)"
log "struttura: $layout$([[ -n $oh ]] && echo ", OpenHAB nativo attivo ($oh $(dpkg-query -W -f='${Version}' "$oh" 2>/dev/null))")"

# ---- col controller: il suo archivio
if [[ $layout == controller ]]; then
  status() {
    curl -s -m 10 http://127.0.0.1:8888/api/backup/status | python3 -c \
      'import json,sys; d=json.load(sys.stdin); print(d.get("state",""), d.get("started_at") or "-", d.get("archive") or "-", d.get("message","").replace("\n"," "), sep="\t")'
  }
  if $FERMA; then
    IFS=$'\t' read -r st t0 _ _ < <(status)
    case "$st" in idle|completed|failed) ;; *) log "ERRORE: il controller ha gia' un backup in corso ($st)"; return 2 ;; esac
    log "backup del controller (i container si fermano mentre crea l'archivio)..."
    curl -s -m 20 -X POST http://127.0.0.1:8888/api/backup/run >/dev/null || { log "ERRORE: il controller non accetta il backup"; return 2; }
    last=""; arch=""
    for _ in $(seq 1 720); do
      sleep 5
      IFS=$'\t' read -r st t1 arch msg < <(status)
      [[ "$st:$msg" != "$last" ]] && { log "  $st: $msg"; last="$st:$msg"; }
      [[ "$t1" != "$t0" && ( $st == completed || $st == failed ) ]] && break
    done
    # anche "failed" con l'archivio: e' il caricamento su WebDAV che non e' riuscito
    f="$DATA/arfea-controller/backups/$(basename "${arch:--}")"
    [[ "$arch" != "-" && -f "$f" ]] || { log "ERRORE: il controller non ha creato l'archivio"; return 2; }
    echo "@@nome $ALIAS-$(basename "$f")"
    log "archivio del controller: $(du -h "$f" | cut -f1), lo scarico"
    cat "$f" >&3
    return $?
  fi
  uuid=$(cat "$DATA/openhab/userdata/uuid" 2>/dev/null || echo unknown)
  EX=('openhab/addons/*.kar' 'openhab/addons/.*.kar.part' 'openhab/userdata/kar/*' 'openhab/userdata/tmp/*'
      'openhab/userdata/cache/*' 'openhab/userdata/core*' 'arfea-controller/backups' 'arfea-controller/backups/*')
  for p in $(awk '/^backup:/{b=1;next} b && /^[^[:space:]]/{b=0} b && /exclude_paths:/{e=1;next} b && e && /^[[:space:]]*- /{gsub(/["'\'']/, "", $2); print $2; next} b && e && !/^[[:space:]]*- /{e=0}' "$YML"); do
    [[ "$p" == "$DATA"/* ]] && EX+=("${p#"$DATA"/}" "${p#"$DATA"/}/*")
  done
  mapfile -t TOP < <(find "$DATA" -mindepth 1 -maxdepth 1 -printf '%f\n' | sort)
  X=(); for e in "${EX[@]}"; do X+=("--exclude=$e"); done
  tot=$(tar -C "$DATA" "${TAROPT[@]}" "${X[@]}" --totals -cf /dev/null "${TOP[@]}" 2>&1 | awk -F': ' '/Total bytes/{printf "%d", $2/1048576}')
  log "archivio come quello del controller, a caldo (i container restano accesi): circa ${tot:-?} MB da leggere"
  log "fuori, come nel controller: kar degli addon, tmp e cache di OpenHAB, backup locali, exclude_paths di arfea.yml"
  echo "@@nome $ALIAS-arfea-backup-$TS-$uuid.tar.gz"
  tar -C "$DATA" "${TAROPT[@]}" "${X[@]}" "${Z[@]}" -cf - "${TOP[@]}" >&3
  return $?
fi

# ---- OpenHAB nativo, vecchio compose, altro: percorsi assoluti
W=$(mktemp -d /var/tmp/arfea-backup.XXXXXX)
M=$W/arfea-backup; mkdir -p "$M"
cleanup() { rm -rf "$W"; }
STOPPED=(); RESTART=""
restart() {
  [[ -n "$RESTART" ]] || return 0
  log "riavvio: $RESTART"
  eval "$RESTART"; RESTART=""
}
trap 'restart; cleanup' EXIT

INC=()
for p in etc var/spool/cron var/www home root opt usr/local var/lib/mosquitto \
         var/lib/openhab var/lib/openhab2 usr/share/openhab/addons usr/share/openhab2/addons \
         var/lib/influxdb var/lib/grafana var/lib/mysql \
         boot/config.txt boot/cmdline.txt boot/armbianEnv.txt boot/firmware/config.txt \
         boot/firmware/cmdline.txt media/boot/boot.ini media/boot/config.ini; do
  [[ -e /$p ]] && INC+=("$p")
done
EX=('var/lib/openhab*/tmp/*' 'var/lib/openhab*/cache/*' 'var/lib/openhab*/kar/*' 'var/lib/openhab*/backups/*'
    'var/lib/openhab*/core*' 'usr/share/openhab*/addons/*.kar'
    "${DATA#/}/openhab/addons/*.kar" "${DATA#/}/openhab/addons/.*.kar.part" "${DATA#/}/openhab/userdata/kar/*"
    "${DATA#/}/openhab/userdata/tmp/*" "${DATA#/}/openhab/userdata/cache/*" "${DATA#/}/openhab/userdata/core*"
    "${DATA#/}/arfea-controller/backups/*" 'opt/docker_store-backup-*'
    opt/openhabian opt/vc opt/jdk opt/zram opt/habapp opt/containerd
    'home/*/.cache' root/.cache 'home/*/.npm' root/.npm 'home/*/.node-gyp' root/.node-gyp
    'home/*/.config/chromium' 'home/*/.local/share/Trash' 'home/*/MagPi' 'home/*/python_games'
    'home/*/.vscode-server' root/.vscode-server)
# dischi di rete e fuse montati dentro le cartelle del backup
while read -r tgt fs; do
  case "$fs" in cifs|smb*|nfs*|fuse*|sshfs|davfs) EX+=("${tgt#/}") ;; esac
done < <(findmnt -rn -o TARGET,FSTYPE)

# database in esecuzione: un dump, al posto dei file copiati a caldo
if pgrep -x mysqld >/dev/null || pgrep -x mariadbd >/dev/null; then
  d=$(command -v mariadb-dump || command -v mysqldump)
  if [[ -n "$d" ]] && ( set -o pipefail; "$d" --all-databases --single-transaction --routines --events | gzip >"$M/db-nativo.sql.gz" ); then
    log "database nativo: dump ($(du -h "$M/db-nativo.sql.gz" | cut -f1)), i file di /var/lib/mysql restano fuori"
    EX+=(var/lib/mysql)
  else
    log "ATTENZIONE: dump del database nativo non riuscito, i file di /var/lib/mysql sono copiati a caldo"
  fi
fi
if command -v docker >/dev/null; then
  for c in $(docker ps --format '{{.Names}} {{.Image}}' | awk '$2 ~ /(mariadb|mysql)/ {print $1}'); do
    if ( set -o pipefail; docker exec "$c" sh -c 'p=${MARIADB_ROOT_PASSWORD:-${MYSQL_ROOT_PASSWORD:-}}; d=$(command -v mariadb-dump || command -v mysqldump); exec "$d" -uroot ${p:+-p"$p"} --all-databases --single-transaction --routines --events' 2>/dev/null \
         | gzip >"$M/db-$c.sql.gz" ) && [[ $(stat -c %s "$M/db-$c.sql.gz") -gt 1024 ]]; then
      log "database nel container $c: dump ($(du -h "$M/db-$c.sql.gz" | cut -f1)), i suoi file restano fuori"
      for s in $(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/var/lib/mysql"}}{{.Source}}{{end}}{{end}}' "$c"); do
        EX+=("${s#/}")
      done
    else
      rm -f "$M/db-$c.sql.gz"
      log "ATTENZIONE: dump del database nel container $c non riuscito, i suoi file sono copiati a caldo (con --ferma sono fermi)"
    fi
  done
fi

# manifest: cosa c'era sulla centralina, per rimetterla in piedi
{
  dpkg --get-selections
} >"$M/pacchetti.txt" 2>/dev/null
apt-mark showmanual >"$M/pacchetti-manuali.txt" 2>/dev/null
{ systemctl list-unit-files --type=service --state=enabled --no-legend; echo; echo "# in esecuzione"
  systemctl list-units --type=service --state=running --no-legend --plain; } >"$M/servizi.txt" 2>/dev/null
{ ip addr; echo; ip route; } >"$M/rete.txt" 2>/dev/null
{ df -h; echo; lsblk; echo; findmnt -r; } >"$M/dischi.txt" 2>/dev/null
command -v docker >/dev/null && { docker ps -a; echo; docker images; echo; docker volume ls; } >"$M/docker.txt" 2>/dev/null
command -v npm >/dev/null && npm ls -g --depth=0 >"$M/npm-globali.txt" 2>/dev/null
mode="a caldo (niente fermato)"; $FERMA && mode="con i servizi fermi durante l'archivio"
cat >"$M/LEGGIMI.txt" <<EOF
Backup di $ALIAS ($(hostname)), $TS (ora del PC), $mode.
Sistema: $(. /etc/os-release; echo "$PRETTY_NAME"), $(uname -m), kernel $(uname -r)
$(tr -d '\0' </proc/device-tree/model 2>/dev/null || cat /sys/class/dmi/id/product_name 2>/dev/null)
Struttura: $layout. OpenHAB: $(for s in openhab openhab2; do v=$(dpkg-query -W -f='${Version}' "$s" 2>/dev/null) && echo -n "$s $v ($(systemctl is-active "$s")) "; done)
Percorsi nell'archivio: assoluti, senza la / iniziale.

Dentro: ${INC[*]}
Fuori: ${EX[*]}
Non ci sono neanche i log (/var/log), il Java di openHABian (/opt/jdk) e il venv di HABApp
(/opt/habapp), che si reinstallano.

Ripristino di OpenHAB su una centralina nativa:
  sudo systemctl stop openhab2        (o openhab)
  sudo tar -xzpf ARCHIVIO -C / etc/openhab2 var/lib/openhab2 usr/share/openhab2/addons
  sudo systemctl start openhab2
Un file o una cartella: sudo tar -xzpf ARCHIVIO -C /tmp etc/hostapd/hostapd.conf
Su un sistema nuovo /etc non si ripristina intero: si prendono i file che servono
(rete, VPN, samba, crontab in var/spool/cron) e si reinstallano i pacchetti di
arfea-backup/pacchetti-manuali.txt. I database sono in arfea-backup/db-*.sql.gz:
  zcat db-nativo.sql.gz | sudo mysql
L'archivio contiene segreti (chiavi della VPN, /etc/shadow, chiavi ssh): non va condiviso.
EOF

X=(); for e in "${EX[@]}"; do X+=("--exclude=$e"); done
tot=$(tar -C / "${TAROPT[@]}" "${X[@]}" --totals -cf /dev/null "${INC[@]}" 2>&1 | awk -F': ' '/Total bytes/{printf "%d", $2/1048576}')
log "da archiviare: circa ${tot:-?} MB, $mode"
echo "@@nome $ALIAS-backup-$TS.tar.gz"

if ! $FERMA; then
  tar "${TAROPT[@]}" "${X[@]}" "${Z[@]}" -cf - -C / "${INC[@]}" -C "$W" arfea-backup >&3
  return $?
fi

# --ferma: archivio sulla centralina coi servizi fermi, poi si riavvia e si scarica
free=$(df -Pm "$W" | awk 'NR==2{print $4}')
if [[ -z "$tot" || $free -lt $((tot + 200)) ]]; then
  log "ERRORE: con --ferma l'archivio si crea sulla centralina, e in /var/tmp ci sono $free MB per circa ${tot:-?} MB: non fermo niente. Usa il backup a caldo"
  return 2
fi
# come il controller: si ferma tutto quello che scrive i dati (OpenHAB nativo e i
# container, anche quelli accanto a un OpenHAB nativo), e riparte com'era
if [[ -n "$oh" ]]; then
  log "fermo $oh"; systemctl stop "$oh"; RESTART="systemctl start $oh"
fi
command -v docker >/dev/null && mapfile -t STOPPED < <(docker ps -q)
if [[ ${#STOPPED[@]} -gt 0 ]]; then
  log "fermo ${#STOPPED[@]} container"; docker stop -t 30 "${STOPPED[@]}" >/dev/null
  RESTART="${RESTART:+$RESTART; }docker start ${STOPPED[*]} >/dev/null"
fi
tar "${TAROPT[@]}" "${X[@]}" "${Z[@]}" -cf "$W/archivio.tar.gz" -C / "${INC[@]}" -C "$W" arfea-backup
rc=$?
restart
[[ $rc -le 1 ]] || { log "ERRORE: tar ha finito con $rc"; return $rc; }
log "archivio: $(du -h "$W/archivio.tar.gz" | cut -f1), servizi ripartiti, lo scarico"
cat "$W/archivio.tar.gz" >&3
}

main </dev/null
REMOTE_EOF

# --------------------------------------------------------------------------- sul PC
rc_all=0
for h in "${HOSTS[@]}"; do
  echo "=================== $h"
  part="$DEST/.$h-$(date +%s).part"
  t0=$(date +%s); ts=$(date +%Y-%m-%d_%H%M)
  ssh -o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=30 "$h" \
      "sudo bash -s -- $FERMA $h $ts" <"$REMOTE" >"$part" 2>"$ERR" &
  pid=$!
  # le righe nuove della centralina, senza il nome del file e gli avvisi di ssh
  show() {
    sed -n "$((shown + 1)),\$p" "$ERR" | grep -vE '^@@|post-quantum|store now, decrypt later|openssh.com/pq.html' | sed 's/^/  /'
    shown=$(wc -l <"$ERR")
  }
  shown=0; tick=0
  while kill -0 "$pid" 2>/dev/null; do
    sleep 2
    show
    tick=$((tick + 1))
    [[ $((tick % 15)) -eq 0 && -s "$part" ]] && echo "  ... scaricati $(du -m "$part" | cut -f1) MB"
  done
  wait "$pid"; rc=$?
  show
  name=$(sed -n 's/^@@nome //p' "$ERR" | tail -1 | tr '/' '_')
  # tar: 1 = qualche file e' cambiato mentre lo leggeva (a caldo succede), non un errore
  if [[ $rc -gt 1 || ! -s "$part" ]]; then
    echo "$h: backup NON riuscito (codice $rc)"; rm -f "$part"; rc_all=1; continue
  fi
  if ! gzip -t "$part" 2>/dev/null; then
    echo "$h: archivio arrivato rovinato (la connessione e' caduta?)"; rm -f "$part"; rc_all=1; continue
  fi
  files=$(tar -tzf "$part" 2>/dev/null | wc -l)
  out="$DEST/${name:-$h-backup-$ts.tar.gz}"
  mv "$part" "$out"
  echo "$h: $(du -h "$out" | cut -f1), $files voci, in $(( $(date +%s) - t0 )) s -> $out"
done
[[ -n "${HUB_DOWNLOAD_DIR:-}" && $rc_all -eq 0 ]] && echo "Il browser lo scarica da solo; il link resta qui sotto per 7 giorni."
exit $rc_all
