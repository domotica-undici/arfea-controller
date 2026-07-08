#!/bin/bash
# ─────────────────────────────────────────────────────────────
# linphone_call.sh — Chiamata di emergenza ARFEA
#
# Eseguito DENTRO il container OpenHAB come utente 'openhab'
# (dal controller via: docker exec -u openhab openhab .../linphone_call.sh).
#
# La sequenza linphonecsh è allineata allo script JS OpenHAB collaudato:
#   exit -> init -> register -> status -> "soundcard use files" -> play -> dial -> exit
# La differenza è che qui il WAV è generato al volo dal messaggio (TTS offline).
#
# Uso:  linphone_call.sh [numero] [messaggio]
#   - numero    : opzionale, default EMERGENCY_NUMBER da linphone.env
#   - messaggio : opzionale, default MESSAGE da linphone.env
# ─────────────────────────────────────────────────────────────
set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$SCRIPT_DIR/linphone.env"
WAV="/tmp/arfea_call.wav"

log() { echo "[linphone_call] $*"; }

# --- 0) configurazione --------------------------------------------------------
if [ ! -f "$ENV_FILE" ]; then
  log "ERRORE: $ENV_FILE non trovato (configurare linphone dal controller)"; exit 2
fi
# shellcheck disable=SC1090
. "$ENV_FILE"

NUMBER="${1:-${EMERGENCY_NUMBER:-}}"
MESSAGE="${2:-${MESSAGE:-Allarme dal sistema domotico}}"
TIMEOUT="${CALL_TIMEOUT:-30}"
REPEAT="${REPEAT:-2}"

if [ "${ENABLED:-0}" != "1" ]; then log "linphone non abilitato"; exit 2; fi
if [ -z "$NUMBER" ]; then log "ERRORE: nessun numero da comporre"; exit 2; fi
if [ -z "${SIP_HOST:-}" ] || [ -z "${SIP_USER:-}" ]; then
  log "ERRORE: SIP non configurato (SIP_HOST/SIP_USER)"; exit 2
fi

# --- 1) WAV del messaggio -----------------------------------------------------
# Preferito: WAV gTTS (voce naturale, 8 kHz mono) generato dal controller in
# conf/sounds/arfea_call.wav PRIMA della chiamata. Se assente (rete giù o
# gTTS fallito) si ricade su TTS offline espeak-ng/pico2wave (voce robotica).
PREBUILT="/openhab/conf/sounds/arfea_call.wav"
if [ -s "$PREBUILT" ]; then
  WAV="$PREBUILT"
  log "uso WAV gTTS: $PREBUILT"
else
  rm -f "$WAV"
  if command -v pico2wave >/dev/null 2>&1; then
    pico2wave -l it-IT -w "$WAV" "$MESSAGE" || { log "ERRORE: TTS pico2wave fallito"; exit 3; }
  elif command -v espeak-ng >/dev/null 2>&1; then
    espeak-ng -v it -w "$WAV" "$MESSAGE" || { log "ERRORE: TTS espeak-ng fallito"; exit 3; }
  else
    log "ERRORE: nessun motore TTS installato (pico2wave/espeak-ng)"; exit 3
  fi
  log "uso WAV offline (espeak/pico): $WAV"
fi

# --- 2) reset demone (stato pulito) -------------------------------------------
linphonecsh exit >/dev/null 2>&1 || true
sleep 1
linphonecsh init >/dev/null 2>&1 || true

# Attendi che il demone sia PRONTO (pipe creata) prima di registrare: altrimenti
# 'register' fallisce con "Failed to connect pipe" e non viene mai consegnato.
ready=0
for _ in $(seq 1 15); do
  if linphonecsh status register >/dev/null 2>&1; then ready=1; break; fi
  sleep 1
done
if [ "$ready" != "1" ]; then
  log "ERRORE: demone linphone non pronto"
  linphonecsh exit >/dev/null 2>&1 || true
  exit 4
fi

# --- 3) registrazione SIP (attendi che sia effettiva) -------------------------
linphonecsh register --host "$SIP_HOST" --username "$SIP_USER" --password "$SIP_PASS" >/dev/null 2>&1 || true
RESP=""
for _ in $(seq 1 15); do
  RESP="$(linphonecsh status register 2>/dev/null)"
  if printf '%s' "$RESP" | grep -q "identity"; then break; fi
  sleep 1
done

# --- 4) verifica registrazione ------------------------------------------------
if ! printf '%s' "$RESP" | grep -q "identity"; then
  log "ERRORE registrazione SIP: ${RESP:-nessuna risposta}"
  linphonecsh exit >/dev/null 2>&1 || true
  exit 4
fi

# --- 5) modalità file + arma il playback (PRIMA della chiamata) ---------------
linphonecsh generic "soundcard use files" >/dev/null 2>&1
sleep 0.2
linphonecsh generic "play $WAV" >/dev/null 2>&1

# --- 6) chiamata --------------------------------------------------------------
log "chiamata verso $NUMBER"
linphonecsh dial "$NUMBER" >/dev/null 2>&1

# --- 7) ripeti il messaggio nella finestra di chiamata, poi chiudi ------------
STEP=$(( TIMEOUT / ( REPEAT > 0 ? REPEAT : 1 ) ))
[ "$STEP" -lt 1 ] && STEP=1
i=1
while [ "$i" -lt "$REPEAT" ]; do
  sleep "$STEP"
  linphonecsh generic "play $WAV" >/dev/null 2>&1
  i=$(( i + 1 ))
done
sleep "$STEP"

# --- 8) chiusura --------------------------------------------------------------
linphonecsh exit >/dev/null 2>&1 || true
log "fine (numero=$NUMBER)"
