import logging
log = logging.getLogger('HABApp')

import HABApp
from HABApp.core.events import ValueChangeEventFilter
import json
import datetime #, time
from system.utils import Utils

#Tetto di sicurezza sulla durata di un ciclo: _schedule e' un item String, quindi
#scrivibile anche via REST senza passare dallo slider della UI (che si ferma a 90).
MAX_DURATION_MINUTES = 180

"""
This class manage an irrigation zone and its scheduled times to start.
"""
class IrrigationZone(HABApp.Rule):
    def __init__(self, cfg):
        super().__init__()

        self.utils = Utils()
        self.tools = self.get_rule('Tools')

#Notify the parent rule when the zone changes state, so it can drive the pump
        self._callbacks = []

        self.name = cfg['name']
        self._type = str(cfg['type']).lower()
        self.label = self.name
        if 'label' in cfg:
            self.label = cfg['label']

#pump serving this zone: driven by the parent rule, see Irrigation.update_pump
        self._pump = None
        if 'pump' in cfg:
            self._pump = cfg['pump']

#Tools.timers is shared with the thermostats, so qualify the name
        self._timerName = f'{self.name}_irrigation'

#Create items for irrigation zone
        itemName = self.name
        #if not self.oh.item_exists(itemName):

        #if valve has a pump let's put that into pump's group to start the pump when at least one valve is started
        if self._pump is not None:
            myGroups = ['gIrrigation_valves', self._pump]
        else:
            myGroups = ['gIrrigation_valves']

        self.openhab.create_item(self._type.capitalize(), itemName, label=self.label, tags=['Equipment', 'Valve'], groups=myGroups)
        self.openhab.create_item("Number", f'{itemName}_management', label=f'Attivazione {itemName}', groups=['gPersistence', 'gIrrigation_valves_management'])
        self.openhab.create_item("String", f'{itemName}_schedule', label=f'Pianificazione {itemName}', groups=['gPersistence'])
        """
        IrrigationZoneschedule syntax:

        [
            {"duration": 15, "rainChance": 0, "soilHumidity": 10, "chrono": ["10:00,18:00", "10:00,18:00", "10:00,18:00", "10:00,18:00", "10:00,18:00", "10:00,18:00", "10:00,18:00"]}
        ]
        """

#zone status on/off. The item state is the source of truth: follow it so that
#_status stays true even when the valve is commanded from outside (UI, other rules)
        self._status = "OFF"
        self.listen_event(itemName, self.valve_state_changed, ValueChangeEventFilter())

#_management is relative to a zone: off, manual (forced on), auto (scheduled)
        management = self.toManagement(self.utils.bindItem(f'{itemName}_management',
                            self.irrigation_valves_management_changed,
                            ValueChangeEventFilter(), 0))
        self._management = management if management is not None else 0

#schedule setup
        self._schedule = str(self.utils.bindItem(f'{itemName}_schedule',
                            self.irrigation_schedule_changed,
                            ValueChangeEventFilter(), '[{"duration": 15, "rainChance": 0, "soilHumidity": 10, "chrono": ["10:00,18:00", "10:00,18:00", "10:00,18:00", "10:00,18:00", "10:00,18:00", "10:00,18:00", "10:00,18:00"]}]'))

#if rain sensor is present in configuration, and is bound to a zone, add it
        if 'rain_sensor' in cfg:
            self._rain_sensor = cfg['rain_sensor']
        else:
            self._rain_sensor = None

        """ #if moisture sensor is present in configuration, and is bound to a zone, add it
        if 'moisture_sensor' in cfg:
            self._moisture_sensor = cfg['moisture_sensor']
        else:
            self._moisture_sensor = None """

#A running cycle cannot survive a restart: the countdown only lives in memory, so
#a valve left open would never be closed. Apply the state implied by _management
#instead of inheriting the stale item state.
        self.apply_management()


    def valve_state_changed(self, event):
        newStatus = self.utils.convertNumberToSwitch(self._type, event.value)
        if newStatus is None or newStatus == self._status:
            return
        old_value = self._status
        self._status = newStatus
        log.debug(f'Zona irrigazione {self.name} con cambio stato da {old_value} a {newStatus}')
        self._notify_observers('status', self.name, newStatus)

    def irrigation_valves_management_changed(self, event):
        management = self.toManagement(event.value)
        if management is None:
            log.warning(f'Zona irrigazione {self.name}: management non valido ({event.value}), ignorato')
            return
        self._management = management
        self.apply_management()

#Any management change invalidates the cycle in progress, timer included
    def apply_management(self):
        self.tools.cancelTimer(self._timerName)
        if self._management == 1:
            #Zone manual ON: the user asked for it, so no countdown closes it
            self.sendValveCommand("ON")
        else:
            #0 = Zone OFF, 2 = Zone managed by chron (waits for the schedule)
            self.sendValveCommand("OFF")

    def irrigation_schedule_changed(self, event):
        self._schedule = str(event.value)

    def sendValveCommand(self, status):
        cmd = self.utils.convertSwitchToNumber(self._type, status)
        if cmd is None:
            log.error(f'Zona irrigazione {self.name}: tipo "{self._type}" non gestito, comando {status} non inviato')
            return
        self.openhab.send_command(self.name, cmd)

#function executer by timer to turn on/off the zone
    def switch_on(self):
        #never open a valve without a countdown that closes it back
        duration = self.scheduledDuration()
        if duration is None:
            log.warning(f'Zona irrigazione {self.name}: durata non valida, non accendo')
            return
        log.debug(f'Accendo zona irrigazione {self.name} per {duration} minuti')
        self.sendValveCommand("ON")
        #countdown to turn off irrigation after user defined time in minutes
        self.tools.createTimer(self._timerName, 60 * duration, self.switch_off)
        self.tools.startCountdown(self._timerName)

    def switch_off(self):
        log.debug(f'Spengo zona irrigazione {self.name}')
        self.sendValveCommand("OFF")

    def toManagement(self, value):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None

#_schedule is a String item, writable from the UI and from the REST API: never
#assume it is well formed
    def loadSchedule(self):
        try:
            cfg = json.loads(self._schedule)[0]
        except (ValueError, TypeError, IndexError) as e:
            log.error(f'Zona irrigazione {self.name}: schedule illeggibile ({e}): {self._schedule}')
            return None
        if not isinstance(cfg, dict):
            log.error(f'Zona irrigazione {self.name}: schedule non valido: {self._schedule}')
            return None
        return cfg

    def scheduledDuration(self):
        cfg = self.loadSchedule()
        if cfg is None:
            return None
        try:
            duration = int(float(cfg['duration']))
        except (KeyError, TypeError, ValueError):
            log.error(f'Zona irrigazione {self.name}: durata assente o non numerica')
            return None
        if duration <= 0:
            log.debug(f'Zona irrigazione {self.name}: durata {duration}, ciclo disabilitato')
            return None
        if duration > MAX_DURATION_MINUTES:
            log.warning(f'Zona irrigazione {self.name}: durata {duration} oltre il limite, ridotta a {MAX_DURATION_MINUTES} minuti')
            duration = MAX_DURATION_MINUTES
        return duration

    def parseSlot(self, value):
        parts = str(value).strip().split(':')
        if len(parts) != 2:
            return None
        try:
            h = int(parts[0])
            m = int(parts[1])
        except ValueError:
            return None
        if not (0 <= h <= 23 and 0 <= m <= 59):
            return None
        return (h, m)

    def checkSchedule(self):
#load today schedule
        cfg = self.loadSchedule()
        if cfg is None:
            return False

        chrono = cfg.get('chrono')
        if not isinstance(chrono, list) or len(chrono) == 0:
            return False

        # weekday 0 = Monday
        weekday = datetime.datetime.today().weekday()
        if weekday >= len(chrono):
            log.warning(f'Zona irrigazione {self.name}: chrono con {len(chrono)} giorni invece di 7, oggi non pianificato')
            return False

        todayschedule = str(chrono[weekday]).strip()
#check if it's time to irrigate
        if todayschedule == '' or todayschedule == '0':
            return False

        now = datetime.datetime.now().time()
        for todConfig in todayschedule.split(","):
            slot = self.parseSlot(todConfig)
            if slot is None:
                if todConfig.strip() != '':
                    log.debug(f'Zona irrigazione {self.name}: slot "{todConfig}" non valido, ignorato')
                continue

            if now.hour == slot[0] and now.minute == slot[1]:
                return True

        return False

#Rain and moisture checks are disabled on purpose: the sensors are declared in the
#configuration but they do not gate irrigation. rainChance/soilHumidity in the
#schedule JSON are written by the UI widget and read by nobody.
    def checkRain(self):
        return True

    def checkMoisure(self):
        return True

#Define callback functions
    def _notify_observers(self, parameter, old_value, new_value):
        for callback in self._callbacks:
            callback(parameter, old_value, new_value)

    def register_callback(self, callback):
        self._callbacks.append(callback)

    """
#_schedule
    @property
    def schedule(self):
        return self._schedule

    @schedule.setter
    def schedule(self, new_value):
        old_value = self._schedule
        self._schedule = new_value
        if old_value != new_value:
            log.debug(f'Schedule zona irrigazione {self.name} cambiato')
    """

#_status is only written by valve_state_changed: use switch_on/switch_off to act
    @property
    def status(self):
        return self._status

    @property
    def management(self):
        return self._management

    @property
    def pump(self):
        return self._pump
