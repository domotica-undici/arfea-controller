#!/bin/bash
###############################################################################
# Definizione UNICA del sottoinsieme HABApp distribuibile.
#
# Sorgente di verita': habapp/25.12.0/habapp/ nella repo privata. Da li' i
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
#   rules/accessControl/ specifico cliente
#   rules/infraRed/      specifico cliente + contiene un certificato (pecosoft.ca)
#   params/              configurazione DELL'IMPIANTO, non un template: thermo.yml
#                        e' un impianto reale. Sull'impianto i params li crea il
#                        controller vuoti ({}) e li si edita dalla Web UI.
#   config/              thing_*.yml non servono alle tre funzioni
#   log/                 runtime
###############################################################################

HABAPP_VER="25.12.0"
HABAPP_SRC_REL="habapp/${HABAPP_VER}/habapp"

# lib/ deployabili. system/ e' la base comune (utils.py), le altre sono per-funzione.
HABAPP_LIB_DIRS=(system thermostats irrigation loads)

# rules/ deployabili: una per funzione attivabile dalla Web UI.
# Deve restare allineato a _FUNCTIONS in arfea-controller/app/habapp_manager.py,
# che decide cosa copiare sull'impianto in base alle funzioni scelte.
HABAPP_RULE_DIRS=(thermostats irrigation loads)

# File singoli alla radice di habapp/
HABAPP_ROOT_FILES=(config.yml logging.yml)

# Path (relativi alla radice della repo) esclusi dalla repo pubblica.
# Usata da export-public.sh, che filtra l'output di `git ls-files`.
HABAPP_EXCLUDE_REGEX="^habapp/${HABAPP_VER//./\\.}/habapp/(rules/(aasystem|accessControl|infraRed)/|params/|config/|log/)"
