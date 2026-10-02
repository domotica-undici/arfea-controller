#!/bin/bash
###############################################################################
# deasy-to-docker.sh — porta Undici (deasy) da nativo a docker (Redmine #280)
#
# Undici nativo = undici.service (Java) + lighttpd/PHP sulla porta 80 + MariaDB,
# nativa o in un container. Qui diventa il compose di /opt/docker_store/deasy
# (container deasy, mariadb, autoheal) sulla rete "domotica" del controller, col
# modello di un impianto dove gira gia' in docker: Dockerfile, compose, lighttpd,
# libreria RXTX, Java a 32 bit e watchdog stanno nel KIT, i dati dell'impianto
# (/opt/undici, /etc/undici, /var/www, database) si prendono da qui.
#
# Uso (da root, sulla centralina):
#   bash deasy-to-docker.sh [--kit DIR] [--serial DEV]            # prova a vuoto
#   bash deasy-to-docker.sh [--kit DIR] [--serial DEV] --apply    # esegue
#
#   --kit     cartella col kit (default /root/deasy-kit): Dockerfile,
#             docker-compose.yml, lighttpd.conf, librxtxSerial-2.2pre1.so,
#             serial_write_9600.py, host-setup.sh, zulu*.tar.gz, watchdog.py,
#             undici-watchdog.service. Si copia da un impianto di riferimento
#             (vedi il catalogo dello Script Hub).
#   --serial  seriale della XBee sull'host (default: spider.port di
#             /opt/undici/undici.properties). Un ttyUSB/ttyACM si mappa per
#             /dev/serial/by-id, col nome di undici.properties dentro il container.
#   --db-data cartella dati (/var/lib/mysql) di una MariaDB in container che non
#             c'e' piu' (tolta per esempio da "docker system prune -a"): si usa
#             quella al posto del container di db.host. --db-store: la sua
#             /home/store, se c'era.
#
# L'immagine deasy-deasy va caricata prima (docker load da un impianto di
# riferimento); senza, "docker compose up" la costruisce dal Dockerfile del kit
# (serve internet e ci vuole tempo).
#
# Fermo di Undici: dallo stop del nativo all'avvio del container, un minuto o
# due. Niente si cancella: dati nativi in /root/undici-nativo-*.tar.gz, database
# in /root/undici-db-*.sql o /root/mariadb-prima-deasy-*.tar.gz, e alla fine
# stampa come tornare indietro.
###############################################################################
set -euo pipefail

KIT=/root/deasy-kit
SERIAL=""
DB_DATA="" DB_STORE=""
APPLY=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --kit) KIT="$2"; shift 2 ;;
    --serial) SERIAL="$2"; shift 2 ;;
    --db-data) DB_DATA="$2"; shift 2 ;;
    --db-store) DB_STORE="$2"; shift 2 ;;
    --apply) APPLY=true; shift ;;
    -h|--help) sed -n '2,37p' "$0"; exit 0 ;;
    *) echo "argomento sconosciuto: $1" >&2; exit 2 ;;
  esac
done

DATA_PATH=/opt/docker_store
D="$DATA_PATH/deasy"
PROPS=/opt/undici/undici.properties
YML="$DATA_PATH/arfea-controller/config/arfea.yml"
ts=$(date +%Y%m%d_%H%M%S)

log()  { echo "$(date +%T) $*"; }
die()  { echo "ERRORE: $*" >&2; exit 1; }
prop() { python3 -c "
import sys
for l in open('$PROPS'):
    l = l.strip()
    if l.startswith(sys.argv[1] + '=') or l.startswith(sys.argv[1] + ' ='):
        print(l.split('=', 1)[1].strip()); break" "$1"; }

# ── Controlli: niente si tocca finché non passano tutti ──────────────────────
[[ $EUID -eq 0 ]] || die "va lanciato da root"
command -v docker >/dev/null && docker info >/dev/null 2>&1 || die "Docker non disponibile"
docker compose version >/dev/null 2>&1 || die "manca docker compose"
python3 -c "import yaml" 2>/dev/null || die "manca python3-yaml"
[[ -f "$PROPS" ]] || die "$PROPS non trovato: Undici nativo non c'e'"
[[ -d /etc/undici && -d /var/www ]] || die "/etc/undici o /var/www mancanti"
# $D che esiste gia': il controllo e' piu' sotto, dopo il database (ammessi solo
# i dati della MariaDB di Undici lasciati li' da uno stack di Portainer, #316)
for f in Dockerfile docker-compose.yml lighttpd.conf librxtxSerial-2.2pre1.so serial_write_9600.py watchdog.py undici-watchdog.service; do
  [[ -f "$KIT/$f" ]] || die "kit incompleto: manca $KIT/$f"
done

[[ -n "$SERIAL" ]] || SERIAL=$(prop spider.port)
[[ -n "$SERIAL" ]] || die "seriale della XBee non trovata (spider.port): passala con --serial"
IN_CT="$(prop spider.port)"; IN_CT="${IN_CT:-$SERIAL}"
HOST_DEV="$SERIAL"
if [[ "$SERIAL" =~ ^/dev/tty(USB|ACM)[0-9]+$ && -d /dev/serial/by-id ]]; then
  for l in /dev/serial/by-id/*; do
    [[ "$(readlink -f "$l")" == "$SERIAL" ]] && { HOST_DEV="$l"; break; }
  done
fi
[[ -e "$(readlink -f "$HOST_DEV")" ]] || die "la seriale $HOST_DEV non c'e'"

DB_HOST=$(prop db.host); DB_NAME=$(prop db.name); DB_USER=$(prop db.user); DB_PW=$(prop db.password)
[[ -n "$DB_NAME" && -n "$DB_PW" ]] || die "db.name o db.password mancanti in $PROPS"
[[ "${DB_USER:-root}" == root ]] || die "Undici usa l'utente $DB_USER e non root: adatta a mano"

# Database: MariaDB nativa, oppure il container con quell'IP o quel nome, oppure
# un container in rete host raggiunto a un indirizzo dell'host (su un impianto
# db.host=172.17.0.1, il gateway di docker0, con MariaDB in rete host)
DB_MODE="" OLD_CT="" OLD_DATA="" OLD_STORE=""
host_ip() { [[ "$1" == localhost || "$1" == 127.0.0.1 ]] || ip -o addr show 2>/dev/null | grep -q " inet $1/"; }
mysql_host_ct() {
  local c
  for c in $(docker ps --format '{{.Names}}'); do
    [[ "$(docker inspect -f '{{.HostConfig.NetworkMode}}' "$c")" == host ]] || continue
    docker inspect -f '{{range .Mounts}}{{println .Destination}}{{end}}' "$c" | grep -qx /var/lib/mysql && { echo "$c"; return 0; }
  done
  return 0
}
if [[ -n "$DB_DATA" ]]; then
  # il container non c'e' piu': restano i suoi dati, che nessun container deve usare
  [[ -d "$DB_DATA/mysql" ]] || die "--db-data $DB_DATA: non e' una cartella dati di MariaDB (manca mysql/)"
  [[ -z "$DB_STORE" || -d "$DB_STORE" ]] || die "--db-store $DB_STORE non c'e'"
  for c in $(docker ps -aq); do
    docker inspect -f '{{range .Mounts}}{{println .Source}}{{end}}' "$c" | grep -qx "$(readlink -f "$DB_DATA")" \
      && die "--db-data: $DB_DATA e' montata dal container $(docker inspect -f '{{.Name}}' "$c")"
  done
  OLD_DATA=$(readlink -f "$DB_DATA") OLD_STORE=${DB_STORE:+$(readlink -f "$DB_STORE")}
  DB_MODE=container
elif [[ "$DB_HOST" == localhost || "$DB_HOST" == 127.0.0.1 ]] \
   && { systemctl is-active --quiet mariadb || systemctl is-active --quiet mysql; }; then
  DB_MODE=native
else
  # Anche per l'IP fisso di un container fermo, e solo se monta /var/lib/mysql: su un
  # impianto la MariaDB (172.11.0.2 fisso) non era ripartita dopo un riavvio perche'
  # quell'IP l'aveva preso mosquitto, che qui sarebbe passato per il database
  for c in $(docker ps -a --format '{{.Names}}'); do
    ips=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{with .IPAMConfig}}{{.IPv4Address}}{{end}} {{end}}' "$c" 2>/dev/null || true)
    [[ "$c" == "$DB_HOST" || " $ips " == *" $DB_HOST "* ]] || continue
    docker inspect -f '{{range .Mounts}}{{println .Destination}}{{end}}' "$c" | grep -qx /var/lib/mysql || continue
    OLD_CT="$c"; break
  done
  if [[ -z "$OLD_CT" ]] && host_ip "$DB_HOST"; then OLD_CT=$(mysql_host_ct); fi
  [[ -n "$OLD_CT" ]] || die "db.host=$DB_HOST: nessun container con quel nome o IP, ne' MariaDB nativa attiva (container tolto? --db-data)"
  OLD_DATA=$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/var/lib/mysql"}}{{.Source}}{{end}}{{end}}' "$OLD_CT")
  [[ -n "$OLD_DATA" && -d "$OLD_DATA" ]] || die "cartella dati di $OLD_CT non trovata (mount di /var/lib/mysql)"
  OLD_STORE=$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/home/store"}}{{.Source}}{{end}}{{end}}' "$OLD_CT")
  DB_MODE=container
fi

# $D non deve esistere, tranne quando contiene solo i dati del container MariaDB
# di Undici (su un impianto uno stack di Portainer li teneva gia' in
# $D/mariadb/database): restano dove sono
DATA_IN_PLACE=false
if [[ -e "$D" ]]; then
  [[ "$(ls -A "$D")" == mariadb && "$(readlink -f "$OLD_DATA" 2>/dev/null)" == "$D/mariadb/database" ]] \
    || die "$D esiste gia': Undici e' gia' in docker?"
  DATA_IN_PLACE=true
fi
# Il compose del kit chiama i suoi container deasy, mariadb e autoheal: un
# container vecchio con lo stesso nome (la MariaDB di Undici si chiamava spesso
# "mariadb") si rinomina, e resta fermo per tornare indietro
OLD_CT_KEEP="$OLD_CT"
[[ "$OLD_CT" =~ ^(deasy|mariadb|autoheal)$ ]] && OLD_CT_KEEP="$OLD_CT-prima-deasy"
for c in deasy autoheal; do
  docker inspect "$c" >/dev/null 2>&1 && die "c'e' gia' un container $c: Undici e' gia' in docker?"
done

NET=domotica
[[ -f "$YML" ]] && NET=$(awk '/^network:/ {f=1; next} f && /^[^[:space:]]/ {f=0}
                              f && /^[[:space:]]+name:/ {gsub(/["[:space:]]/, "", $2); print $2; exit}' "$YML")
NET="${NET:-domotica}"
IMG_OK=false; docker image inspect deasy-deasy:latest >/dev/null 2>&1 && IMG_OK=true

echo "════════════════════════════════════════════════════════════"
echo "   Undici nativo → docker ($D)"
echo "════════════════════════════════════════════════════════════"
echo "  kit:        $KIT"
echo "  XBee:       $HOST_DEV → $IN_CT (nel container)"
echo "  database:   $DB_NAME, $( [[ $DB_MODE == native ]] && echo "MariaDB nativa: dump e import" || echo "${OLD_CT:+container $OLD_CT, }dati in $OLD_DATA: $($DATA_IN_PLACE && echo "restano dove sono" || echo spostati)" )"
[[ "$OLD_CT_KEEP" != "$OLD_CT" ]] && echo "              $OLD_CT fermato e rinominato $OLD_CT_KEEP (il kit usa lo stesso nome)"
echo "  rete:       $NET $(docker network inspect "$NET" >/dev/null 2>&1 && echo "(esiste)" || echo "(da creare, 172.11.0.0/24)")"
echo "  immagine:   deasy-deasy $($IMG_OK && echo "presente" || echo "ASSENTE: verra' costruita dal Dockerfile (internet, tempo)")"
echo "  nativo:     undici e lighttpd fermati e disabilitati$( [[ $DB_MODE == native ]] && echo ", MariaDB nativa anche" ); cron che riaccendono Undici nativo commentati"
echo "  watchdog:   undici-watchdog.service col log del container"
[[ -f "$YML" ]] && echo "  backup:     $D/mariadb/database escluso dal backup del controller"
echo ""
if ! $APPLY; then echo "Prova a vuoto: nulla e' stato toccato. Per eseguire: --apply"; exit 0; fi

# ── 1. Cartella deasy (a Undici ancora acceso) ───────────────────────────────
log "[1/6] cartella $D"
tar -czf "/root/undici-nativo-$ts.tar.gz" /opt/undici /etc/undici /var/www 2>/dev/null
log "  dati nativi salvati in /root/undici-nativo-$ts.tar.gz"
mkdir -p "$D"/{opt,etc,var} "$D"/mariadb/{backup,store}
for f in Dockerfile docker-compose.yml lighttpd.conf librxtxSerial-2.2pre1.so serial_write_9600.py host-setup.sh; do
  [[ -f "$KIT/$f" ]] && cp -p "$KIT/$f" "$D/"
done
cp -p "$KIT"/zulu*.tar.gz "$D/" 2>/dev/null || log "  (nessun Java nel kit: servirebbe solo per ricostruire l'immagine)"
cp -a /opt/undici "$D/opt/undici"
cp -a /etc/undici "$D/etc/undici"
cp -a /var/www "$D/var/www"
# il database per nome del container, non per IP o localhost
sed -i -E 's/^db\.host[[:space:]]*=.*/db.host=mariadb/' "$D/opt/undici/undici.properties"
[[ -f "$D/var/www/include/config.php" ]] && sed -i -E 's/^\$dbhost = .*/$dbhost = "mariadb";/' "$D/var/www/include/config.php"

python3 - "$D/docker-compose.yml" "$HOST_DEV" "$IN_CT" "$DB_PW" "$NET" <<'PY'
import re, sys, yaml
cf, host_dev, in_ct, pw, net = sys.argv[1:6]
s = open(cf).read()
s = re.sub(r"(?m)^# NOTA sulla seriale:\n(#.*\n)*",
           "# NOTA sulla seriale: la XBee dell'impianto, col nome che usa undici.properties.\n", s)
s = re.sub(r"(?m)^\s*# Path stabile via udev.*\n", "", s)
lines = s.splitlines(keepends=True)
# la prima seriale della sezione devices del servizio deasy
out, in_deasy, in_dev, done = [], False, False, False
for l in lines:
    if re.match(r"^  deasy:\s*$", l): in_deasy = True
    elif re.match(r"^  \S", l): in_deasy = False
    if in_deasy and re.match(r"^    devices:\s*$", l): in_dev = True
    elif in_dev and re.match(r"^    \S", l): in_dev = False
    if in_dev and not done and re.match(r"^\s+- /dev/\S+:/dev/\S+", l):
        ind = re.match(r"^(\s+)", l).group(1)
        l = f"{ind}- {host_dev}:{in_ct}\n"; done = True
    out.append(l)
assert done, "seriale del servizio deasy non trovata nel compose del kit"
s = "".join(out)
# /etc/undici dell'impianto sopra quella dell'immagine (credenziali dell'unita')
if "./etc/undici:/etc/undici" not in s:
    s, n = re.subn(r"(?m)^(\s*)- \./serial_write_9600\.py:/opt/undici/serial_write_9600\.py\s*$",
                  r"\g<0>\n\1- ./etc/undici:/etc/undici:rw", s)
    assert n == 1, "volume serial_write_9600.py non trovato nel compose del kit"
for k in ("DEASY_DB_PASSWORD", "MARIADB_PASSWORD", "MARIADB_ROOT_PASSWORD"):
    s = re.sub(rf"(?m)^(\s*{k}:\s*).*$", lambda m: m.group(1) + pw, s)
d = yaml.safe_load(s)
ext = [k for k, v in (d.get("networks") or {}).items() if isinstance(v, dict) and v.get("external")]
for e in ext:
    if e != net:
        s = re.sub(rf"(^|[^A-Za-z0-9_-]){re.escape(e)}([^A-Za-z0-9_-]|$)", rf"\g<1>{net}\g<2>", s, flags=re.M)
d = yaml.safe_load(s)
# Un servizio senza rete (autoheal nel kit) farebbe creare a compose la rete
# deasy_default: sul kernel 4.9 con iptables-nft la creazione fallisce
# (RULE_INSERT failed) e deasy non parte, col nativo gia' fermo (#316). Va
# anche lui sulla rete del controller, che esiste gia'.
for name, svc in (d.get("services") or {}).items():
    if not svc.get("networks") and not svc.get("network_mode"):
        s, k = re.subn(rf"(?m)^(  {re.escape(name)}:\n(?:    .*\n|\s*\n)*?)(    volumes:\n)",
                       rf"\g<1>    networks:\n      - {net}\n\g<2>", s)
        assert k == 1, f"servizio {name} senza rete e senza volumes: mettilo a mano sulla rete {net}"
d = yaml.safe_load(s)
assert all(v.get("networks") or v.get("network_mode") for v in d["services"].values()), "un servizio e' senza rete"
assert d["services"]["deasy"]["devices"][0] == f"{host_dev}:{in_ct}"
assert d["services"]["mariadb"]["environment"]["MARIADB_ROOT_PASSWORD"] == pw
assert (d.get("networks") or {}).get(net, {}).get("external") is True, f"il compose non usa la rete {net}"
open(cf, "w").write(s)
print(f"  compose: seriale {host_dev}:{in_ct}, /etc/undici montata, credenziali del database, rete {net}")
PY
chmod 600 "$D/docker-compose.yml"
docker network inspect "$NET" >/dev/null 2>&1 \
  || { docker network create --driver bridge --subnet 172.11.0.0/24 --gateway 172.11.0.1 "$NET" >/dev/null; log "  rete $NET creata"; }

# ── 2. Stop del nativo ───────────────────────────────────────────────────────
log "[2/6] stop di Undici nativo"
# check_undici.sh o undici_whatchdog.sh (a seconda dell'impianto): riaccendono
# undici.service, che col container si prenderebbe la seriale della XBee
CRON_RE='check_undici|undici_wh?atchdog'
if crontab -l 2>/dev/null | grep -qE "^[^#].*($CRON_RE)"; then
  crontab -l > "/root/crontab-prima-deasy-$ts"
  crontab -l | sed -E "s@^([^#].*($CRON_RE).*)\$@#\\1  # Undici in docker (deasy)@" | crontab -
  log "  cron di Undici nativo commentati (copia in /root/crontab-prima-deasy-$ts)"
fi
systemctl stop undici lighttpd 2>/dev/null || true
systemctl disable undici lighttpd >/dev/null 2>&1 || true
sleep 2
pgrep -f Undici.jar >/dev/null && die "Undici.jar ancora in esecuzione"
ss -ltn | grep -q ':80 ' && die "porta 80 ancora occupata"

# ── 3. Database ──────────────────────────────────────────────────────────────
log "[3/6] database"
if [[ $DB_MODE == native ]]; then
  MYSQL_PWD="$DB_PW" mysqldump -uroot --databases "$DB_NAME" --routines --triggers --events --single-transaction \
    > "/root/undici-db-$ts.sql"
  log "  dump in /root/undici-db-$ts.sql ($(du -h "/root/undici-db-$ts.sql" | cut -f1))"
  for u in mariadb mysql; do systemctl stop "$u" 2>/dev/null || true; systemctl disable "$u" >/dev/null 2>&1 || true; done
  log "  MariaDB nativa fermata e disabilitata (i dati restano in /var/lib/mysql)"
else
  if [[ -n "$OLD_CT" ]]; then
    docker update --restart=no "$OLD_CT" >/dev/null
    docker stop "$OLD_CT" >/dev/null
  fi
  if [[ "$OLD_CT_KEEP" != "$OLD_CT" ]]; then
    docker rename "$OLD_CT" "$OLD_CT_KEEP"
    log "  $OLD_CT rinominato $OLD_CT_KEEP (il compose del kit usa lo stesso nome)"
  fi
  tar -czf "/root/mariadb-prima-deasy-$ts.tar.gz" -C "$(dirname "$OLD_DATA")" "$(basename "$OLD_DATA")"
  # la cartella intera (stesso disco: proprietari e file nascosti restano)
  $DATA_IN_PLACE || mv "$OLD_DATA" "$D/mariadb/database"
  if [[ -n "$OLD_STORE" && -d "$OLD_STORE" && "$(readlink -f "$OLD_STORE")" != "$D/mariadb/store" ]]; then
    cp -a "$OLD_STORE"/. "$D/mariadb/store"/
  fi
  log "  ${OLD_CT:+$OLD_CT fermato, }dati $($DATA_IN_PLACE && echo "lasciati in $OLD_DATA" || echo spostati) (copia in /root/mariadb-prima-deasy-$ts.tar.gz)"
fi

# ── 4. Container ─────────────────────────────────────────────────────────────
log "[4/6] container"
cd "$D"
docker compose up -d mariadb 2>&1 | tail -2
# Una query autenticata, non "mysqladmin ping": quello risponde anche al server
# temporaneo dell'inizializzazione, prima che la password di root sia impostata.
ok=false
for _ in $(seq 1 90); do
  docker exec -e MYSQL_PWD="$DB_PW" mariadb mysql -uroot -N -e "select 1" >/dev/null 2>&1 && { ok=true; break; }
  sleep 2
done
$ok || die "MariaDB del container non accetta la password di Undici: controlla (cd $D && docker compose logs mariadb)"
if [[ $DB_MODE == native ]]; then
  docker exec -i -e MYSQL_PWD="$DB_PW" mariadb mysql -uroot < "/root/undici-db-$ts.sql"
fi
n=$(docker exec -e MYSQL_PWD="$DB_PW" mariadb mysql -uroot -N -e "select count(*) from information_schema.tables where table_schema='$DB_NAME'")
log "  database $DB_NAME: $n tabelle"
docker compose up -d 2>&1 | tail -3
if [[ -n "$OLD_CT" ]]; then docker rm "$OLD_CT_KEEP" >/dev/null 2>&1 && log "  container $OLD_CT_KEEP tolto" || true; fi

# ── 5. Watchdog e backup ─────────────────────────────────────────────────────
log "[5/6] watchdog e backup"
cp "$KIT/watchdog.py" "$D/watchdog.py"
sed -i -E "s|^LOG_FILE = .*|LOG_FILE = \"$D/opt/undici/log/log\"|; s|^WATCHDOG_LOG = .*|WATCHDOG_LOG = \"$D/opt/undici/log/watchdog.log\"|" "$D/watchdog.py"
sed -E "s|^ExecStart=.*|ExecStart=/usr/bin/python3 $D/watchdog.py|" "$KIT/undici-watchdog.service" > /etc/systemd/system/undici-watchdog.service
systemctl daemon-reload
systemctl enable --now undici-watchdog >/dev/null 2>&1 && log "  undici-watchdog attivo"
if [[ -f "$YML" ]] && ! grep -qF "$D/mariadb/database" "$YML"; then
  python3 - "$YML" "$D/mariadb/database" <<'PY' && docker restart arfea-controller >/dev/null 2>&1 && log "  controller riavviato (rilegge arfea.yml)" || true
import re, sys, yaml
p, db = sys.argv[1:3]
lines = open(p).read().splitlines(keepends=True)
for i, l in enumerate(lines):
    if re.match(r"^  exclude_paths:\s*$", l):
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        ind = re.match(r"^(\s*)- ", nxt).group(1) if re.match(r"^\s*- ", nxt) else "    "
        lines.insert(i + 1, f'{ind}- "{db}"\n')
        new = "".join(lines)
        assert db in (yaml.safe_load(new)["backup"]["exclude_paths"] or [])
        open(p, "w").write(new)
        print(f"  {db} escluso dal backup del controller (il database si salva a mano)")
        sys.exit(0)
sys.exit(1)
PY
fi

# ── 6. Verifica ──────────────────────────────────────────────────────────────
log "[6/6] verifica"
st=""
for _ in $(seq 1 40); do
  st=$(docker inspect -f '{{.State.Health.Status}}' deasy 2>/dev/null || true)
  [[ "$st" == healthy ]] && break; sleep 5
done
log "  deasy: ${st:-?} | login.php $(curl -s -o /dev/null -w '%{http_code}' http://localhost/login.php)"
grep -aE "Coordinator Operative Pan ID:" "$D/opt/undici/log/log" | tail -1 | sed 's/^/  XBee: /' || true

cat <<EOF

Fatto. Controlla il log di Undici ($D/opt/undici/log/log) e il ponte Node-RED.
Per tornare indietro:
  cd $D && docker compose down
EOF
if [[ $DB_MODE == native ]]; then
  echo "  systemctl enable --now mariadb undici lighttpd"
else
  $DATA_IN_PLACE || echo "  mv $D/mariadb/database $OLD_DATA"
  echo "  # e ricrea ${OLD_CT:-la MariaDB} come prima (dal suo compose, o con docker run)"
  echo "  systemctl enable --now undici lighttpd"
fi
[[ -f "/root/crontab-prima-deasy-$ts" ]] && echo "  crontab /root/crontab-prima-deasy-$ts"
exit 0
