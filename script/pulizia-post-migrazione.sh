#!/bin/bash
###############################################################################
# Pulizia di una centralina dopo la migrazione al controller, quando l'impianto
# e' confermato in esercizio. Gira sulla centralina, da root.
#
#   sudo bash pulizia-post-migrazione.sh              # prova a vuoto: dice cosa toglierebbe
#   sudo bash pulizia-post-migrazione.sh --apply      # toglie davvero
#   sudo bash pulizia-post-migrazione.sh --apply --nativo --pacchetti --undici-nativo
#
# Dal PC (o dallo Script Hub), su piu' centraline: lo script si manda da solo via
# ssh a ognuna, con le stesse opzioni; una centralina che non risponde non ferma le altre.
#   ./script/pulizia-post-migrazione.sh --centralina <alias> --centralina <alias> [--apply] [...]
#
# Toglie:
#   - i backup pre-migrazione /opt/docker_store-backup-*.tar.gz;
#   - le immagini docker che nessun container usa e che arfea.yml non nomina
#     (restano arfea-controller e python:3.11-slim, che serve al rebuild OTA);
#   - sorgenti (/usr/src/linux-*) e initrd (/boot) di kernel non piu' installati
#     che nessun pacchetto possiede, cache di root e degli utenti;
#   - la vecchia "execpipe" (/opt/mypipe: esegue da root quello che ci si scrive)
#     se nessun container la monta piu';
#   - cache apt, journal oltre 200 MB, log ruotati;
#   - con --nativo: OpenHAB/HABApp/frontail nativi (pacchetti senza purge,
#     /etc/openhab, /var/lib/openhab, /usr/share/openhab, /var/log/openhab,
#     /opt/habapp) e i mount /srv/openhab-* di openHABian. Dopo, tornare al
#     nativo non e' piu' possibile;
#   - con --pacchetti: i pacchetti che al controller non servono, ognuno solo se
#     la sua condizione regge (OpenJDK se niente di nativo usa Java, linux-firmware
#     col kernel Hardkernel 4.9 senza wifi/BT, nodejs, samba, ModemManager senza
#     modem, compilatori senza dkms, nginx spento), con le dipendenze rimaste
#     orfane. Se apt volesse togliere anche altro, la rimozione non parte;
#   - con --undici-nativo, solo se il container deasy gira ed e' sano: Undici
#     nativo (lighttpd, PHP, RXTX, Java Zulu, /opt/undici,
#     /etc/undici, /var/www, undici.service, i dati della vecchia MariaDB in
#     container rimasti fuori da deasy).
# Non tocca: /root (dump del database di deasy, che il backup del controller
# esclude, e archivi di Undici), deasy in docker, i backup del controller, i
# volumi docker. Non parte durante un backup o un aggiornamento di versione.
###############################################################################
set -u

APPLY=false; NATIVO=false; PACCHETTI=false; UNDICI=false; HOSTS=(); FWD=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) APPLY=true; FWD+=(--apply) ;;
    --nativo) NATIVO=true; FWD+=(--nativo) ;;
    --pacchetti) PACCHETTI=true; FWD+=(--pacchetti) ;;
    --undici-nativo) UNDICI=true; FWD+=(--undici-nativo) ;;
    --centralina) [[ -n "${2:-}" ]] || { echo "--centralina vuole l'alias ssh"; exit 1; }; HOSTS+=("$2"); shift ;;
    *) echo "opzione sconosciuta: $1"; exit 1 ;;
  esac
  shift
done

# Dal PC: lo script va a ogni centralina via ssh, con le stesse opzioni
if [[ ${#HOSTS[@]} -gt 0 ]]; then
  [[ -f "$0" ]] || { echo "--centralina si usa lanciando il file, non via bash -s"; exit 1; }
  rc=0
  for h in "${HOSTS[@]}"; do
    echo "=================== $h"
    if ! ssh -o BatchMode=yes -o ConnectTimeout=15 "$h" "sudo bash -s -- ${FWD[*]:-}" < "$0"; then
      echo "$h: non raggiungibile o errore"; rc=1
    fi
  done
  exit $rc
fi

# Tutto dentro main: con "bash -s" lo script arriva da stdin, e bash lo deve
# leggere intero prima che un comando (apt, docker) possa consumarne un pezzo
main() {
[[ $EUID -eq 0 ]] || { echo "va lanciato da root (o dal PC con --centralina)"; exit 1; }
$APPLY || echo "PROVA A VUOTO: niente viene tolto (--apply per farlo)"

run() { if $APPLY; then "$@"; fi; }
state_of() {
  curl -s -m 10 "http://127.0.0.1:8888/api/$1" </dev/null \
    | python3 -c "import json,sys; print(json.load(sys.stdin).get('state',''))" 2>/dev/null
}
installed() { dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q "install ok installed"; }
# un servizio (attivo o abilitato) che nella riga di avvio nomina $1 (regex)
unit_uses() {
  local u
  for u in $( { systemctl list-units --type=service --state=running --no-legend --plain
                systemctl list-unit-files --type=service --state=enabled --no-legend; } </dev/null \
              | awk '{print $1}' | sort -u); do
    systemctl show -p ExecStart --value "$u" </dev/null 2>/dev/null | grep -qE "$1" && { echo "$u"; return 0; }
  done
  return 1
}
size_of() { du -sh "$1" 2>/dev/null | cut -f1; }

before=$(df -h / | tail -1 | awk '{print $5" ("$4" liberi)"}')

# Mai durante un backup (ferma i container) o un aggiornamento di versione
case "$(state_of backup/status)" in idle|completed|failed|"") ;; *) echo "backup in corso: riprova quando ha finito"; exit 0 ;; esac
case "$(state_of system/releases/status)" in idle|completed|failed|done|"") ;; *) echo "aggiornamento in corso: riprova quando ha finito"; exit 0 ;; esac

# 1. Backup pre-migrazione di /opt/docker_store
for f in /opt/docker_store-backup-*.tar.gz; do
  [[ -f "$f" ]] || continue
  echo "backup pre-migrazione: $f ($(size_of "$f"))"
  run rm -f "$f"
done

# 2. OpenHAB nativo
if $NATIVO; then
  for s in openhab habapp frontail; do
    if systemctl is-active --quiet "$s"; then
      echo "ERRORE: $s nativo ancora attivo, il nativo non si tocca"; NATIVO=false
    fi
  done
fi
if $NATIVO; then
  found=false
  for d in /etc/openhab /var/lib/openhab /usr/share/openhab /var/log/openhab /opt/habapp; do
    [[ -e "$d" ]] && { echo "nativo: $d ($(size_of "$d"))"; found=true; }
  done
  dpkg -l openhab openhab-addons 2>/dev/null | grep -qE "^(ii|hi)" && found=true
  ls /etc/systemd/system/srv-openhab* >/dev/null 2>&1 && found=true
  $found || { echo "nativo: niente da togliere"; NATIVO=false; }
fi
if $NATIVO; then
  if $APPLY; then
    for u in 'srv-openhab\x2daddons.mount' 'srv-openhab\x2dconf.mount' 'srv-openhab\x2dsys.mount' 'srv-openhab\x2duserdata.mount'; do
      systemctl stop "$u" 2>/dev/null || true; systemctl disable "$u" 2>/dev/null || true
      rm -f "/etc/systemd/system/$u"
    done
    rmdir /srv/openhab-addons /srv/openhab-conf /srv/openhab-sys /srv/openhab-userdata 2>/dev/null || true
    apt-mark unhold openhab openhab-addons >/dev/null 2>&1 || true
    DEBIAN_FRONTEND=noninteractive apt-get remove -y -qq openhab openhab-addons </dev/null >/dev/null 2>&1 \
      && echo "pacchetti openhab rimossi"
    rm -rf /etc/openhab /var/lib/openhab /usr/share/openhab /var/log/openhab /opt/habapp
    rm -rf /etc/systemd/system/habapp.service /etc/systemd/system/frontail.service /etc/systemd/system/openhab.service.d
    systemctl daemon-reload; systemctl reset-failed 2>/dev/null || true
    echo "mount /srv/openhab rimasti: $(mount | grep -c /srv/openhab || true)"
  fi
fi

# 3. Immagini docker che nessun container usa e che arfea.yml non nomina
yml=/opt/docker_store/arfea-controller/config/arfea.yml
keep=$(grep -E "^[[:space:]]+image:" "$yml" 2>/dev/null | awk '{print $2}' | tr -d "'\"" | sort -u)
used=$(docker ps -aq </dev/null | xargs -r docker inspect -f '{{.Image}}' | sort -u)
while read -r id tag size; do
  case "$tag" in arfea-controller:*|python:3.11-slim|"<none>:<none>") continue ;; esac
  grep -qx "$id" <<<"$used" && continue
  grep -qx "$tag" <<<"$keep" && continue
  echo "immagine non usata: $tag ($size)"
  if $APPLY; then docker rmi "$tag" </dev/null >/dev/null 2>&1 || echo "  non tolta: $tag"; fi
done < <(docker image ls --no-trunc --format '{{.ID}} {{.Repository}}:{{.Tag}} {{.Size}}' </dev/null)
run docker image prune -f </dev/null >/dev/null 2>&1 || true

# 4. Kernel non piu' installati (niente /usr/lib/modules/<versione>): sorgenti e
#    initrd che nessun pacchetto possiede. Il kernel Hardkernel 4.9 ne lascia uno
#    per ogni aggiornamento (71 MB l'uno in /usr/src). Si avvia da /media/boot.
K=$(uname -r)
for p in /usr/src/linux-[0-9]* /boot/initrd.img-* /boot/uInitrd-*; do
  [[ -e "$p" ]] || continue
  v=${p#/usr/src/linux-}; v=${v#/boot/initrd.img-}; v=${v#/boot/uInitrd-}
  [[ "$v" == "$K" || -d "/usr/lib/modules/$v" ]] && continue
  dpkg -S "$p" >/dev/null 2>&1 && continue
  # mai un file che la configurazione di avvio nomina
  grep -qsF "$(basename "$p")" /media/boot/boot.ini /boot/boot.ini /boot/extlinux/extlinux.conf /boot/armbianEnv.txt && continue
  echo "kernel $v non installato: $p ($(size_of "$p"))"
  run rm -rf "$p"
done

# 5. Cache di root e degli utenti (npm, pip, ...) e glmark2, benchmark della GPU
#    compilato sulle ODROID quando sono state preparate (con lui i compilatori)
for p in /root/.npm /root/.cache /home/*/.cache /root/glmark2* /home/*/glmark2* /usr/local/bin/glmark2* /usr/local/share/glmark2; do
  [[ -e "$p" ]] || continue
  echo "cache e avanzi: $p ($(size_of "$p"))"
  run rm -rf "$p"
done

# 6. execpipe: il vecchio OpenHAB in docker scriveva comandi in /opt/mypipe e un
#    ciclo da root (/opt/execpipe.sh, @reboot nel crontab) li eseguiva. Il
#    controller fa queste cose dalla sua API: si toglie se nessun container la monta.
if [[ -e /opt/execpipe.sh || -p /opt/mypipe ]] || crontab -l 2>/dev/null | grep -q execpipe; then
  if docker ps -aq </dev/null | xargs -r docker inspect -f '{{range .Mounts}}{{println .Source}}{{end}}' | grep -qx /opt/mypipe; then
    echo "execpipe: resta, un container monta ancora /opt/mypipe"
  else
    echo "execpipe: ciclo fermato, riga @reboot tolta dal crontab di root, /opt/mypipe e /opt/execpipe.sh tolti"
    if $APPLY; then
      pkill -f '^/bin/bash /opt/execpipe.sh'; pkill -f '^sudo nohup /opt/execpipe.sh'; pkill -f '^cat /opt/mypipe'
      if cur=$(crontab -l 2>/dev/null); then printf '%s\n' "$cur" | grep -v execpipe | crontab -; fi
      rm -f /opt/mypipe /opt/execpipe.sh /opt/mypipeoutput.txt
    fi
  fi
fi

# 7. Undici nativo, quando Undici gira in docker (deasy-to-docker.sh)
D=/opt/docker_store/deasy
undici_native=false
{ systemctl is-enabled --quiet undici 2>/dev/null || systemctl is-active --quiet undici 2>/dev/null; } && undici_native=true
UNDICI_PKGS=()
if $UNDICI; then
  health=$(docker inspect -f '{{.State.Health.Status}}' deasy 2>/dev/null </dev/null)
  if [[ "$health" != healthy || ! -f "$D/docker-compose.yml" ]]; then
    echo "Undici nativo: resta, il container deasy non c'e' o non e' sano (${health:-assente})"; UNDICI=false
  elif $undici_native; then
    echo "Undici nativo: resta, undici.service e' ancora attivo o abilitato"; UNDICI=false
  fi
fi
if $UNDICI; then
  # i dati stanno gia' nel compose di deasy (deasy-to-docker ne ha fatto anche l'archivio in /root)
  for p in /opt/undici /etc/undici /var/www; do
    [[ -e "$p" ]] || continue
    if [[ -e "$D$p" ]]; then echo "Undici nativo: $p ($(size_of "$p")), la copia e' in $D$p"; run rm -rf "$p"
    else echo "Undici nativo: $p resta, manca la copia $D$p"; fi
  done
  for p in /usr/lib/jvm/zulu*; do
    [[ -d "$p" ]] && ! dpkg -S "$p" >/dev/null 2>&1 && { echo "Undici nativo: Java $p ($(size_of "$p"))"; run rm -rf "$p"; }
  done
  for p in /lib/systemd/system/undici.service /etc/systemd/system/undici.service; do
    [[ -f "$p" ]] && ! dpkg -S "$p" >/dev/null 2>&1 && { echo "Undici nativo: $p"; run rm -f "$p"; }
  done
  # i dati di una vecchia MariaDB in container rimasti fuori da deasy, se nessun container li monta
  if [[ -d /opt/docker_store/mariadb && -d "$D/mariadb/database" ]] \
     && ! docker ps -aq </dev/null | xargs -r docker inspect -f '{{range .Mounts}}{{println .Source}}{{end}}' | grep -q '^/opt/docker_store/mariadb'; then
    echo "Undici nativo: /opt/docker_store/mariadb ($(size_of /opt/docker_store/mariadb)), il database sta in $D/mariadb"
    run rm -rf /opt/docker_store/mariadb
  fi
  # le librerie armhf della Zulu restano (17 MB): per apt libc6:armhf e' essenziale
  for x in $(dpkg-query -W -f='${Package}\n' 'lighttpd*' 'php*' librxtx-java 2>/dev/null); do
    installed "$x" && UNDICI_PKGS+=("$x")
  done
  # MariaDB nativa (dump fatto da deasy-to-docker): pacchetti tolti senza purge, i dati restano
  if ! systemctl is-active --quiet mariadb && ! systemctl is-active --quiet mysql; then
    for x in $(dpkg-query -W -f='${Package}\n' 'mariadb-server*' 'mysql-server*' 2>/dev/null); do
      installed "$x" && { UNDICI_PKGS+=("$x"); echo "Undici nativo: $x (i dati in /var/lib/mysql restano)"; }
    done
  fi
fi

# 8. Pacchetti che a una centralina col controller non servono, ognuno con la sua condizione
ROOTS=()
if $PACCHETTI; then
  java_user=""
  $undici_native && ! $UNDICI && java_user="undici (nativo)"
  [[ -z "$java_user" ]] && systemctl is-enabled --quiet openhab 2>/dev/null && java_user="openhab (nativo)"
  [[ -z "$java_user" ]] && java_user=$(unit_uses '(/|[[:space:]])java([[:space:]]|;)' || true)
  jdk=()
  for x in $(dpkg-query -W -f='${Package}\n' 'openjdk-*' 'default-jre*' 'default-jdk*' ca-certificates-java 2>/dev/null); do
    installed "$x" && jdk+=("$x")
  done
  if [[ ${#jdk[@]} -gt 0 && -n "$java_user" ]]; then echo "pacchetti: OpenJDK resta, lo usa $java_user"
  elif [[ ${#jdk[@]} -gt 0 ]]; then ROOTS+=("${jdk[@]}"); fi
  if installed linux-firmware; then
    if [[ "$K" != 4.9.* || ! -d /media/boot ]]; then echo "pacchetti: linux-firmware resta (kernel $K)"
    elif compgen -G '/sys/class/net/*/wireless' >/dev/null || compgen -G '/sys/class/net/*/phy80211' >/dev/null \
         || compgen -G '/sys/class/bluetooth/*' >/dev/null; then
      echo "pacchetti: linux-firmware resta, c'e' un dispositivo wifi o bluetooth"
    else ROOTS+=(linux-firmware); fi
  fi
  if installed nodejs; then
    u=$(unit_uses '/(node|nodejs|npm|npx|pm2|node-red|frontail)[ ;]' || true)
    if [[ -n "$u" ]]; then echo "pacchetti: nodejs resta, lo usa $u"; else ROOTS+=(nodejs); fi
  fi
  if installed samba; then
    if systemctl is-active --quiet smbd || systemctl is-enabled --quiet smbd 2>/dev/null; then
      echo "pacchetti: samba nativo resta, smbd e' attivo o abilitato"
    else
      for x in samba samba-common samba-common-bin samba-libs python3-samba samba-dsdb-modules samba-vfs-modules; do
        installed "$x" && ROOTS+=("$x")
      done
    fi
  fi
  if installed modemmanager; then
    if command -v mmcli >/dev/null && mmcli -L </dev/null 2>/dev/null | grep -q /Modem/; then
      echo "pacchetti: ModemManager resta, vede un modem"
    else ROOTS+=(modemmanager); fi
  fi
  if installed dkms; then
    echo "pacchetti: compilatori restano, c'e' dkms"
  else
    # gli attrezzi per costruire pacchetti Debian, installati a mano per compilare qualcosa,
    # e i -dev (intestazioni e librerie per compilare). Non systemd-dev: dalle versioni
    # recenti systemd ne dipende (e' fra i protetti)
    for x in build-essential gcc g++ cpp dpkg-dev fakeroot debhelper devscripts dh-autoreconf \
             dh-strip-nondeterminism lintian libtool autoconf automake autotools-dev cmake \
             $(dpkg-query -W -f='${Package}\n' 'gcc-*' 'g++-*' 'cpp-*' '*-dev' 2>/dev/null \
               | grep -E '^(gcc|g\+\+|cpp)-[0-9]+$|-dev$'); do
      installed "$x" && ROOTS+=("$x")
    done
  fi
  if dpkg-query -W -f='${Status}\n' 'nginx*' 2>/dev/null | grep -q "install ok installed"; then
    if systemctl is-active --quiet nginx || systemctl is-enabled --quiet nginx 2>/dev/null; then
      echo "pacchetti: nginx resta, e' attivo o abilitato"
    else
      for x in $(dpkg-query -W -f='${Package}\n' 'nginx*' 'libnginx-mod-*' 2>/dev/null); do installed "$x" && ROOTS+=("$x"); done
    fi
  fi
fi
ROOTS+=("${UNDICI_PKGS[@]}")
if [[ ${#ROOTS[@]} -gt 0 ]]; then
  # Restano: quello che serve al controller e agli script, quello che apt gia' oggi
  # darebbe per inutile (lo stato di prima), e Undici nativo finche' c'e'
  PROTECT="docker.io docker-ce docker-ce-cli containerd containerd.io runc docker-compose-v2
    docker-compose-plugin docker-buildx docker-buildx-plugin openvpn network-manager openssh-server
    openssh-client python3 python3-yaml python3-requests python3-urllib3 python3-certifi python3-idna
    python3-chardet curl wget ca-certificates xz-utils git rsync socat jq iw wpasupplicant ufw iptables
    nftables unattended-upgrades sudo cron vim nano htop zip unzip systemd-dev slirp4netns
    fuse-overlayfs"
  # le chiavi dei repository (docker, sury, nodesource, ...): senza, apt update li rifiuta
  PROTECT+=" $(dpkg-query -W -f='${Package}\n' '*keyring*' 2>/dev/null | tr '\n' ' ')"
  if $undici_native || ! $UNDICI; then
    PROTECT+=" $(dpkg-query -W -f='${Package}\n' 'lighttpd*' 'php*' librxtx-java 'libmysqlclient*' mysql-common 2>/dev/null | tr '\n' ' ')"
    PROTECT+=" $(dpkg -l | awk '$1=="ii" && $2 ~ /:armhf$/ {print $2}' | tr '\n' ' ')"
  fi
  # e le librerie dei programmi compilati a mano (/usr/local, /opt): tolto il loro -dev,
  # apt le darebbe per orfane, ma servono a farli girare
  handmade=$(find /usr/local /opt -xdev -path /opt/docker_store -prune -o -type f \( -perm -u+x -o -name '*.so*' \) -print 2>/dev/null \
    | grep -v glmark2 | while read -r f; do [[ "$(head -c4 "$f" 2>/dev/null)" == $'\x7fELF' ]] && ldd "$f" 2>/dev/null; done \
    | awk '/=> \//{print $3}' | sort -u | while read -r l; do
        dpkg -S "$l" 2>/dev/null || dpkg -S "${l#/usr}" 2>/dev/null || dpkg -S "/usr$l" 2>/dev/null
      done | cut -d: -f1 | sort -u | tr '\n' ' ')
  protect=$( { LC_ALL=C apt-get -s autoremove </dev/null | awk '/^Remv/{print $2}'; printf '%s\n' $PROTECT $handmade; } \
             | sed 's/:arm64$//' | sort -u)
  # Cosa toglierebbe apt (simulazione); un errore di apt vuol dire "non si tocca",
  # e anche un pacchetto essenziale nell'elenco (apt -y poi si rifiuterebbe).
  # Il motivo del rifiuto resta in $remv_err (un file: remv gira in un sottoshell)
  remv_err=$(mktemp)
  remv() {
    local out; out=$(LC_ALL=C apt-get -s remove "$@" </dev/null 2>&1)
    if [[ $? -ne 0 ]]; then
      grep -E "^E:|: (Depends|PreDepends|Breaks|Conflicts):" <<<"$out" | sed 's/^ *//' | head -3 | tr '\n' ' ' >"$remv_err"
      return 1
    fi
    if grep -q "essential packages will be removed" <<<"$out"; then echo "toglierebbe pacchetti essenziali" >"$remv_err"; return 1; fi
    awk '/^Remv/{print $2}' <<<"$out" | sed 's/:arm64$//' | sort -u
  }
  # un candidato protetto non e' piu' candidato (systemd-dev fra i -dev)
  mapfile -t ROOTS < <(printf '%s\n' "${ROOTS[@]}" | grep -vxF -f <(printf '%s\n' $PROTECT $handmade))
  cand=$(printf '%s\n' "${ROOTS[@]}" | sed 's/:arm64$//' | sort -u)
  # Prima tutti insieme. Se apt rifiuta, o se si portano dietro un pacchetto che non e'
  # candidato (che dipende da loro: devscripts da dpkg-dev), si prova uno per uno e
  # restano quelli che apt non toglie da soli o che si portano dietro altro
  OK=("${ROOTS[@]}")
  while [[ ${#OK[@]} -gt 0 ]]; do
    if all=$(remv "${OK[@]}"); then
      extra=$(grep -vxF -f <(printf '%s\n' "$cand") <<<"$all" | sed '/^$/d')
      [[ -z "$extra" ]] && break
    else
      echo "pacchetti: tutti insieme apt li rifiuta ($(cat "$remv_err")): li provo uno per uno"
    fi
    new=()
    for r in "${OK[@]}"; do
      if ! one=$(remv "$r"); then echo "pacchetti: $r resta, apt da solo non lo toglie ($(cat "$remv_err"))"; continue; fi
      out_=$(grep -vxF -f <(printf '%s\n' "$cand") <<<"$one" | sed '/^$/d' | tr '\n' ' ')
      if [[ -n "${out_// /}" ]]; then echo "pacchetti: $r resta, toglierlo porterebbe via anche $out_"; continue; fi
      new+=("$r")
    done
    if [[ ${#new[@]} -eq ${#OK[@]} ]]; then echo "pacchetti: NON tolti, apt non li toglie insieme ($(cat "$remv_err"))"; OK=(); break; fi
    OK=("${new[@]}")
  done
  ROOTS=("${OK[@]}")
fi
if [[ ${#ROOTS[@]} -gt 0 ]]; then
  # i protetti passati ad apt come "da tenere" (nome+): restano anche le loro dipendenze
  keep=$(for x in $protect; do installed "$x" && echo "$x"; done | grep -vxF -f <(printf '%s\n' "$cand") | sed 's/$/+/' | tr '\n' ' ')
  FINAL=$(LC_ALL=C apt-get -s -o APT::Get::AutomaticRemove=1 remove "${ROOTS[@]}" $keep </dev/null | awk '/^Remv/{print $2}' \
            | sed 's/:arm64$//' | sort -u | grep -vxF -f <(printf '%s\n' "$protect") \
            | { cat; printf '%s\n' "${UNDICI_PKGS[@]}"; } | sed '/^$/d' | sort -u | tr '\n' ' ')
  if [[ -n "${FINAL// /}" ]]; then
    kb=$(dpkg-query -W -f='${Installed-Size}\n' $FINAL 2>/dev/null | awk '{s+=$1} END{print s+0}')
    echo "pacchetti: $(wc -w <<<"$FINAL") da togliere, circa $((kb/1024)) MB: $FINAL"
    # apt non deve togliere nulla oltre l'elenco
    sim=$(remv $FINAL | tr '\n' ' ')
    if [[ "$sim" != "$FINAL" ]]; then
      echo "pacchetti: NON tolti, apt toglierebbe anche: $(comm -13 <(tr ' ' '\n' <<<"$FINAL" | sort) <(tr ' ' '\n' <<<"$sim" | sort) | tr '\n' ' ')"
    elif $APPLY; then
      boot_before=$(sha256sum /media/boot/* 2>/dev/null)
      # cio' che resta non deve diventare "inutile" per un autoremove futuro
      for x in $protect; do installed "$x" && echo "$x"; done | xargs -r apt-mark manual >/dev/null
      DEBIAN_FRONTEND=noninteractive apt-get remove --purge -y -q $FINAL </dev/null 2>&1 | grep -E "^(E:|W:)" || true
      if [[ "$(sha256sum /media/boot/* 2>/dev/null)" != "$boot_before" ]]; then
        echo "ATTENZIONE: /media/boot e' cambiata dopo la rimozione dei pacchetti: controllarla prima di riavviare"
      fi
      if ! dpkg -l | awk '$2 ~ /:armhf$/' | grep -q .; then
        dpkg --print-foreign-architectures | grep -qx armhf && dpkg --remove-architecture armhf && echo "architettura armhf tolta"
      fi
    fi
  fi
fi
if $UNDICI; then
  run systemctl daemon-reload
  [[ -L /etc/alternatives/java && ! -e /etc/alternatives/java ]] && run rm -f /etc/alternatives/java /usr/bin/java
fi

# 9. Cache apt, journal, log ruotati
echo "cache apt: $(size_of /var/cache/apt), journal: $(size_of /var/log/journal)"
run apt-get clean </dev/null 2>&1 | grep -v apt-fast
run journalctl --vacuum-size=200M >/dev/null 2>&1 || true
run find /var/log -type f \( -name "*.gz" -o -name "*.[0-9]" -o -name "*.old" \) -delete 2>/dev/null || true

echo "disco prima: $before, dopo: $(df -h / | tail -1 | awk '{print $5" ("$4" liberi)"}')"
docker ps -a --format '{{.Names}} {{.Status}}' </dev/null | grep -v " Up " | sed 's/^/container non su: /' || true
}

main
