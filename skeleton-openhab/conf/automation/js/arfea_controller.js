// ─────────────────────────────────────────────────────────────
// ARFEA Controller – OpenHAB JS Scripting rules
//
// Comunicano con arfea-controller via REST API (porta 8888).
// Triggered from widget buttons via action:rule + actionRuleContext.
// ─────────────────────────────────────────────────────────────

var HTTP = Java.type('org.openhab.core.model.script.actions.HTTP');
var logger = Java.type('org.slf4j.LoggerFactory').getLogger('org.openhab.rule.arfea');

var BASE_URL = 'http://localhost:8888/api';
var TIMEOUT = 15000;

// OpenHAB calls from localhost — no API key needed (controller trusts localhost)
function httpGet(path) {
  return HTTP.sendHttpGetRequest(BASE_URL + path, TIMEOUT);
}
function httpPost(path) {
  return HTTP.sendHttpPostRequest(BASE_URL + path, 'application/json', '', TIMEOUT);
}
function httpPut(path) {
  return HTTP.sendHttpPutRequest(BASE_URL + path, 'application/json', '', TIMEOUT);
}

// Map service names in arfea.yml → item name fragments
var SERVICE_ITEM_MAP = {
  'openhab':      'openhab',
  'samba':        'samba',
  'mosquitto':    'mosquitto',
  'habapp':       'habapp',
  'zwave-js-ui':  'zwave',
  'zigbee2mqtt':  'zigbee2mqtt',
  'node-red':     'nodered'
};

// ─────────────────────────────────────────────────────────────
// Main rule: called from widget with actionRuleContext
// ─────────────────────────────────────────────────────────────

rules.JSRule({
  name: 'ARFEA Controller Actions',
  id: 'arfea_controller',
  // Dummy trigger (29 feb 2099, non scatta mai): serve a forzare la registrazione
  // della regola nel rule registry di OpenHAB per renderla callable via REST runnow.
  // Con triggers: [] la regola si perde dopo il riavvio di OpenHAB.
  triggers: [triggers.GenericCronTrigger('0 0 0 29 2 ? 2099')],
  execute: function (event) {
    var action = '';
    var target = '';

    // runnow context is in event.raw (Java Map) in OpenHAB 4+/5
    try {
      var raw = event && event.raw;
      if (raw && typeof raw.get === 'function') {
        action = String(raw.get('action') || '');
        target = String(raw.get('target') || '');
      }
    } catch (e) {
      logger.warn('ARFEA: error reading context: {}', e.message);
    }

    logger.info('ARFEA action={}, target={}', action, target);

    try {
      switch (action) {
        case 'start':
          doStart(target);
          break;
        case 'restart':
          doRestart(target);
          break;
        case 'enable':
          doEnable(target);
          break;
        case 'disable':
          doDisable(target);
          break;
        case 'backup':
          doBackup();
          break;
        case 'reboot':
          doReboot();
          break;
        case 'restore':
          doRestore(target);
          break;
        case 'refresh_backups':
          refreshBackupList();
          break;
        case 'vpn_start':
          doVpnStart();
          break;
        case 'vpn_stop':
          doVpnStop();
          break;
        case 'emergency_call':
          doEmergencyCall();
          break;
        case 'apply_update':
          doApplyUpdate();
          break;
        case 'refresh':
          refreshAll();
          break;
        default:
          logger.warn('ARFEA: unknown action "{}"', action);
      }
    } catch (e) {
      logger.error('ARFEA action failed: {}', e.message);
    }
  }
});

// ─────────────────────────────────────────────────────────────
// Periodic refresh: update all service states every 60 seconds
// ─────────────────────────────────────────────────────────────

rules.JSRule({
  name: 'ARFEA Status Refresh',
  id: 'arfea_status_refresh',
  triggers: [
    triggers.GenericCronTrigger('0 * * * * ?')  // every minute
  ],
  execute: function () {
    refreshAll();
  }
});

// ─────────────────────────────────────────────────────────────
// Toggle handler: react to Switch item changes
// ─────────────────────────────────────────────────────────────

rules.JSRule({
  name: 'ARFEA Service Toggle - HABApp',
  id: 'arfea_toggle_habapp',
  triggers: [triggers.ItemCommandTrigger('arfea_habapp_enabled')],
  execute: function (event) {
    toggleService('habapp', event.receivedCommand.toString());
  }
});

rules.JSRule({
  name: 'ARFEA Service Toggle - Z-Wave',
  id: 'arfea_toggle_zwave',
  triggers: [triggers.ItemCommandTrigger('arfea_zwave_enabled')],
  execute: function (event) {
    toggleService('zwave-js-ui', event.receivedCommand.toString());
  }
});

rules.JSRule({
  name: 'ARFEA Service Toggle - Zigbee2MQTT',
  id: 'arfea_toggle_zigbee2mqtt',
  triggers: [triggers.ItemCommandTrigger('arfea_zigbee2mqtt_enabled')],
  execute: function (event) {
    toggleService('zigbee2mqtt', event.receivedCommand.toString());
  }
});

rules.JSRule({
  name: 'ARFEA Service Toggle - Node-RED',
  id: 'arfea_toggle_nodered',
  triggers: [triggers.ItemCommandTrigger('arfea_nodered_enabled')],
  execute: function (event) {
    toggleService('node-red', event.receivedCommand.toString());
  }
});

// ─────────────────────────────────────────────────────────────
// Restore trigger: react to arfea_restore_target item command
// ─────────────────────────────────────────────────────────────

rules.JSRule({
  name: 'ARFEA Reboot Trigger',
  id: 'arfea_reboot_trigger_rule',
  triggers: [triggers.ItemCommandTrigger('arfea_reboot_trigger')],
  execute: function (event) {
    if (event.receivedCommand.toString() === 'ON') {
      doReboot();
    }
  }
});

rules.JSRule({
  name: 'ARFEA Backup Trigger',
  id: 'arfea_backup_trigger_rule',
  triggers: [triggers.ItemCommandTrigger('arfea_backup_trigger')],
  execute: function (event) {
    if (event.receivedCommand.toString() === 'ON') {
      doBackup();
    }
  }
});

rules.JSRule({
  name: 'ARFEA Update Trigger',
  id: 'arfea_update_trigger_rule',
  triggers: [triggers.ItemCommandTrigger('arfea_update_trigger')],
  execute: function (event) {
    if (event.receivedCommand.toString() === 'ON') {
      var response = httpPost('/system/update');
      logger.info('Update trigger: {}', response);
    }
  }
});

rules.JSRule({
  name: 'ARFEA Apply Version Update Trigger',
  id: 'arfea_apply_update_trigger_rule',
  triggers: [triggers.ItemCommandTrigger('arfea_apply_update_trigger')],
  execute: function (event) {
    if (event.receivedCommand.toString() === 'ON') {
      doApplyUpdate();
    }
  }
});

rules.JSRule({
  name: 'ARFEA VPN Toggle',
  id: 'arfea_toggle_vpn',
  triggers: [triggers.ItemCommandTrigger('arfea_vpn_active')],
  execute: function (event) {
    var cmd = event.receivedCommand.toString();
    if (cmd === 'ON') {
      doVpnStart();
    } else {
      doVpnStop();
    }
  }
});

rules.JSRule({
  name: 'ARFEA Restore Trigger',
  id: 'arfea_restore_trigger',
  triggers: [triggers.ItemCommandTrigger('arfea_restore_target')],
  execute: function (event) {
    var backupName = event.receivedCommand.toString();
    if (backupName && backupName !== 'NULL' && backupName !== '') {
      doRestore(backupName);
    }
  }
});

// ─────────────────────────────────────────────────────────────
// Emergency call: invia ON ad arfea_emergency_call per chiamare.
// Da altre regole: doEmergencyCall('messaggio personalizzato')
// ─────────────────────────────────────────────────────────────

rules.JSRule({
  name: 'ARFEA Emergency Call',
  id: 'arfea_emergency_call_rule',
  triggers: [triggers.ItemCommandTrigger('arfea_emergency_call')],
  execute: function (event) {
    if (event.receivedCommand.toString() === 'ON') {
      doEmergencyCall();
    }
  }
});

// ─────────────────────────────────────────────────────────────
// Action functions
// ─────────────────────────────────────────────────────────────

function doStart(target) {
  var response = httpPost('/services/' + target + '/start');
  logger.info('Start {}: {}', target, response);
  java.lang.Thread.sleep(3000);
  refreshServiceStatus(target);
}

function doRestart(target) {
  var response = httpPost('/services/' + target + '/restart');
  logger.info('Restart {}: {}', target, response);
  // Refresh status after a short delay to let container restart
  java.lang.Thread.sleep(3000);
  refreshServiceStatus(target);
}

function doEnable(target) {
  var response = httpPut('/services/' + target + '/enable');
  logger.info('Enable {}: {}', target, response);
  refreshAll();
}

function doDisable(target) {
  var response = httpPut('/services/' + target + '/disable');
  logger.info('Disable {}: {}', target, response);
  refreshAll();
}

function toggleService(serviceName, command) {
  if (command === 'ON') {
    doEnable(serviceName);
  } else {
    doDisable(serviceName);
  }
}

function doRestore(backupName) {
  if (!backupName) {
    logger.warn('ARFEA: no backup name specified for restore');
    return;
  }
  items.getItem('arfea_backup_state').postUpdate('running');
  items.getItem('arfea_backup_message').postUpdate('Ripristino in corso da: ' + backupName);

  var response = httpPost('/backup/restore?backup_name=' + encodeURIComponent(backupName));
  logger.info('Restore started: {}', response);
  pollBackupStatus();
}

function refreshBackupList() {
  try {
    var response = httpGet('/backup/list');
    if (!response) return;
    var backups = JSON.parse(response);
    if (backups.length === 0) {
      items.getItem('arfea_backup_list').postUpdate('Nessun backup disponibile');
      return;
    }
    // Format as readable text
    var lines = [];
    for (var i = 0; i < backups.length; i++) {
      lines.push(backups[i].name + ' (' + backups[i].size_mb + ' MB, ' + backups[i].date + ')');
    }
    items.getItem('arfea_backup_list').postUpdate(lines.join('\n'));

    logger.info('ARFEA backup list updated: {} backups', backups.length);
  } catch (e) {
    logger.error('ARFEA refreshBackupList failed: {}', e.message);
  }
}

function doBackup() {
  items.getItem('arfea_backup_state').postUpdate('running');
  items.getItem('arfea_backup_message').postUpdate('Backup avviato...');

  var response = httpPost('/backup/run');
  logger.info('Backup started: {}', response);

  // Poll backup status until done
  pollBackupStatus();
}

function doVpnStart() {
  var response = httpPost('/system/vpn/start');
  logger.info('VPN start: {}', response);
  java.lang.Thread.sleep(3000);
  refreshVpnStatus();
}

function doVpnStop() {
  var response = httpPost('/system/vpn/stop');
  logger.info('VPN stop: {}', response);
  java.lang.Thread.sleep(2000);
  refreshVpnStatus();
}

function doReboot() {
  logger.warn('ARFEA: Host reboot requested');
  var response = httpPost('/system/reboot');
  logger.info('Reboot: {}', response);
}

// Chiamata di emergenza. message/number opzionali sovrascrivono i default del controller.
// Se message non è passato, usa l'item arfea_emergency_message (se valorizzato).
function doEmergencyCall(message, number) {
  if (!message) {
    try {
      var m = items.getItem('arfea_emergency_message').state;
      if (m && m.toString() !== 'NULL' && m.toString() !== '' && m.toString() !== 'UNDEF') {
        message = m.toString();
      }
    } catch (e) { /* item assente */ }
  }
  var qs = [];
  if (number) qs.push('number=' + encodeURIComponent(number));
  if (message) qs.push('message=' + encodeURIComponent(message));
  var path = '/linphone/call' + (qs.length ? '?' + qs.join('&') : '');
  var response = httpPost(path);
  logger.warn('ARFEA chiamata di emergenza: {}', response);
}

// Servizi con toggle di conferma per-software: frammento item -> nome servizio
var UPDATE_ITEM_MAP = {
  'openhab':      'openhab',
  'habapp':       'habapp',
  'zwave':        'zwave-js-ui',
  'zigbee2mqtt':  'zigbee2mqtt',
  'nodered':      'node-red'
};

// Aggiornamento di versione (release certificate). Rispetta la conferma
// software-per-software: aggiorna solo i componenti con toggle arfea_upd_<x>_ok
// su ON e con un aggiornamento effettivamente disponibile.
function doApplyUpdate() {
  var selected = [];
  for (var frag in UPDATE_ITEM_MAP) {
    try {
      var avail = items.getItem('arfea_upd_' + frag).state;
      var ok = items.getItem('arfea_upd_' + frag + '_ok').state;
      if (avail && avail.toString() !== '' && avail.toString() !== 'NULL'
          && avail.toString() !== 'UNDEF' && ok && ok.toString() === 'ON') {
        selected.push(UPDATE_ITEM_MAP[frag]);
      }
    } catch (e) { /* item assente */ }
  }
  if (selected.length === 0) {
    items.getItem('arfea_update_progress').postUpdate('Nessun software selezionato');
    logger.warn('ARFEA apply update: nessun software selezionato');
    return;
  }
  items.getItem('arfea_update_progress').postUpdate('Avvio aggiornamento...');
  var response = httpPost('/system/releases/apply?services=' + encodeURIComponent(selected.join(',')));
  logger.warn('ARFEA apply update ({}): {}', selected.join(','), response);
  pollReleaseStatus();
}

// ─────────────────────────────────────────────────────────────
// Refresh functions
// ─────────────────────────────────────────────────────────────

function refreshAll() {
  refreshServices();
  refreshNetwork();
  refreshVpnStatus();
  refreshSystemInfo();
  refreshBackupStatus();
  refreshBackupList();
  refreshLinphoneStatus();
  refreshReleaseCheck();
  refreshReleaseStatus();
}

// Riconcilia l'avanzamento dell'upgrade nel giro periodico: serve soprattutto
// dopo che un upgrade ha ricreato OpenHAB (il thread del poll muore col container,
// ma il controller completa l'apply per conto suo). Mostra il progresso solo se
// un aggiornamento è effettivamente in corso o concluso da poco.
function refreshReleaseStatus() {
  try {
    var response = httpGet('/system/releases/status');
    if (!response) return;
    var st = JSON.parse(response);
    if (!st.state || st.state === 'idle') return;
    var label = st.message || st.state;
    if (st.step) label = '[' + st.step + '] ' + label;
    items.getItem('arfea_update_progress').postUpdate(label);
  } catch (e) {
    logger.error('ARFEA refreshReleaseStatus failed: {}', e.message);
  }
}

// Controlla se esiste una release certificata più recente e valorizza gli item
// che l'app mostra all'utente (versione attuale, disponibilità, novità).
function refreshReleaseCheck() {
  try {
    var response = httpGet('/system/releases/check');
    if (!response) return;
    var res = JSON.parse(response);
    items.getItem('arfea_current_release').postUpdate(res.current_release || 'n/d');
    if (res.update_available) {
      items.getItem('arfea_update_available').postUpdate(res.latest_release || '');
      items.getItem('arfea_update_changelog').postUpdate(res.notes || '');
    } else {
      items.getItem('arfea_update_available').postUpdate('');
      items.getItem('arfea_update_changelog').postUpdate(res.error ? ('errore: ' + res.error) : 'sistema aggiornato');
    }

    // Diff per-software: mappa nome servizio -> versione target
    var byService = {};
    var svcList = res.services || [];
    for (var i = 0; i < svcList.length; i++) {
      byService[svcList[i].name] = svcList[i].target_image;
    }
    for (var frag in UPDATE_ITEM_MAP) {
      var svcName = UPDATE_ITEM_MAP[frag];
      var target = byService[svcName];
      try {
        if (target) {
          items.getItem('arfea_upd_' + frag).postUpdate(target);
          items.getItem('arfea_upd_' + frag + '_ok').postUpdate('ON');   // default: aggiorna
        } else {
          items.getItem('arfea_upd_' + frag).postUpdate('');
          items.getItem('arfea_upd_' + frag + '_ok').postUpdate('OFF');
        }
      } catch (e) { /* item assente */ }
    }
  } catch (e) {
    logger.error('ARFEA refreshReleaseCheck failed: {}', e.message);
  }
}

// Segue l'avanzamento dell'aggiornamento di versione fino a fine/errore.
function pollReleaseStatus() {
  // Poll ogni 15s per max 40 minuti (upgrade + backup possono essere lunghi)
  var maxAttempts = 160;
  for (var i = 0; i < maxAttempts; i++) {
    java.lang.Thread.sleep(15000);
    try {
      var response = httpGet('/system/releases/status');
      if (!response) continue;
      var st = JSON.parse(response);
      var label = st.message || st.state;
      if (st.step) label = '[' + st.step + '] ' + label;
      items.getItem('arfea_update_progress').postUpdate(label);

      if (st.state === 'completed' || st.state === 'failed' || st.state === 'rolled_back') {
        refreshServices();
        refreshReleaseCheck();
        return;
      }
    } catch (e) {
      logger.error('ARFEA pollReleaseStatus error: {}', e.message);
    }
  }
  logger.warn('ARFEA: polling aggiornamento versione scaduto');
}

function refreshLinphoneStatus() {
  try {
    var response = httpGet('/linphone/status');
    if (!response) return;
    var st = JSON.parse(response);
    var text;
    if (!st.enabled) {
      text = 'disabilitato';
    } else if (/identity|registered/i.test(st.registration || '')) {
      text = 'registrato';
    } else {
      text = st.registration || 'sconosciuto';
    }
    items.getItem('arfea_linphone_status').postUpdate(text);
  } catch (e) {
    logger.error('ARFEA refreshLinphoneStatus failed: {}', e.message);
  }
}

function refreshServices() {
  try {
    var response = httpGet('/services');
    if (!response) return;

    var services = JSON.parse(response);
    for (var i = 0; i < services.length; i++) {
      var svc = services[i];
      updateServiceItems(svc);
    }
  } catch (e) {
    logger.error('ARFEA refreshServices failed: {}', e.message);
  }
}

function refreshServiceStatus(serviceName) {
  try {
    var response = httpGet('/services/' + serviceName);
    if (!response) return;
    var svc = JSON.parse(response);
    updateServiceItems(svc);
  } catch (e) {
    logger.error('ARFEA refreshServiceStatus({}) failed: {}', serviceName, e.message);
  }
}

function updateServiceItems(svc) {
  var fragment = SERVICE_ITEM_MAP[svc.name];
  if (!fragment) return;

  // Update state item
  var stateItemName = 'arfea_' + fragment + '_state';
  try {
    items.getItem(stateItemName).postUpdate(svc.state);
  } catch (e) { /* item may not exist */ }

  // Update enabled toggle (only for optional services)
  if (!svc.core) {
    var enabledItemName = 'arfea_' + fragment + '_enabled';
    try {
      var enabledItem = items.getItem(enabledItemName);
      var shouldBeOn = svc.enabled || svc.effectively_enabled;
      enabledItem.postUpdate(shouldBeOn ? 'ON' : 'OFF');
    } catch (e) { /* item may not exist */ }
  }
}

function refreshVpnStatus() {
  try {
    var response = httpGet('/system/vpn');
    if (!response) return;

    var vpn = JSON.parse(response);
    items.getItem('arfea_vpn_active').postUpdate(vpn.active ? 'ON' : 'OFF');
  } catch (e) {
    logger.error('ARFEA refreshVpnStatus failed: {}', e.message);
  }
}

function refreshNetwork() {
  try {
    var response = httpGet('/system/network');
    if (!response) return;

    var net = JSON.parse(response);
    items.getItem('arfea_lan_ip').postUpdate(net.lan_ip || 'N/A');
    items.getItem('arfea_vpn_ip').postUpdate(net.vpn_ip || 'N/A');
    items.getItem('arfea_external_ip').postUpdate(net.external_ip || 'N/A');
  } catch (e) {
    logger.error('ARFEA refreshNetwork failed: {}', e.message);
  }
}

function refreshSystemInfo() {
  try {
    var response = httpGet('/system/info');
    if (!response) return;

    var info = JSON.parse(response);
    items.getItem('arfea_hostname').postUpdate(info.hostname || '');
    items.getItem('arfea_uptime').postUpdate(info.uptime || '');
    // Versione di arfea-controller (VERSION in main.py), esposta da /system/info
    items.getItem('arfea_controller_version').postUpdate(info.version || 'n/d');
  } catch (e) {
    logger.error('ARFEA refreshSystemInfo failed: {}', e.message);
  }

  // OpenHAB version (from Java API, not via controller)
  try {
    var OpenHAB = Java.type('org.openhab.core.OpenHAB');
    items.getItem('ohVersion').postUpdate(OpenHAB.getVersion());
  } catch (e) {
    logger.error('ARFEA ohVersion failed: {}', e.message);
  }
}

function refreshBackupStatus() {
  try {
    var response = httpGet('/backup/status');
    if (!response) return;

    var status = JSON.parse(response);
    items.getItem('arfea_backup_state').postUpdate(status.state || 'idle');
    items.getItem('arfea_backup_message').postUpdate(status.message || '');
  } catch (e) {
    logger.error('ARFEA refreshBackupStatus failed: {}', e.message);
  }
}

function pollBackupStatus() {
  // Poll every 10 seconds for up to 10 minutes
  var maxAttempts = 60;
  for (var i = 0; i < maxAttempts; i++) {
    java.lang.Thread.sleep(10000);
    try {
      var response = httpGet('/backup/status');
      if (!response) continue;

      var status = JSON.parse(response);
      items.getItem('arfea_backup_state').postUpdate(status.state);
      items.getItem('arfea_backup_message').postUpdate(status.message || '');

      if (status.state === 'completed' || status.state === 'failed' || status.state === 'idle') {
        refreshServices();
        return;
      }
    } catch (e) {
      logger.error('ARFEA pollBackupStatus error: {}', e.message);
    }
  }
  logger.warn('ARFEA: backup polling timed out');
}
