#!/bin/bash
###############################################################################
# Definizione UNICA del sottoinsieme HABApp distribuibile.
#
# Sorgente di verita': habapp/25.12.4/habapp/ nella repo privata. Da li' i
# sorgenti vengono presi COSI' COME SONO da:
#   - script/build-update-tarball.sh -> dentro arfea-controller.tar.xz (OTA),
#     da cui il controller deploya le sole funzioni scelte sull'impianto;
#   - script/export-public.sh        -> nella repo pubblica.
# Nessuna copia intermedia: si modifica la repo e i due canali seguono.
#
# Cosa NON e' distribuibile e perche':
#   rules/aasystem/      arfea.py e' legacy (buona parte gia' portata in
#                        conf/automation/js/arfea_system.js); time.py e' stato
#                        rimosso perche' duplicava le fasce del JS.
#                        ECCEZIONE: tools.py, vedi HABAPP_RULE_FILES.
#   params/              configurazione DELL'IMPIANTO, non un template: thermo.yml
#                        e' un impianto reale. Sull'impianto i params li crea il
#                        controller vuoti ({}) e li si edita dalla Web UI.
#   config/              thing_*.yml non servono alle tre funzioni
#   log/                 runtime
###############################################################################

HABAPP_VER="25.12.4"
HABAPP_SRC_REL="habapp/${HABAPP_VER}/habapp"

# lib/ deployabili. system/ e' la base comune (utils.py), le altre sono per-funzione.
HABAPP_LIB_DIRS=(system thermostats irrigation loads)

# rules/ deployabili: una per funzione attivabile dalla Web UI.
# Deve restare allineato a _FUNCTIONS in arfea-controller/app/habapp_manager.py,
# che decide cosa copiare sull'impianto in base alle funzioni scelte.
HABAPP_RULE_DIRS=(thermostats irrigation loads)

# rules/ deployabili file per file: la BASE COMUNE, che serve a tutte le funzioni
# e non appartiene a nessuna. aasystem/tools.py definisce la regola 'Tools' (i
# timer), che termostati, carichi e irrigazione prendono con get_rule('Tools').
# Senza, get_rule solleva KeyError e la funzione muore al primo termostato/carico
# /zona: l'impianto resta con i soli item globali e nessuno se ne accorge, perche'
# l'errore finisce solo in HABApp.log. Il resto di aasystem/ resta escluso.
HABAPP_RULE_FILES=(aasystem/tools.py)

# File singoli alla radice di habapp/
HABAPP_ROOT_FILES=(config.yml logging.yml)

# Path (relativi alla radice della repo) esclusi dalla repo pubblica.
# Usata da export-public.sh, che filtra l'output di `git ls-files`.
# NB: di rules/aasystem/ si esclude arfea.py, non la cartella: tools.py e' in
# HABAPP_RULE_FILES e deve uscire anche nel pubblico.
HABAPP_EXCLUDE_REGEX="^habapp/${HABAPP_VER//./\\.}/habapp/(rules/aasystem/arfea\.py|params/|config/|log/)"
