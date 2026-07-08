#!/bin/bash

# Funzioni di sistema per OpenHAB
# Le seguenti funzioni sono ora gestite da arfea-controller:
#   - restart container (API: /api/services/{name}/restart)
#   - backup/restore (API: /api/backup/run, /api/backup/restore)
#   - network IP detection (API: /api/system/network)
#   - store_ip cronjob (non più necessario)
#
# Restano qui solo le funzioni che operano su file locali di OpenHAB.

ohVersion()
{
  RES=$(cat /openhab/userdata/etc/version.properties | grep openhab-distro | awk '{print $3}')
  echo $RES
}

change_log_level(){
  LOG_FILE=/openhab/userdata/etc/log4j2.xml
  sed -i 's/level="[[:alpha:]]*" name="org.openhab"/level="'$1'" name="org.openhab"/g' $LOG_FILE
  sed -i 's/level="[[:alpha:]]*" name="openhab.event.ItemStateEvent"/level="'$1'" name="openhab.event.ItemStateEvent"/g' $LOG_FILE
  sed -i 's/level="[[:alpha:]]*" name="openhab.event.ItemStateChangedEvent"/level="'$1'" name="openhab.event.ItemStateChangedEvent"/g' $LOG_FILE
  sed -i 's/level="[[:alpha:]]*" name="openhab.event.GroupItemStateChangedEvent"/level="'$1'" name="openhab.event.GroupItemStateChangedEvent"/g' $LOG_FILE
  sed -i 's/level="[[:alpha:]]*" name="openhab.event"/level="'$1'" name="openhab.event"/g' $LOG_FILE
}

change_habapp_log_level(){
  LOG_FILE=/openhab/conf/habapp/logging.yml
  sed -i "s/level: [[:alpha:]]*/level: $1/g" $LOG_FILE
}

check_file_existance()
{
  if [ ! -f $1 ]; then
    echo "no"
  fi
}

FUNCTION=$1
PAR2=$2

if [ "$FUNCTION" = "ohVersion" ]; then
  ohVersion
elif [ "$FUNCTION" = "change_log_level" ]; then
  change_log_level $PAR2
elif [ "$FUNCTION" = "change_habapp_log_level" ]; then
  change_habapp_log_level $PAR2
elif [ "$FUNCTION" = "check_file_existance" ]; then
  check_file_existance $PAR2
else
  echo "Unknown function: $FUNCTION"
fi
