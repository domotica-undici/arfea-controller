// ─────────────────────────────────────────────────────────────
// ARFEA System – portato da HABApp
//   - rules/aasystem/arfea.py (gPersistence, users_list, messaging, gBatteryPowered)
//   - rules/aasystem/time.py  (timeSlots, timeSlot, isHoliday, holidayName, adesso)
//
// Questo file è l'UNICA implementazione delle fasce giornaliere: vive nello
// skeleton, quindi è presente in OGNI installazione anche senza HABApp.
// rules/aasystem/time.py faceva le stesse cose in parallelo (e ricreava via
// REST item già definiti da file): è stato rimosso da habapp/25.12.0.
//
// Gli item sono definiti in conf/items/arfea.items
// ─────────────────────────────────────────────────────────────

var logger = Java.type('org.slf4j.LoggerFactory').getLogger('org.openhab.rule.arfea.system');

var DEFAULT_TIME_SLOTS = '[' +
  '{"name": "sveglia", "start": "06:30"},' +
  '{"name": "mattino", "start": "08:30"},' +
  '{"name": "pomeriggio", "start": "14:00"},' +
  '{"name": "sera", "start": "19:00"},' +
  '{"name": "notte", "start": "22:00"}' +
']';

// ─────────────────────────────────────────────────────────────
// Time slots
// ─────────────────────────────────────────────────────────────

function initTimeSlotsDefault() {
  try {
    var ts = items.getItem('timeSlots');
    var state = ts.state ? ts.state.toString() : 'NULL';
    if (state === 'NULL' || state === 'UNDEF' || state === '' || state === '[]') {
      ts.postUpdate(DEFAULT_TIME_SLOTS);
      logger.info('ARFEA: timeSlots inizializzato con valore di default');
    }
  } catch (e) {
    logger.warn('ARFEA initTimeSlotsDefault: {}', e.message);
  }
}

function manageTimeSlot() {
  try {
    var state = items.getItem('timeSlots').state;
    if (!state) return;
    var raw = state.toString();
    if (raw === 'NULL' || raw === 'UNDEF' || raw === '' || raw === '[]') return;

    var cfg = JSON.parse(raw);
    if (!cfg.length) return;

    var now = new Date();
    var nowMin = now.getHours() * 60 + now.getMinutes();

    function toMin(str) {
      var p = str.split(':');
      return parseInt(p[0], 10) * 60 + parseInt(p[1], 10);
    }

    // Default: ultima fascia (copre le ore dopo l'ultimo start fino al primo del giorno dopo)
    var newMode = cfg[cfg.length - 1].name;
    for (var i = 0; i < cfg.length - 1; i++) {
      var startMin = toMin(cfg[i].start);
      var endMin = toMin(cfg[i + 1].start);
      if (nowMin >= startMin && nowMin < endMin) {
        newMode = cfg[i].name;
        break;
      }
    }

    var currentState = items.getItem('timeSlot').state;
    var current = currentState ? currentState.toString() : '';
    if (current !== newMode) {
      items.getItem('timeSlot').postUpdate(newMode);
      logger.debug('ARFEA: timeSlot cambiato da [{}] a [{}]', current, newMode);
    }
  } catch (e) {
    logger.error('ARFEA manageTimeSlot: {}', e.message);
  }
}

// L'item 'adesso' esiste da sempre in arfea.items ma non lo aggiornava NESSUNO:
// time.py lo creava e gli metteva i metadata senza mai scriverci un valore, e il
// JS non lo gestiva. Restava a NULL per tutta la vita dell'impianto.
function updateNow() {
  try {
    items.getItem('adesso').postUpdate(time.ZonedDateTime.now());
  } catch (e) {
    logger.warn('ARFEA updateNow: {}', e.message);
  }
}

// ─────────────────────────────────────────────────────────────
// Italian holidays (Gauss Easter algorithm + date fisse)
// ─────────────────────────────────────────────────────────────

function calcEaster(year) {
  var a = year % 19;
  var b = Math.floor(year / 100);
  var c = year % 100;
  var d = Math.floor(b / 4);
  var e = b % 4;
  var f = Math.floor((b + 8) / 25);
  var g = Math.floor((b - f + 1) / 3);
  var h = (19 * a + b - d - g + 15) % 30;
  var i = Math.floor(c / 4);
  var k = c % 4;
  var L = (32 + 2 * e + 2 * i - h - k) % 7;
  var mm = Math.floor((a + 11 * h + 22 * L) / 451);
  var month = Math.floor((h + L - 7 * mm + 114) / 31);
  var day = ((h + L - 7 * mm + 114) % 31) + 1;
  return { month: month, day: day };
}

var FIXED_HOLIDAYS = {
  '1-1':   'capodanno',
  '1-6':   'befana',
  '4-25':  'liberazione',
  '5-1':   'lavoro',
  '6-2':   'repubblica',
  '8-15':  'ferragosto',
  '11-1':  'santi',
  '12-8':  'immacolata',
  '12-25': 'natale',
  '12-26': 'stefano'
};

function updateItalianHoliday() {
  try {
    var date = new Date();
    var year = date.getFullYear();
    var month = date.getMonth() + 1;
    var day = date.getDate();
    var easter = calcEaster(year);

    var isHoliday = 'feriale';
    var holidayName = '-';

    // Domenica
    if (date.getDay() === 0) {
      holidayName = 'domenica';
      isHoliday = 'festivo';
    }

    // Pasqua
    if (month === easter.month && day === easter.day) {
      holidayName = 'pasqua';
      isHoliday = 'festivo';
    }
    // Pasquetta (giorno dopo Pasqua)
    var pasquetta = new Date(year, easter.month - 1, easter.day + 1);
    if (month === (pasquetta.getMonth() + 1) && day === pasquetta.getDate()) {
      holidayName = 'pasquetta';
      isHoliday = 'festivo';
    }

    // Festività fisse
    var key = month + '-' + day;
    if (FIXED_HOLIDAYS[key]) {
      holidayName = FIXED_HOLIDAYS[key];
      isHoliday = 'festivo';
    }

    items.getItem('isHoliday').postUpdate(isHoliday);
    items.getItem('holidayName').postUpdate(holidayName);
    logger.debug('ARFEA: isHoliday={}, holidayName={}', isHoliday, holidayName);
  } catch (e) {
    logger.error('ARFEA updateItalianHoliday: {}', e.message);
  }
}

// ─────────────────────────────────────────────────────────────
// Battery check
// ─────────────────────────────────────────────────────────────

function checkBatteries() {
  try {
    var state = items.getItem('gBatteryPowered').state;
    if (!state) return;
    var raw = state.toString();
    if (raw === 'NULL' || raw === 'UNDEF' || raw === '') return;
    var level = parseInt(raw, 10);
    if (!isNaN(level) && level < 10) {
      items.getItem('send_message').postUpdate('Controllare i livelli delle batterie');
      logger.info('ARFEA: livello batterie basso: {}%', level);
    }
  } catch (e) {
    logger.error('ARFEA checkBatteries: {}', e.message);
  }
}

// ─────────────────────────────────────────────────────────────
// Rules
// ─────────────────────────────────────────────────────────────

// Un solo cron al minuto per fascia + orologio: 'adesso' si visualizza al minuto
// (pattern %1$tH:%1$tM), non serve una seconda regola.
rules.JSRule({
  name: 'ARFEA Time Slot Manager',
  id: 'arfea_time_slot_manager',
  triggers: [triggers.GenericCronTrigger('0 * * * * ?')],  // ogni minuto
  execute: function () {
    manageTimeSlot();
    updateNow();
  }
});

rules.JSRule({
  name: 'ARFEA Italian Holidays',
  id: 'arfea_italian_holidays',
  triggers: [triggers.GenericCronTrigger('0 0 0 * * ?')],  // mezzanotte ogni giorno
  execute: updateItalianHoliday
});

rules.JSRule({
  name: 'ARFEA Battery Check',
  id: 'arfea_battery_check',
  triggers: [triggers.GenericCronTrigger('0 0 10 * * ?')],  // 10:00 ogni giorno
  execute: checkBatteries
});

// ─────────────────────────────────────────────────────────────
// Init al caricamento
// ─────────────────────────────────────────────────────────────

initTimeSlotsDefault();
manageTimeSlot();
updateNow();
updateItalianHoliday();
