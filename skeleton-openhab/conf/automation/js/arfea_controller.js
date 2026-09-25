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
function httpPostJson(path, obj) {
  return HTTP.sendHttpPostRequest(BASE_URL + path, 'application/json', JSON.stringify(obj), TIMEOUT);
}
function httpPut(path) {
  return HTTP.sendHttpPutRequest(BASE_URL + path, 'application/json', '', TIMEOUT);
}

// Contesto passato dal widget (actionRuleContext) quando la regola parte con
// "run now". openhab-js lo mette in event.raw, ma non sempre nella stessa forma:
// mappa Java fino alla 5.19 e dalla 5.21, oggetto JS nella 5.20 (quella di
// OpenHAB 5.2.0). Leggerlo solo con raw.get() lasciava l'azione vuota su
// OpenHAB 5.2.0, e nessun pulsante del widget funzionava (Redmine #198). Le
// chiavi possono arrivare anche col prefisso del modulo ("<modulo>.action").
function contextValue(event, key) {
  var raw = event && event.raw;
  if (!raw) return '';
  var isMap = typeof raw.get === 'function' && typeof raw.keySet === 'function';
  var v = null;
  try {
    v = isMap ? raw.get(key) : raw[key];
    if (v === null || v === undefined) {
      var keys = isMap ? raw.keySet().toArray() : Object.keys(raw);
      for (var i = 0; i < keys.length; i++) {
        if (String(keys[i]).endsWith('.' + key)) {
          v = isMap ? raw.get(keys[i]) : raw[keys[i]];
          break;
        }
      }
    }
  } catch (e) {
    logger.warn('ARFEA: contesto illeggibile ({}): {}', key, e.message);
  }
  return (v === null || v === undefined) ? '' : String(v);
}

// Chiavi del contesto ricevuto, per il log quando l'azione manca.
function contextKeys(event) {
  var raw = event && event.raw;
  if (!raw) return '(nessun contesto)';
  try {
    return typeof raw.keySet === 'function' ? String(raw.keySet()) : Object.keys(raw).join(', ');
  } catch (e) {
    return '(illeggibile)';
  }
}

function itemState(name) {
  try {
    var st = items.getItem(name).state;
    return (st === null || st === undefined) ? '' : String(st);
  } catch (e) {
    return '';
  }
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
    var action = contextValue(event, 'action');
    var target = contextValue(event, 'target');

    logger.info('ARFEA action={}, target={}', action, target);
    if (!action) {
      logger.warn('ARFEA: azione assente nel contesto, chiavi ricevute: {}', contextKeys(event));
    }

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
        case 'update_decision':
          doUpdateDecision(target);
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

// Avanzamento dell'aggiornamento di versione: ogni 10 secondi, ma solo mentre
// ce n'e' uno in corso (Redmine #197). Prima la regola del pulsante restava ferma
// fino a 40 minuti dentro un ciclo di sleep, e teneva bloccate tutte le altre
// regole di questo file.
rules.JSRule({
  name: 'ARFEA Update Progress',
  id: 'arfea_update_progress_poll',
  triggers: [triggers.GenericCronTrigger('0/10 * * * * ?')],
  execute: function () {
    if (updateActive()) {
      refreshReleaseStatus();
    }
  }
});

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
// software-per-software: esclude i componenti con toggle arfea_upd_<x>_ok spento
// e il controller aggiorna tutto il resto, compreso quello che nel widget non ha
// un interruttore (codice HABApp, mosquitto, otbr, tag dei servizi spenti:
// Redmine #248).
function doApplyUpdate() {
  var shown = 0;
  var excluded = [];
  for (var frag in UPDATE_ITEM_MAP) {
    try {
      var avail = items.getItem('arfea_upd_' + frag).state;
      var ok = items.getItem('arfea_upd_' + frag + '_ok').state;
      if (avail && avail.toString() !== '' && avail.toString() !== 'NULL'
          && avail.toString() !== 'UNDEF') {
        shown++;
        if (!ok || ok.toString() !== 'ON') excluded.push(UPDATE_ITEM_MAP[frag]);
      }
    } catch (e) { /* item assente */ }
  }
  if (updateActive()) {
    logger.warn('ARFEA apply update: un aggiornamento e\' gia\' in corso');
    return;   // la riga di stato del widget lo sta gia' mostrando
  }
  if (shown > 0 && excluded.length === shown) {
    setUpdateStatus('failed', 'Nessun software selezionato: accendi almeno un interruttore', 100, '');
    logger.warn('ARFEA apply update: nessun software selezionato');
    return;
  }
  // Riscontro immediato: fino a qui l'utente non vedeva niente per decine di
  // secondi, e non sapeva se il clic era arrivato.
  cache.private.put('arfea_update_requested_at', Date.now());
  var detail = excluded.length ? 'esclusi: ' + excluded.join(', ') : 'tutti i software';
  setUpdateStatus('starting', 'Richiesta inviata al controller...', 3, detail);
  var response = httpPost('/system/releases/apply' +
    (excluded.length ? '?exclude=' + encodeURIComponent(excluded.join(',')) : ''));
  logger.warn('ARFEA apply update ({}): {}', detail, response);
  if (!response) {
    setUpdateStatus('failed', 'Il controller non ha accettato la richiesta o non risponde: riprova fra poco, ' +
      'o apri la sua pagina da «Funzioni di sistema»', 100, '');
    return;
  }
  try {
    var res = JSON.parse(response);
    if (res.success === false && !/in corso/i.test(res.message || '')) {
      setUpdateStatus('failed', res.message || 'Il controller ha rifiutato la richiesta', 100, '');
      return;
    }
  } catch (e) { /* risposta non JSON: lo stato lo dice il giro successivo */ }
  refreshReleaseStatus();
}

// Fasi in cui l'aggiornamento e' in corso, e fasi in cui e' finito.
var UPDATE_ACTIVE = ['starting', 'backup', 'awaiting_decision', 'migrating_pre', 'pulling',
                     'recreating', 'waiting_healthy', 'migrating_post'];

// Niente spazio per il backup: il controller aspetta una scelta (Redmine #201).
// target = 'continue' (aggiorna senza backup) o 'abort' (fermati).
function doUpdateDecision(choice) {
  if (choice !== 'continue' && choice !== 'abort') {
    logger.warn('ARFEA update_decision: scelta non valida "{}"', choice);
    return;
  }
  var response = httpPostJson('/system/releases/decision', { choice: choice });
  logger.warn('ARFEA update_decision ({}): {}', choice, response);
  refreshReleaseStatus();
}
var UPDATE_DONE = ['completed', 'failed', 'rolled_back'];

function updateActive() {
  return UPDATE_ACTIVE.indexOf(itemState('arfea_update_state')) >= 0;
}

function setUpdateStatus(state, message, percent, detail) {
  try {
    items.getItem('arfea_update_state').postUpdate(state);
    items.getItem('arfea_update_progress').postUpdate(message || '');
    items.getItem('arfea_update_percent').postUpdate(String(percent || 0));
    if (detail !== undefined) {
      items.getItem('arfea_update_detail').postUpdate(detail);
    }
  } catch (e) {
    logger.error('ARFEA setUpdateStatus failed: {}', e.message);
  }
}

// "verso la 2026.09.01 · openhab · iniziato alle 09:12"
function updateDetail(st) {
  var parts = [];
  if (st.target_release) parts.push('verso la ' + st.target_release);
  if (st.step) parts.push(st.step);
  if (st.started_at) parts.push('iniziato alle ' + String(st.started_at).substring(11, 16));
  if (st.completed_at && UPDATE_DONE.indexOf(st.state) >= 0) {
    parts.push('finito alle ' + String(st.completed_at).substring(11, 16));
  }
  return parts.join(' · ');
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

// Porta negli item lo stato dell'aggiornamento tenuto dal controller. Gira ogni
// 10 s durante un aggiornamento e ogni minuto nel giro periodico: quest'ultimo
// serve dopo che l'aggiornamento ha ricreato OpenHAB (gli item ripartono vuoti,
// ma il controller completa l'apply per conto suo e sa a che punto e').
function refreshReleaseStatus() {
  var prev = itemState('arfea_update_state');
  var response = null;
  try {
    response = httpGet('/system/releases/status');
  } catch (e) {
    logger.error('ARFEA refreshReleaseStatus failed: {}', e.message);
  }
  if (!response) {
    if (updateActive()) {
      items.getItem('arfea_update_detail').postUpdate('il controller non risponde, riprovo...');
    }
    return;
  }
  try {
    var st = JSON.parse(response);
    var state = st.state || 'idle';
    if (state === 'idle') {
      // Il controller non sa di nessun aggiornamento. Se il widget ne crede uno in
      // corso: appena richiesto aspetta, altrimenti lo dice invece di restare appeso.
      if (prev === 'starting' && Date.now() - (cache.private.get('arfea_update_requested_at') || 0) < 60000) return;
      if (UPDATE_ACTIVE.indexOf(prev) >= 0) {
        setUpdateStatus('failed', 'Il controller non ha piu\' notizie dell\'aggiornamento (si e\' riavviato?): ' +
          'controlla le versioni e riprova', 100, '');
      }
      return;
    }
    setUpdateStatus(state, st.message || state, typeof st.progress === 'number' ? st.progress : 0, updateDetail(st));
    if (UPDATE_DONE.indexOf(state) >= 0 && UPDATE_ACTIVE.indexOf(prev) >= 0) {
      refreshServices();
      refreshReleaseCheck();
      refreshSystemInfo();
    }
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

    // Diff per-software: mappa nome servizio -> versione target. I servizi spenti
    // prendono solo il tag, senza interruttore (Redmine #247).
    var byService = {};
    var svcList = res.services || [];
    for (var i = 0; i < svcList.length; i++) {
      if (svcList[i].enabled === false) continue;
      byService[svcList[i].name] = svcList[i].target_image;
    }
    for (var frag in UPDATE_ITEM_MAP) {
      var svcName = UPDATE_ITEM_MAP[frag];
      var target = byService[svcName];
      try {
        if (target) {
          // Default ON solo quando compare un aggiornamento nuovo: rimetterlo a ON
          // a ogni giro annullava dopo un minuto la scelta di chi l'aveva spento.
          if (itemState('arfea_upd_' + frag) !== target) {
            items.getItem('arfea_upd_' + frag).postUpdate(target);
            items.getItem('arfea_upd_' + frag + '_ok').postUpdate('ON');
          }
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
