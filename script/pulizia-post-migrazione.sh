#!/bin/bash
###############################################################################
# Pulizia di una centralina dopo la migrazione al controller, quando l'impianto
# e' confermato in esercizio. Gira sulla centralina, da root.
#
#   sudo bash pulizia-post-migrazione.sh              # prova a vuoto: dice cosa toglierebbe
#   sudo bash pulizia-post-migrazione.sh --apply      # toglie davvero
#   sudo bash pulizia-post-migrazione.sh --apply --nativo   # anche l'OpenHAB nativo
#
# Dal PC (o dallo Script Hub), su piu' centraline: lo script si manda da solo via
# ssh a ognuna, con le stesse opzioni; una centralina che non risponde non ferma le altre.
#   ./script/pulizia-post-migrazione.sh --centralina <alias> --centralina <alias> [--apply] [--nativo]
#
# Toglie:
#   - i backup pre-migrazione /opt/docker_store-backup-*.tar.gz;
#   - le immagini docker che nessun container usa e che arfea.yml non nomina
#     (restano arfea-controller e python:3.11-slim, che serve al rebuild OTA);
#   - cache apt, journal oltre 200 MB, log ruotati;
#   - con --nativo: OpenHAB/HABApp/frontail nativi (pacchetti senza purge,
#     /etc/openhab, /var/lib/openhab, /usr/share/openhab, /var/log/openhab,
#     /opt/habapp) e i mount /srv/openhab-* di openHABian. Dopo, tornare al
#     nativo non e' piu' possibile.
# Non tocca: /root (dump del database di deasy, che il backup del controller
# esclude, e archivi di Undici), deasy/Undici, i backup del controller, i
# volumi docker. Non parte durante un backup o un aggiornamento di versione.
###############################################################################
set -u

APPLY=false; NATIVO=false; HOSTS=(); FWD=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) APPLY=true; FWD+=(--apply) ;;
    --nativo) NATIVO=true; FWD+=(--nativo) ;;
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

[[ $EUID -eq 0 ]] || { echo "va lanciato da root (o dal PC con --centralina)"; exit 1; }
$APPLY || echo "PROVA A VUOTO: niente viene tolto (--apply per farlo)"

run() { if $APPLY; then "$@"; fi; }
state_of() {
  curl -s -m 10 "http://127.0.0.1:8888/api/$1" \
    | python3 -c "import json,sys; print(json.load(sys.stdin).get('state',''))" 2>/dev/null
}

before=$(df -h / | tail -1 | awk '{print $5" ("$4" liberi)"}')

# Mai durante un backup (ferma i container) o un aggiornamento di versione
case "$(state_of backup/status)" in idle|completed|failed|"") ;; *) echo "backup in corso: riprova quando ha finito"; exit 0 ;; esac
case "$(state_of system/releases/status)" in idle|completed|failed|done|"") ;; *) echo "aggiornamento in corso: riprova quando ha finito"; exit 0 ;; esac

# 1. Backup pre-migrazione di /opt/docker_store
for f in /opt/docker_store-backup-*.tar.gz; do
  [[ -f "$f" ]] || continue
  echo "backup pre-migrazione: $f ($(du -h "$f" | cut -f1))"
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
    [[ -e "$d" ]] && { echo "nativo: $d ($(du -sh "$d" 2>/dev/null | cut -f1))"; found=true; }
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
    DEBIAN_FRONTEND=noninteractive apt-get remove -y -qq openhab openhab-addons >/dev/null 2>&1 \
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
used=$(docker ps -aq | xargs -r docker inspect -f '{{.Image}}' | sort -u)
while read -r id tag size; do
  case "$tag" in arfea-controller:*|python:3.11-slim|"<none>:<none>") continue ;; esac
  grep -qx "$id" <<<"$used" && continue
  grep -qx "$tag" <<<"$keep" && continue
  echo "immagine non usata: $tag ($size)"
  if $APPLY; then docker rmi "$tag" >/dev/null 2>&1 || echo "  non tolta: $tag"; fi
done < <(docker image ls --no-trunc --format '{{.ID}} {{.Repository}}:{{.Tag}} {{.Size}}')
run docker image prune -f >/dev/null 2>&1 || true

# 4. Cache apt, journal, log ruotati
echo "cache apt: $(du -sh /var/cache/apt 2>/dev/null | cut -f1), journal: $(du -sh /var/log/journal 2>/dev/null | cut -f1)"
run apt-get clean
run journalctl --vacuum-size=200M >/dev/null 2>&1 || true
run find /var/log -type f \( -name "*.gz" -o -name "*.[0-9]" -o -name "*.old" \) -delete 2>/dev/null || true

echo "disco prima: $before, dopo: $(df -h / | tail -1 | awk '{print $5" ("$4" liberi)"}')"
docker ps -a --format '{{.Names}} {{.Status}}' | grep -v " Up " | sed 's/^/container non su: /' || true
