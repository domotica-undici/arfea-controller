#!/bin/bash
###############################################################################
# Pulizia di una Raspberry (Raspbian / Raspberry Pi OS, OpenHAB nativo o in
# docker): l'ambiente desktop che nessuno guarda, il Java che nessuno usa, i
# pacchetti orfani, cache apt, journal e log ruotati (Redmine #325).
# Gira sulla Raspberry, da root.
#
#   sudo bash pulizia-raspberry.sh                     # prova a vuoto: dice cosa toglierebbe
#   sudo bash pulizia-raspberry.sh --apply             # toglie davvero
#   sudo bash pulizia-raspberry.sh --apply --tieni-desktop
#
# Dal PC (o dallo Script Hub), su piu' Raspberry: lo script si manda da solo via
# ssh a ognuna, con le stesse opzioni; una che non risponde non ferma le altre.
#   ./script/pulizia-raspberry.sh --centralina <alias> --centralina <alias> [--apply] [--tieni-desktop]
#
# Toglie:
#   - il desktop (LXDE/PIXEL, lightdm, Xorg, Chromium, le applicazioni dell'immagine
#     "with desktop" e i pacchetti che ne dipendono), solo se nessuno lo guarda: resta con uno schermo collegato
#     (EDID sull'HDMI, display DSI, touch), con un browser in esecuzione, se un
#     crontab o un servizio usa il display, o con --tieni-desktop. Le librerie X
#     che servono a Java restano (la Zulu dipende da libx11-6, libxtst6, ...).
#     L'avvio passa a multi-user.target, e le cache e i profili di Chromium degli
#     utenti se ne vanno con lui;
#   - oracle-java8-jdk, se il java di sistema e' un altro, nessun processo lo usa
#     e nessuna configurazione lo nomina (OpenHAB 2 sulle Raspberry usa la Zulu);
#   - i pacchetti che apt gia' da' per inutili, e le dipendenze rimaste orfane;
#   - cache apt, journal oltre 200 MB, log ruotati.
# Restano sempre OpenHAB, il Java in uso, la rete (dhcpcd, wpa_supplicant, hostapd,
# dnsmasq, NetworkManager), openvpn, ssh, samba, nginx, ser2net, nodejs, mosquitto,
# docker, kernel e firmware, le chiavi dei repository e i pacchetti di cui vivono i
# servizi in esecuzione (eseguibili e librerie caricate). Se apt volesse togliere
# anche altro, la rimozione non parte.
###############################################################################
set -u

APPLY=false; KEEP_DESKTOP=false; HOSTS=(); FWD=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) APPLY=true; FWD+=(--apply) ;;
    --tieni-desktop) KEEP_DESKTOP=true; FWD+=(--tieni-desktop) ;;
    --centralina) [[ -n "${2:-}" ]] || { echo "--centralina vuole l'alias ssh"; exit 1; }; HOSTS+=("$2"); shift ;;
    *) echo "opzione sconosciuta: $1"; exit 1 ;;
  esac
  shift
done

# Dal PC: lo script va a ogni Raspberry via ssh, con le stesse opzioni
if [[ ${#HOSTS[@]} -gt 0 ]]; then
  [[ -f "$0" ]] || { echo "--centralina si usa lanciando il file, non via bash -s"; exit 1; }
  rc=0
  S=(ssh -o BatchMode=yes -o ConnectTimeout=15)
  for h in "${HOSTS[@]}"; do
    echo "=================== $h"
    if ! $APPLY; then
      "${S[@]}" "$h" "sudo bash -s -- ${FWD[*]:-}" < "$0" || { echo "$h: non raggiungibile o errore"; rc=1; }
      continue
    fi
    # Con --apply in un'unita' systemd sulla Raspberry: se la connessione cade, apt
    # non resta a meta'. Il PC ne segue la fine e mostra il log
    if ! "${S[@]}" "$h" "sudo tee /root/pulizia-raspberry.sh >/dev/null && sudo systemctl reset-failed pulizia-raspberry 2>/dev/null;
          sudo systemd-run --unit=pulizia-raspberry /bin/bash -c 'bash /root/pulizia-raspberry.sh ${FWD[*]} > /var/tmp/pulizia-raspberry.log 2>&1'" < "$0"; then
      echo "$h: non raggiungibile o errore"; rc=1; continue
    fi
    lost=0
    while [[ $lost -lt 30 ]]; do
      sleep 20
      st=$("${S[@]}" "$h" "systemctl is-active pulizia-raspberry" </dev/null 2>/dev/null)
      if [[ -z "$st" ]]; then lost=$((lost + 1)); continue; fi
      lost=0
      [[ "$st" == active || "$st" == activating ]] || break
    done
    "${S[@]}" "$h" "cat /var/tmp/pulizia-raspberry.log" </dev/null || { echo "$h: log non leggibile (/var/tmp/pulizia-raspberry.log)"; rc=1; }
  done
  exit $rc
fi

# Tutto dentro main: con "bash -s" lo script arriva da stdin, e bash lo deve
# leggere intero prima che un comando (apt) possa consumarne un pezzo
main() {
[[ $EUID -eq 0 ]] || { echo "va lanciato da root (o dal PC con --centralina)"; exit 1; }
model=$(tr -d '\0' </proc/device-tree/model 2>/dev/null)
[[ "$model" == *"Raspberry Pi"* ]] || { echo "non e' una Raspberry ($model): niente da fare"; exit 0; }
echo "$model, $(. /etc/os-release; echo "$PRETTY_NAME")"
$APPLY || echo "PROVA A VUOTO: niente viene tolto (--apply per farlo)"
# (unattended-upgrade-shutdown resta sempre in attesa: non conta)
for x in apt apt-get dpkg aptitude; do
  pgrep -x "$x" >/dev/null && { echo "apt e' gia' al lavoro: riprova quando ha finito"; exit 0; }
done
pgrep -f 'bin/unattended-upgrade( |$)' >/dev/null && { echo "apt e' gia' al lavoro: riprova quando ha finito"; exit 0; }

run() { if $APPLY; then "$@"; fi; }
installed() { dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q "install ok installed"; }
# i pacchetti installati fra quelli indicati (anche con * e ?)
pkgs() { dpkg-query -W -f='${db:Status-Abbrev} ${Package}\n' "$@" 2>/dev/null | awk '$1 ~ /^[ih]i/ {print $2}' | sort -u; }
size_of() { du -sh "$1" 2>/dev/null | cut -f1; }
export LC_ALL=C

before=$(df -h / | tail -1 | awk '{print $5" ("$4" liberi)"}')

# 1. Desktop: resta se qualcuno lo guarda
keep_why=""
$KEEP_DESKTOP && keep_why="--tieni-desktop"
if [[ -z "$keep_why" ]] && command -v tvservice >/dev/null; then
  n=$(tvservice -n 2>&1)
  # senza HDMI la Raspberry esce sul composito e risponde "Unk-Composite dis"
  [[ "$n" == device_name=* && "$n" != *Composite* ]] && keep_why="schermo HDMI collegato (${n#device_name=})"
  [[ -z "$keep_why" && "$(tvservice -s 2>&1)" == *LCD* ]] && keep_why="display DSI collegato"
fi
if [[ -z "$keep_why" ]]; then
  for st in /sys/class/drm/card*-*/status; do
    [[ -f "$st" && "$st" != *Writeback* && "$(cat "$st")" == connected ]] \
      && { keep_why="schermo collegato ($(basename "$(dirname "$st")"))"; break; }
  done
fi
[[ -z "$keep_why" ]] && grep -qiE '^N: Name=.*(touch|FT5406|ft5x06|goodix|ads7846)' /proc/bus/input/devices 2>/dev/null \
  && keep_why="touch screen collegato"
if [[ -z "$keep_why" ]]; then
  b=$(pgrep -fa 'chromium-browser|/chromium |firefox|epiphany|midori|kweb|surf ' | head -1 | cut -c1-80)
  [[ -n "$b" ]] && keep_why="browser in esecuzione ($b)"
fi
if [[ -z "$keep_why" ]]; then
  f=$(grep -lsE 'DISPLAY=|xdotool|startx|xinit' /etc/rc.local /etc/crontab /etc/cron.d/* /var/spool/cron/crontabs/* \
        /etc/systemd/system/*.service 2>/dev/null | grep -v display-manager | head -1)
  [[ -n "$f" ]] && keep_why="$f usa il display"
fi
DESK=()
mapfile -t DESK < <(pkgs raspberrypi-ui-mods 'lxde*' 'lxpanel*' 'lxsession*' 'lxappearance*' lxinput lxrandr \
  lxtask lxterminal 'lxplug-*' 'lxhotkey-*' lxpolkit lxmenu-data lxkeymap openbox obconf 'pcmanfm*' \
  'lightdm*' pi-greeter 'xserver-xorg*' xserver-common xinit x11-xserver-utils x11-utils x11-xkb-utils xinput \
  xterm xcompmgr xdotool unclutter 'xscreensaver*' 'wayfire*' labwc 'wf-panel-pi*' squeekboard wvkbd \
  'chromium-browser*' chromium-codecs-ffmpeg-extra rpi-chromium-mods chromium chromium-common chromium-sandbox \
  'chromium-l10n' firefox-esr rpi-firefox-mods thonny 'geany*' 'scratch*' nuscratch 'sonic-pi*' minecraft-pi \
  'python*-minecraftpi' wolfram-engine 'libreoffice*' bluej 'greenfoot*' 'claws-mail*' 'vlc*' 'realvnc-vnc-*' \
  mu-editor smartsim sense-emu-tools python-games code-the-classics piwiz rp-prefapps rc-gui pipanel pprompt \
  pishutdown arandr gpicview galculator leafpad mousepad xarchiver zenity dillo epiphany-browser 'netsurf*' \
  qpdfview 'gtk2-engines*' 'rpd-*' pixel-wallpaper raspberrypi-artwork desktop-base dhcpcd-gtk alacarte menu-xdg \
  'gvfs*' 'gnome-themes*' gnome-icon-theme gnome-menus gnome-desktop3-data policykit-1-gnome agnostics gldriver-test \
  openbox-lxde-session piclone 'matchbox-keyboard*' scrot pimixer point-rpi)
if [[ ${#DESK[@]} -eq 0 ]]; then
  echo "desktop: non installato"
elif [[ -n "$keep_why" ]]; then
  echo "desktop: resta, $keep_why"; DESK=()
fi

# 2. Java Oracle, se il java di sistema e' un altro e nessuno lo usa
JAVA=()
mapfile -t oracle < <(pkgs 'oracle-java*')
if [[ ${#oracle[@]} -gt 0 ]]; then
  why=""
  [[ "$(readlink -f /etc/alternatives/java 2>/dev/null)" == *oracle* ]] && why="e' il java di sistema"
  for p in $(pgrep -x java); do
    [[ -z "$why" && "$(readlink -f "/proc/$p/exe")" == *oracle* ]] && why="lo usa il processo $p"
  done
  if [[ -z "$why" ]]; then
    f=$(grep -rlsE --exclude-dir=openhabian 'jdk-8-oracle|oracle-java|java-8-oracle' /etc/default /etc/systemd /lib/systemd/system /etc/init.d \
          /etc/rc.local /etc/crontab /etc/cron.d /var/spool/cron/crontabs /etc/openhab2 /etc/openhab /etc/environment \
          /etc/profile.d /opt 2>/dev/null | head -1)
    [[ -n "$f" ]] && why="lo nomina $f"
  fi
  if [[ -n "$why" ]]; then echo "java: ${oracle[*]} resta, $why"; else JAVA=("${oracle[@]}"); fi
fi

# 3. Quello che apt gia' da' per inutile
mapfile -t AUTO < <(apt-get -s autoremove </dev/null 2>/dev/null | awk '/^Remv/{print $2}')

# Restano: i pacchetti di base dell'impianto e quelli di cui vivono i servizi in
# esecuzione (eseguibile e librerie caricate, letti da /proc). Il display manager
# non conta: e' il desktop che se ne va; e nemmeno i moduli GIO (gvfs), che GLib
# carica in ogni processo se ci sono (polkit, udisks2, packagekit)
PROTECT="openhab2 openhab2-addons openhab2-addons-legacy openhab openhab-addons java-common
  openvpn openssh-server openssh-client openssh-sftp-server ssh sudo cron anacron rsyslog logrotate
  dhcpcd5 wpasupplicant raspberrypi-net-mods ifupdown iproute2 iptables iw wireless-tools wireless-regdb crda
  hostapd dnsmasq dnsmasq-base network-manager modemmanager avahi-daemon ntp systemd-timesyncd fake-hwclock
  raspberrypi-kernel raspberrypi-bootloader raspberrypi-sys-mods libraspberrypi0 libraspberrypi-bin raspi-config
  rpi-update rpi-eeprom pi-bluetooth bluez bluez-firmware alsa-utils
  samba samba-common samba-common-bin smbclient nginx nginx-common nginx-full nginx-light ser2net
  nodejs npm mosquitto mosquitto-clients docker-ce docker-ce-cli containerd.io docker.io containerd runc
  docker-compose docker-compose-plugin docker-buildx-plugin
  python3 python3-pip python python-pip python3-yaml python3-requests curl wget ca-certificates
  apt-transport-https unattended-upgrades git rsync xz-utils zip unzip nano vim screen htop jq socat
  fontconfig fontconfig-config fonts-dejavu-core libfontconfig1"
PROTECT+=" linux-image-$(uname -r) $(pkgs '*keyring*' 'zulu*' 'firmware-*' 'openjdk-*' 'default-jre*' | tr '\n' ' ')"
paths=$(mktemp)
for c in $(find /sys/fs/cgroup/systemd/system.slice /sys/fs/cgroup/system.slice -maxdepth 3 -name cgroup.procs \
             -path '*.service/*' 2>/dev/null | grep -vE '/(lightdm|display-manager)\.service/'); do
  for p in $(cat "$c" 2>/dev/null); do
    readlink -f "/proc/$p/exe" 2>/dev/null
    awk '$6 ~ /^\// {print $6}' "/proc/$p/maps" 2>/dev/null
  done
done | grep -vE '/gio/modules/|/gvfs/' | sort -u | awk '{print; if ($0 ~ /^\/usr\//) print substr($0, 5); else print "/usr" $0}' >"$paths"
svc=$(grep -lxFf "$paths" /var/lib/dpkg/info/*.list 2>/dev/null | xargs -r -n1 basename | sed 's/\.list$//; s/:armhf$//' | sort -u)
rm -f "$paths"
protect=$(printf '%s\n' $PROTECT $svc | sort -u)

# Candidati: tolti i protetti
cand=$(printf '%s\n' "${DESK[@]}" "${JAVA[@]}" "${AUTO[@]}" | sed '/^$/d; s/:armhf$//' | sort -u | grep -vxF -f <(echo "$protect"))
mapfile -t ROOTS < <(echo "$cand" | sed '/^$/d')

# Cosa toglierebbe apt (simulazione); un errore di apt vuol dire "non si tocca",
# e anche un pacchetto essenziale nell'elenco. Il motivo resta in $remv_err
remv_err=$(mktemp)
remv() {
  local out; out=$(apt-get -s purge "$@" </dev/null 2>&1)
  if [[ $? -ne 0 ]]; then
    grep -E "^E:|: (Depends|PreDepends|Breaks|Conflicts):" <<<"$out" | sed 's/^ *//' | head -3 | tr '\n' ' ' >"$remv_err"
    return 1
  fi
  if grep -q "essential packages will be removed" <<<"$out"; then echo "toglierebbe pacchetti essenziali" >"$remv_err"; return 1; fi
  if grep -q "^Inst" <<<"$out"; then echo "installerebbe o aggiornerebbe: $(awk '/^Inst/{print $2}' <<<"$out" | head -5 | tr '\n' ' ')" >"$remv_err"; return 1; fi
  awk '/^Purg/{print $2}' <<<"$out" | sed 's/:armhf$//' | sort -u
}
# Prima tutti insieme: con loro se ne vanno i pacchetti che ne dipendono (applicazioni
# del desktop che l'elenco non nomina). Se apt rifiuta, o se fra questi c'e' un
# protetto, si prova uno per uno e restano quelli che apt non toglie da soli o che si
# porterebbero dietro un protetto (e si dice quale)
OK=("${ROOTS[@]}")
while [[ ${#OK[@]} -gt 0 ]]; do
  if all=$(remv "${OK[@]}"); then
    bad=$(grep -xF -f <(echo "$protect") <<<"$all" | sed '/^$/d')
    [[ -z "$bad" ]] && break
  else
    echo "pacchetti: tutti insieme apt li rifiuta ($(cat "$remv_err")): li provo uno per uno"
  fi
  new=()
  for r in "${OK[@]}"; do
    if ! one=$(remv "$r"); then echo "pacchetti: $r resta, apt da solo non lo toglie ($(cat "$remv_err"))"; continue; fi
    out_=$(grep -xF -f <(echo "$protect") <<<"$one" | sed '/^$/d' | tr '\n' ' ')
    if [[ -n "${out_// /}" ]]; then echo "pacchetti: $r resta, toglierlo porterebbe via anche $out_"; continue; fi
    new+=("$r")
  done
  if [[ ${#new[@]} -eq ${#OK[@]} ]]; then echo "pacchetti: NON tolti, apt non li toglie insieme ($(cat "$remv_err"))"; OK=(); break; fi
  OK=("${new[@]}")
done
ROOTS=("${OK[@]}")
desk_out=false
declare -A is_desk=()
for d in "${DESK[@]}"; do is_desk[$d]=1; done
for r in "${ROOTS[@]}"; do [[ -n "${is_desk[$r]:-}" ]] && desk_out=true; done

if [[ ${#ROOTS[@]} -gt 0 ]]; then
  # i protetti, segnati come installati a mano in una copia dello stato di apt: per
  # l'autoremove restano loro e le loro dipendenze. Non con nome+ (apt li porterebbe
  # alla versione candidata) ne' con nome=versione (per apt non conterebbero)
  keep_list=$(pkgs '*' | sed 's/:armhf$//' | grep -xF -f <(echo "$protect"))
  ext=$(mktemp); cp /var/lib/apt/extended_states "$ext" 2>/dev/null
  xargs -r apt-mark -o Dir::State::extended_states="$ext" manual <<<"$keep_list" >/dev/null 2>&1
  fin=$(apt-get -s -o Dir::State::extended_states="$ext" -o APT::Get::AutomaticRemove=1 purge "${ROOTS[@]}" </dev/null 2>&1)
  rc=$?
  rm -f "$ext"
  FINAL=$(awk '/^Purg/{print $2}' <<<"$fin" | sed 's/:armhf$//' | sort -u | grep -vxF -f <(echo "$protect") | tr '\n' ' ')
  if [[ $rc -ne 0 ]]; then
    echo "pacchetti: NON tolti, apt non trova una soluzione: $(grep -E '^E:|: (Depends|PreDepends|Breaks|Conflicts):' <<<"$fin" | sed 's/^ *//' | head -3 | tr '\n' ' ')"
    desk_out=false
  elif [[ -n "${FINAL// /}" ]]; then
    kb=$(dpkg-query -W -f='${Installed-Size}\n' $FINAL 2>/dev/null | awk '{s+=$1} END{print s+0}')
    echo "pacchetti: $(wc -w <<<"$FINAL") da togliere, circa $((kb/1024)) MB: $FINAL"
    # apt non deve togliere nulla oltre l'elenco
    sim=$(remv $FINAL | tr '\n' ' ')
    if [[ -z "${sim// /}" ]]; then
      echo "pacchetti: NON tolti, apt da solo non li toglie ($(cat "$remv_err"))"
      desk_out=false
    elif [[ "$sim" != "$FINAL" ]]; then
      echo "pacchetti: NON tolti, apt toglierebbe anche: $(comm -13 <(tr ' ' '\n' <<<"$FINAL" | sort) <(tr ' ' '\n' <<<"$sim" | sort) | tr '\n' ' ')"
      desk_out=false
    elif $APPLY; then
      cfg_before=$(cat /boot/config.txt /boot/firmware/config.txt 2>/dev/null)
      cmd_before=$(cat /boot/cmdline.txt /boot/firmware/cmdline.txt 2>/dev/null)
      # cio' che resta non deve diventare "inutile" per un autoremove futuro
      xargs -r apt-mark manual <<<"$keep_list" >/dev/null
      $desk_out && systemctl set-default multi-user.target >/dev/null 2>&1
      DEBIAN_FRONTEND=noninteractive apt-get purge -y -q $FINAL </dev/null 2>&1 | grep -E "^(E:|W:)" || true
      systemctl daemon-reload
      [[ "$(cat /boot/config.txt /boot/firmware/config.txt 2>/dev/null)" != "$cfg_before" ]] \
        && echo "ATTENZIONE: config.txt e' cambiato dopo la rimozione dei pacchetti: controllarlo prima di riavviare"
      # lo splash di avvio (rpd-plym-splash) toglie da cmdline.txt "quiet splash
      # plymouth.ignore-serial-consoles": e' atteso. Altro va guardato
      cmd_after=$(cat /boot/cmdline.txt /boot/firmware/cmdline.txt 2>/dev/null)
      if [[ "$cmd_after" != "$cmd_before" ]]; then
        gone=$(comm -23 <(tr ' ' '\n' <<<"$cmd_before" | sort -u) <(tr ' ' '\n' <<<"$cmd_after" | sort -u) | tr '\n' ' ')
        added=$(comm -13 <(tr ' ' '\n' <<<"$cmd_before" | sort -u) <(tr ' ' '\n' <<<"$cmd_after" | sort -u) | tr '\n' ' ')
        if [[ -z "${added// /}" && -z "$(tr ' ' '\n' <<<"$gone" | grep -vE '^(quiet|splash|plymouth\..*|)$')" ]]; then
          echo "cmdline.txt: tolti $gone(lo splash di avvio se n'e' andato col desktop)"
        else
          echo "ATTENZIONE: cmdline.txt e' cambiato (tolti: $gone aggiunti: $added): controllarlo prima di riavviare"
        fi
      fi
      left=$(for x in $FINAL; do installed "$x" && echo "$x"; done | tr '\n' ' ')
      [[ -n "${left// /}" ]] && echo "pacchetti: rimasti installati: $left"
    fi
  else
    desk_out=false
  fi
fi
if $desk_out; then
  echo "desktop: tolto, l'avvio passa a multi-user.target (oggi $(systemctl get-default))"
  # cache e profili di Chromium degli utenti, se Chromium se ne va
  chromium_out=false
  grep -qE '(^| )(chromium-browser|chromium)( |$)' <<<"$FINAL" && chromium_out=true
  if $APPLY && { installed chromium-browser || installed chromium; }; then chromium_out=false; fi
  if $chromium_out; then
    for p in /home/*/.config/chromium /home/*/.cache/chromium /root/.config/chromium /root/.cache/chromium; do
      [[ -e "$p" ]] || continue
      echo "chromium: $p ($(size_of "$p"))"
      run rm -rf "$p"
    done
  fi
fi

# 4. Cache apt, journal, log ruotati
echo "cache apt: $(size_of /var/cache/apt), journal: $(size_of /var/log/journal), /var/log: $(size_of /var/log)"
run apt-get clean </dev/null
run journalctl --vacuum-size=200M >/dev/null 2>&1 || true
run find /var/log -type f \( -name "*.gz" -o -name "*.[0-9]" -o -name "*.old" \) -delete 2>/dev/null || true
big=$(find /var/log -xdev -type f -size +100M -printf '%s %p\n' 2>/dev/null | sort -rn | head -3 \
        | awk '{printf "%s (%d MB) ", $2, $1/1048576}')
[[ -n "$big" ]] && echo "log grandi, da guardare (non toccati): $big"

echo "disco prima: $before, dopo: $(df -h / | tail -1 | awk '{print $5" ("$4" liberi)"}')"
for s in openhab2 openhab docker; do
  systemctl is-enabled --quiet "$s" 2>/dev/null && echo "$s: $(systemctl is-active "$s")"
done
if systemctl is-active --quiet openhab2 || systemctl is-active --quiet openhab; then
  echo "REST di OpenHAB: $(curl -s -m 20 -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/rest/ </dev/null)"
fi
rm -f "$remv_err"
return 0
}

main
