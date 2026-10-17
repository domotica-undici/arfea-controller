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
#   - con OpenHAB sano, le copie tar dell'userdata che l'immagine OpenHAB scrive in
#     userdata/backup a ogni cambio di versione (300-620 MB);
#   - le immagini docker che nessun container usa e che arfea.yml non nomina
#     (restano arfea-controller e python:3.11-slim, che serve al rebuild OTA) e la
#     cache di build del controller;
#   - sorgenti (/usr/src/linux-*) e initrd (/boot) di kernel non piu' installati
#     che nessun pacchetto possiede, cache di root e degli utenti;
#   - la vecchia "execpipe" (/opt/mypipe: esegue da root quello che ci si scrive)
#     se nessun container la monta piu';
#   - cache apt, journal oltre 200 MB (e un tetto fisso di 200 MB, se nessuno ne
#     ha messo uno), log ruotati;
#   - il repository apt di NodeSource quando nodejs non c'e' (o se ne va con --pacchetti);
#   - con --nativo, solo col container openhab sano: OpenHAB/HABApp/frontail nativi,
#     2.x compreso (pacchetti senza purge, cartelle, conffile rimasti, unita'), il
#     repository apt di OpenHAB con la sua chiave, openHABian (/opt/openhabian,
#     openhabian-config, /etc/openhabian.conf, il .bash_profile che al login da'
#     "openhab3_is_installed: command not found") e i mount /srv/openhab-*. Alla
#     fine controlla che non resti niente. Dopo, tornare al nativo non e' piu'
#     possibile. Senza --nativo dice cosa resta del nativo;
#   - con --pacchetti: i pacchetti che al controller non servono, ognuno solo se
#     la sua condizione regge (OpenJDK se niente di nativo usa Java, linux-firmware
#     col kernel Hardkernel 4.9 senza wifi/BT, nodejs, samba, ModemManager senza
#     modem, compilatori senza dkms, nginx spento), con le dipendenze rimaste
#     orfane. Se apt volesse togliere anche altro, la rimozione non parte;
#   - con --undici-nativo, solo se il container deasy gira ed e' sano: Undici
#     nativo (lighttpd, PHP, RXTX, Java Zulu, /opt/undici,
#     /etc/undici, /var/www, undici.service, i dati della vecchia MariaDB in
#     container rimasti fuori da deasy, i volumi docker e /var/lib/mysql delle
#     MariaDB di prima, la copia del kit in /root/deasy-kit);
#   - con --pacchetti o --undici-nativo, se niente sull'host usa Java: la Java
#     scompattata a mano (/opt/jdk di openHABian o di Undici, una JDK sciolta in
#     /usr/lib/jvm, /root/zulu*), con le sue alternative.
# Non tocca: in /root i dump del database di deasy (che il backup del controller
# esclude) e gli archivi di Undici; deasy in docker con la sua Zulu (il kit si
# copia da li'), i backup del controller, i volumi docker di servizi in uso. Non
# parte durante un backup, un aggiornamento di versione o del controller.
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
# un processo dell'host (non di un container: hanno un altro mount namespace, e i
# loro percorsi sono quelli interni) il cui eseguibile sta sotto $1, oppure il cui
# nome e' esattamente $2 (regex)
host_ns=$(readlink /proc/1/ns/mnt)
host_proc() {
  local p e
  for p in /proc/[0-9]*; do
    [[ "$(readlink "$p/ns/mnt" 2>/dev/null)" == "$host_ns" ]] || continue
    e=$(readlink "$p/exe" 2>/dev/null) || continue
    [[ -n "$1" && "$e" == "$1"/* ]] && return 0
    [[ -n "${2:-}" ]] && grep -qxE "$2" "$p/comm" 2>/dev/null && return 0
  done
  return 1
}
# un container (anche fermo) che monta $1 o qualcosa sotto
mounted_by_container() {
  docker ps -aq </dev/null | xargs -r docker inspect -f '{{range .Mounts}}{{println .Source}}{{end}}' 2>/dev/null \
    | grep -qE "^$1(/|$)"
}
# i database in una cartella dati di MariaDB/MySQL (senza quelli di sistema)
dbs_in() {
  find "$1" -mindepth 1 -maxdepth 1 -type d ! -name mysql ! -name performance_schema \
    ! -name sys ! -name '#*' -printf '%f ' 2>/dev/null
}
# toglie dalle alternative di Debian quelle che puntano dentro $1
drop_alternatives() {
  local n a
  for n in $(ls /var/lib/dpkg/alternatives 2>/dev/null); do
    for a in $(update-alternatives --query "$n" 2>/dev/null | awk '/^Alternative: /{print $2}'); do
      [[ "$a" == "$1" || "$a" == "$1"/* ]] && update-alternatives --remove "$n" "$a" >/dev/null 2>&1
    done
  done
  return 0
}
# un processo dell'host (come host_proc) la cui riga di comando corrisponde a $1 (regex)
host_cmd() {
  local p a=()
  for p in /proc/[0-9]*; do
    [[ "$(readlink "$p/ns/mnt" 2>/dev/null)" == "$host_ns" ]] || continue
    mapfile -d '' -t a 2>/dev/null <"$p/cmdline" || continue
    [[ "${a[*]}" =~ $1 ]] && return 0
  done
  return 1
}
apt_sources() { ls -d /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources 2>/dev/null; }
# Toglie i repository apt il cui URL corrisponde a $1 (regex): un file che ha solo loro
# si toglie, in uno misto le righe si commentano (un .sources misto resta). Con loro la
# chiave signed-by, se nessun pacchetto la possiede e nessun'altra sorgente la usa
drop_repo() {
  local re="$1" f k keys=() gone=()
  for f in $(apt_sources); do
    grep -qE "^[^#]*($re)" "$f" || continue
    if [[ "$f" == *.sources ]]; then
      if grep -E '^URIs:' "$f" | grep -qvE "$re"; then echo "repository apt: $f resta, ha anche altre sorgenti"; continue; fi
      keys+=($(awk '$1 == "Signed-By:" && $2 ~ /^\// {print $2}' "$f"))
      gone+=("$f"); echo "repository apt: $f"; run rm -f "$f"
    else
      keys+=($(grep -E "^[^#]*($re)" "$f" | grep -oE 'signed-by=[^] ]+' | cut -d= -f2))
      if [[ "$f" != /etc/apt/sources.list ]] && ! grep -E '^[[:space:]]*deb' "$f" | grep -qvE "$re"; then
        gone+=("$f"); echo "repository apt: $f"; run rm -f "$f"
      else
        echo "repository apt: righe commentate in $f: $(grep -E "^[^#]*($re)" "$f" | tr '\n' ' ')"
        run sed -i -E "\%^[^#]*($re)%s%^%# pulizia-post-migrazione: %" "$f"
      fi
    fi
  done
  for k in $(printf '%s\n' "${keys[@]}" | sort -u); do
    [[ -f "$k" ]] && ! dpkg -S "$k" >/dev/null 2>&1 || continue
    apt_sources | grep -vxF -f <(printf '%s\n' "${gone[@]}") | xargs -r cat 2>/dev/null \
      | grep -v '^[[:space:]]*#' | grep -vE "$re" | grep -qF "$k" && continue
    echo "repository apt: chiave $k"; run rm -f "$k"
  done
}

before=$(df -h / | tail -1 | awk '{print $5" ("$4" liberi)"}')

# Mai durante un backup (ferma i container), un aggiornamento di versione o del
# controller (il rebuild gira in arfea-selfupdate e usa la cache di build)
case "$(state_of backup/status)" in idle|completed|failed|"") ;; *) echo "backup in corso: riprova quando ha finito"; exit 0 ;; esac
case "$(state_of system/releases/status)" in idle|completed|failed|done|"") ;; *) echo "aggiornamento in corso: riprova quando ha finito"; exit 0 ;; esac
case "$(state_of system/update/status)" in idle|completed|failed|"") ;; *) echo "aggiornamento del controller in corso: riprova quando ha finito"; exit 0 ;; esac
if systemctl is-active --quiet arfea-selfupdate 2>/dev/null || systemctl is-active --quiet arfea-controller-guard 2>/dev/null; then
  echo "rebuild del controller in corso: riprova quando ha finito"; exit 0
fi

# 1. Backup pre-migrazione di /opt/docker_store
for f in /opt/docker_store-backup-*.tar.gz; do
  [[ -f "$f" ]] || continue
  echo "backup pre-migrazione: $f ($(size_of "$f"))"
  run rm -f "$f"
done

# 1b. La copia tar dell'userdata che l'immagine OpenHAB scrive a ogni cambio di
#     versione (300-620 MB sulle C4): con OpenHAB sano non serve piu', e il
#     controller fa un backup suo prima di ogni aggiornamento (Redmine #366)
UB=/opt/docker_store/openhab/userdata/backup
if compgen -G "$UB/*.tar" >/dev/null; then
  if [[ "$(docker inspect -f '{{.State.Health.Status}}' openhab 2>/dev/null </dev/null)" == healthy ]]; then
    for f in "$UB"/*.tar; do
      echo "copia dell'userdata di OpenHAB: $f ($(size_of "$f"))"
      run rm -f "$f"
    done
  else
    echo "copie dell'userdata di OpenHAB in $UB: restano, il container openhab non e' sano"
  fi
fi

# 2. OpenHAB nativo (2.x e successivi) e openHABian, con --nativo e solo col container
#    openhab sano. I pacchetti si tolgono senza purge: il purge cancella l'utente
#    openhab, che su openHABian possiede /opt e /srv. I conffile rimasti, il
#    repository apt e la sua chiave si tolgono a mano. Alla fine si controlla che non
#    resti niente; senza --nativo si dice cosa c'e' ancora
OH_REPO='openhab\.jfrog\.io|bintray\.com/openhab'
OH_PROC='openhab\.home=|/usr/share/openhab2?/|/opt/habapp/bin/|frontail'
OH_PATHS=(/etc/openhab /etc/openhab2 /var/lib/openhab /var/lib/openhab2 /usr/share/openhab /usr/share/openhab2
          /var/log/openhab /var/log/openhab2 /opt/habapp /etc/default/openhab /etc/default/openhab2 /etc/openhab-cli
          /etc/profile.d/openhab*.sh* /etc/systemd/system/habapp.service /etc/systemd/system/frontail.service
          /etc/systemd/system/openhab.service /etc/systemd/system/openhab2.service
          /etc/systemd/system/openhab.service.d /etc/systemd/system/openhab2.service.d
          /root/.firemotd-cache /home/*/.firemotd-cache)
OHB_PATHS=(/opt/openhabian /usr/local/bin/openhabian-config /etc/openhabian.conf* /opt/openHABian-install-*)
# copia dell'init di raspi-config: all'avvio mette il governor della CPU a ondemand
OHB_INIT=/etc/init.d/openhabian-config
init_on() { compgen -G '/etc/rc[S0-6].d/S[0-9]*openhabian-config' >/dev/null; }
# il ~/.bash_profile di openHABian: carica /opt/openhabian e al login stampa il logo con
# gli errori "openhab3_is_installed: command not found" e version.properties (Redmine #399)
ohb_logins() { grep -lsF /opt/openhabian /root/.bash_profile /home/*/.bash_profile /etc/skel/.bash_profile; return 0; }
# chi nomina ancora openHABian, oltre ai file che questa pulizia toglie o rinomina
ohb_users() {
  grep -rlsIE '/opt/openhabian|openhabian-config' /etc /usr/local/bin /usr/local/sbin /var/spool/cron \
      /root/.bashrc /root/.profile /root/.bash_login /root/.bash_aliases \
      /home/*/.bashrc /home/*/.profile /home/*/.bash_login /home/*/.bash_aliases \
    | grep -vE '^/etc/(openhabian\.conf|init\.d/openhabian-config$|profile\.d/openhab|skel/\.bash_profile$)|\.disabled-arfea$' \
    | tr '\n' ' '
}
oh_pkgs() {
  dpkg-query -W -f='${Package} ${Status}\n' 'openhab*' 2>/dev/null \
    | awk '$4 != "not-installed" && $4 != "config-files" {print $1}'
}
# cio' che resta dell'OpenHAB nativo e di openHABian, una voce per riga
nat_left() {
  local p
  dpkg-query -W -f='${Package} ${Status} ${Version}\n' 'openhab*' 2>/dev/null \
    | awk '$4 != "not-installed" && $4 != "config-files" {print "pacchetto " $1 " " $5 " (" $4 ")"}'
  for p in openhab openhab2 habapp frontail; do
    systemctl is-active --quiet "$p" 2>/dev/null && echo "servizio $p attivo"
  done
  host_cmd "$OH_PROC" && echo "un processo di OpenHAB, HABApp o frontail sull'host"
  for p in "${OH_PATHS[@]}" "${OHB_PATHS[@]}"; do
    [[ -e "$p" || -L "$p" ]] && echo "$p ($(size_of "$p"))"
  done
  [[ -e "$OHB_INIT" ]] && echo "$OHB_INIT$(init_on && echo ", attivo all'avvio")"
  ohb_logins | sed 's/$/ (banner di openHABian al login)/'
  ls -d /etc/systemd/system/srv-openhab* 2>/dev/null
  mount | grep -q ' /srv/openhab' && echo "mount /srv/openhab-* attivi"
  for p in $(apt_sources); do grep -qE "^[^#]*($OH_REPO)" "$p" && echo "repository apt di OpenHAB in $p"; done
  return 0
}
NATIVO_ASKED=$NATIVO
nat=$(nat_left)
if $NATIVO; then
  health=$(docker inspect -f '{{.State.Health.Status}}' openhab 2>/dev/null </dev/null)
  if [[ -z "$nat" ]]; then
    echo "OpenHAB nativo: niente da togliere"; NATIVO=false
  elif [[ "$health" != healthy ]]; then
    echo "OpenHAB nativo: resta, il container openhab non c'e' o non e' sano (${health:-assente})"; NATIVO=false
  else
    for s in openhab openhab2 habapp frontail; do
      if systemctl is-active --quiet "$s"; then
        echo "ERRORE: $s nativo ancora attivo, il nativo non si tocca"; NATIVO=false
      fi
    done
    if $NATIVO && host_cmd "$OH_PROC"; then
      echo "ERRORE: sull'host gira ancora un OpenHAB, HABApp o frontail nativo, il nativo non si tocca"; NATIVO=false
    fi
    # anche una sorgente che e' un link verso una cartella nativa
    msrc=$(docker ps -aq </dev/null | xargs -r docker inspect -f '{{range .Mounts}}{{println .Source}}{{end}}' 2>/dev/null \
           | sed '/^$/d' | while read -r s; do echo "$s"; readlink -f "$s"; done)
    for p in "${OH_PATHS[@]}" "${OHB_PATHS[@]}"; do
      if [[ -e "$p" ]] && grep -qE "^$p(/|$)" <<<"$msrc"; then
        echo "ERRORE: un container monta $p, il nativo non si tocca"; NATIVO=false
      fi
    done
  fi
fi
if $NATIVO; then
  grep -v '^repository apt' <<<"$nat" | sed 's/^/OpenHAB nativo: /'
  # apt non deve togliere altro che i pacchetti di OpenHAB; se non li toglie, i loro
  # file restano (sotto un pacchetto installato non si cancella niente)
  go=true
  pk=$(oh_pkgs | tr '\n' ' ')
  if [[ -n "${pk// /}" ]]; then
    extra=$(LC_ALL=C apt-get -s remove $pk </dev/null 2>/dev/null | awk '/^Remv/{print $2}' | grep -vxF -f <(tr ' ' '\n' <<<"$pk") | tr '\n' ' ')
    if [[ -n "${extra// /}" ]]; then echo "OpenHAB nativo: resta, apt toglierebbe anche $extra"; go=false; fi
  fi
  ohb_keep=$(ohb_users)
  [[ -n "$ohb_keep" ]] && echo "openHABian: resta, lo nomina ancora $ohb_keep"
  init_on && echo "openHABian: $OHB_INIT resta, e' attivo all'avvio (governor della CPU): update-rc.d openhabian-config remove, poi si toglie"
  if $go && $APPLY && [[ -n "${pk// /}" ]]; then
    apt-mark unhold $pk >/dev/null 2>&1 || true
    if out=$(DEBIAN_FRONTEND=noninteractive apt-get remove -y -q $pk </dev/null 2>&1); then
      echo "OpenHAB nativo: pacchetti tolti: $pk"
    else
      echo "ERRORE: apt non ha tolto $pk, il resto del nativo non si tocca:"; grep -E "^(E:|dpkg)" <<<"$out" | head -5 | sed 's/^/  /'
      go=false
    fi
  fi
  if $go && $APPLY; then
    for f in /etc/systemd/system/srv-openhab*.mount; do
      [[ -e "$f" ]] || continue
      systemctl stop "${f##*/}" 2>/dev/null || true; systemctl disable "${f##*/}" 2>/dev/null || true
      rm -f "$f"
    done
    rmdir /srv/openhab-addons /srv/openhab-conf /srv/openhab-sys /srv/openhab-userdata 2>/dev/null || true
    for p in "${OH_PATHS[@]}"; do rm -rf "$p"; done
    find /etc/systemd/system -xtype l \( -name 'openhab*.service' -o -name habapp.service -o -name frontail.service \
      -o -name 'srv-openhab*.mount' \) -delete 2>/dev/null
    # bash torna a ~/.profile, che il .bash_profile di openHABian caricava in fondo
    for f in $(ohb_logins); do
      mv "$f" "$f.disabled-arfea" && echo "openHABian: $f rinominato in $f.disabled-arfea"
    done
    if [[ -z "$ohb_keep" ]]; then for p in "${OHB_PATHS[@]}"; do rm -rf "$p"; done; fi
    init_on || rm -f "$OHB_INIT"
  fi
  $go && drop_repo "$OH_REPO"
  if $APPLY; then
    systemctl daemon-reload; systemctl reset-failed 2>/dev/null || true
    # il controllo: dell'OpenHAB nativo non deve restare niente
    nat=$(nat_left)
    if [[ -z "$nat" ]]; then
      echo "OpenHAB nativo: tolto, sull'host non restano pacchetti, servizi, processi, cartelle, openHABian ne' repository"
    else
      echo "ATTENZIONE: dell'OpenHAB nativo resta ancora:"; sed 's/^/  /' <<<"$nat"
    fi
  fi
elif ! $NATIVO_ASKED && [[ -n "$nat" ]]; then
  echo "OpenHAB nativo ancora sull'host (lo toglie --nativo):"; sed 's/^/  /' <<<"$nat"
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
# la cache di build del rebuild del controller (~475 MB): dalla 1.8.16 la toglie il
# guardiano dopo ogni rebuild, prima restava
bc=$(docker builder du </dev/null 2>/dev/null | awk '/^Reclaimable:/{print $2}')
[[ -n "$bc" && "$bc" != 0B ]] && echo "cache di build di docker: $bc"
run docker builder prune -af </dev/null >/dev/null 2>&1 || true

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
  # le MariaDB di prima di deasy: volumi docker che nessun container usa con dentro
  # un database (i container della MariaDB di Undici), e /var/lib/mysql di una
  # MariaDB nativa gia' tolta (pacchetto non installato, nessun mysqld sull'host,
  # nessun container che la monta). Su un impianto erano 600 MB (Redmine #305)
  if [[ -d "$D/mariadb/database" ]]; then
    for v in $(docker volume ls -qf dangling=true </dev/null 2>/dev/null); do
      p=$(docker volume inspect -f '{{.Mountpoint}}' "$v" </dev/null 2>/dev/null)
      [[ -n "$p" && ( -e "$p/ibdata1" || -e "$p/aria_log_control" || -d "$p/mysql" ) ]] || continue
      echo "Undici nativo: volume di una vecchia MariaDB $v ($(size_of "$p"); database: $(dbs_in "$p")), il database sta in $D/mariadb"
      run docker volume rm "$v" </dev/null >/dev/null
    done
    if [[ -d /var/lib/mysql ]]; then
      if dpkg-query -W -f='${Status}\n' 'mariadb-server*' 'mysql-server*' 2>/dev/null | grep -q "install ok installed"; then
        echo "Undici nativo: /var/lib/mysql resta, il pacchetto del server e' installato (si toglie al giro dopo la sua rimozione)"
      elif systemctl is-active --quiet mariadb || systemctl is-active --quiet mysql || host_proc "" 'mysqld|mariadbd'; then
        echo "Undici nativo: /var/lib/mysql resta, c'e' un server MariaDB/MySQL in esecuzione sull'host"
      elif mounted_by_container /var/lib/mysql; then
        echo "Undici nativo: /var/lib/mysql resta, un container la monta"
      else
        echo "Undici nativo: /var/lib/mysql di una MariaDB nativa tolta ($(size_of /var/lib/mysql); database: $(dbs_in /var/lib/mysql)), il database sta in $D/mariadb"
        run rm -rf /var/lib/mysql
      fi
    fi
  fi
  # la copia del kit che deasy-to-docker ha usato: la sorgente e' $D di un impianto con deasy
  if [[ -d /root/deasy-kit ]] && docker image inspect deasy-deasy >/dev/null 2>&1 </dev/null; then
    echo "Undici nativo: copia del kit /root/deasy-kit ($(size_of /root/deasy-kit))"
    run rm -rf /root/deasy-kit
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

# Chi usa Java sull'host (i container hanno la loro): Undici o OpenHAB nativi, un servizio
java_user=""
if $PACCHETTI || $UNDICI; then
  $undici_native && ! $UNDICI && java_user="undici (nativo)"
  for s in openhab openhab2; do
    [[ -z "$java_user" ]] && systemctl is-enabled --quiet "$s" 2>/dev/null && java_user="$s (nativo)"
  done
  [[ -z "$java_user" ]] && java_user=$(unit_uses '(/|[[:space:]])java([[:space:]]|;)' || true)
fi

# 7b. Java scompattata a mano, fuori dai pacchetti: /opt/jdk (openHABian e Undici
#     nativo), una JDK sciolta direttamente in /usr/lib/jvm, le Zulu in /root. Solo se
#     niente sull'host usa Java; con lei le alternative (/usr/bin/java) che puntano li'
if $PACCHETTI || $UNDICI; then
  J=()
  for p in /opt/jdk/* /root/zulu*; do
    [[ -e "$p" ]] && ! dpkg -S "$p" >/dev/null 2>&1 && J+=("$p")
  done
  # una JDK scompattata in /usr/lib/jvm stessa (release e bin/java al primo livello):
  # solo le sue voci, mai le cartelle dei pacchetti (java-*-openjdk-*, .jinfo)
  if [[ -f /usr/lib/jvm/release && -e /usr/lib/jvm/bin/java ]] && ! dpkg -S /usr/lib/jvm/release >/dev/null 2>&1; then
    for n in bin conf demo include jmods legal lib man sample jre src.zip release readme.txt \
             README.html Welcome.html DISCLAIMER ASSEMBLY_EXCEPTION THIRD_PARTY_README LICENSE COPYRIGHT; do
      p=/usr/lib/jvm/$n
      [[ -e "$p" ]] && ! dpkg -S "$p" >/dev/null 2>&1 && J+=("$p")
    done
  fi
  if [[ ${#J[@]} -gt 0 ]]; then
    users="$java_user"
    if [[ -z "$users" ]]; then
      for p in "${J[@]}"; do
        [[ -d "$p" ]] && host_proc "$p" && { users="un processo dell'host ($p)"; break; }
      done
    fi
    if [[ -n "$users" ]]; then
      echo "Java scompattata a mano: resta, la usa $users"
    else
      for p in "${J[@]}"; do
        echo "Java scompattata a mano: $p ($(size_of "$p"))"
        if $APPLY; then drop_alternatives "$p"; rm -rf "$p"; fi
      done
      run rmdir /opt/jdk 2>/dev/null || true
    fi
  fi
fi

# 8. Pacchetti che a una centralina col controller non servono, ognuno con la sua condizione
ROOTS=(); GONE=""
if $PACCHETTI; then
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
    [[ "$sim" == "$FINAL" ]] && GONE=" $FINAL"
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

# 8b. Il repository NodeSource (openHABian lo metteva per frontail) quando nodejs non
#     c'e', o se ne va adesso: da li' non viene altro
if [[ "$(dpkg-query -W -f='${Status}' nodejs 2>/dev/null)" != *" installed" ]] \
   || { ! $APPLY && [[ "$GONE" == *" nodejs "* ]]; }; then
  drop_repo 'deb\.nodesource\.com'
fi

# 9. Cache apt, journal, log ruotati
echo "cache apt: $(size_of /var/cache/apt), journal: $(size_of /var/log/journal)"
run apt-get clean </dev/null 2>&1 | grep -v apt-fast
# Tetto fisso al journal: il vacuum da solo non basta, un servizio in ciclo lo riporta a
# 1 GB in pochi giorni (Redmine #367). Solo se nessuno ne ha gia' messo uno
JC=/etc/systemd/journald.conf.d/arfea.conf
if ! grep -qsE '^[[:space:]]*SystemMaxUse=' /etc/systemd/journald.conf /etc/systemd/journald.conf.d/*.conf; then
  echo "journal: tetto fisso a 200 MB ($JC)"
  if $APPLY; then
    mkdir -p "${JC%/*}"
    printf '[Journal]\n# pulizia-post-migrazione.sh: il journal non supera i 200 MB\nSystemMaxUse=200M\n' > "$JC"
    systemctl restart systemd-journald
  fi
fi
run journalctl --vacuum-size=200M >/dev/null 2>&1 || true
run find /var/log -type f \( -name "*.gz" -o -name "*.[0-9]" -o -name "*.old" \) -delete 2>/dev/null || true

echo "disco prima: $before, dopo: $(df -h / | tail -1 | awk '{print $5" ("$4" liberi)"}')"
docker ps -a --format '{{.Names}} {{.Status}}' </dev/null | grep -v " Up " | sed 's/^/container non su: /' || true
}

main
