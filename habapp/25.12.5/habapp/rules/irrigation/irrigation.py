# HABApp:
#   reloads on:
#    - params/irrigation.yml

import logging
log = logging.getLogger('HABApp')

import HABApp
from irrigation.zone import IrrigationZone
#from HABApp.openhab.events import GroupItemStateChangedEvent
from system.utils import Utils
import datetime

# Rules are classes that inherit from HABApp.Rule
class Irrigation(HABApp.Rule):
    def __init__(self):
        super().__init__()

        self.__version__ = '2.1.0'

#Receive callbacks from subclass events
        self._callbacks = []
                
        self.utils = Utils()

#Groups and items
        itemName="gIrrigation_pumps"
        #aggregatore globale: non e' un equipment fisico e non sta in una Location,
        #quindi resta fuori dal modello semantico. Il tag Equipment sta sulle singole pompe.
        self.openhab.create_item("Group", itemName, label='Pompe irrigazione', category='sani_pump', tags=[], groups=None, group_type='Switch', group_function='OR', group_function_params=['ON','OFF'])
        self.openhab.post_update(itemName, "OFF")
        self.utils.set_stateDescription_metadata(itemName,"%s")
        
        itemName="gIrrigation_valves"
        #aggregatore globale: il tag Equipment sta sulle singole zone (irrigation/zone.py)
        self.openhab.create_item("Group", itemName, label='Valvole irrigazione', category='sani_valve_50', tags=[], groups=None, group_type='Switch', group_function='OR', group_function_params=['ON','OFF'])
        self.openhab.post_update(itemName, "OFF")
        self.utils.set_stateDescription_metadata(itemName,"%s")

        itemName="gIrrigation_valves_management"
        self.openhab.create_item("Group", itemName, label='Gestione valvole irrigazione', category=None, tags=None, group_type='Number', group_function='SUM', group_function_params=None)
        #self.openhab.create_item("Group", itemName, label='Gestione valvole irrigazione', group_type='Number')
        self.openhab.post_update(itemName, "0")
        self.utils.set_stateDescription_metadata(itemName,"%d")

        itemName="gIrrigation_rain_sensors"
        self.openhab.create_item("Group", itemName, label='Sensori pioggia', group_type='Switch', group_function='OR', group_function_params=['ON','OFF'])
        self.openhab.post_update(itemName, "OFF")
        self.utils.set_stateDescription_metadata(itemName,"%s")
        
        """ itemName="gIrrigation_moisture_sensors"
        self.openhab.create_item("Group", itemName, label='Sensori umidità terreno', group_type='Number', group_function='SUM')
        self.openhab.post_update(itemName, "0")
        self.utils.set_stateDescription_metadata(itemName,"%s") """

#Load configuration
        cfg = HABApp.DictParameter('irrigation')    # this will get the file content


#Load plant
        self.pumps = {}
        if 'pumps' in cfg:
            for pump in cfg['pumps']:
                itemName = pump['name']
                label = itemName
                if 'label' in pump:
                    label = pump['label']

                pumpType = 'switch'
                if 'type' in pump:
                    pumpType = str(pump['type']).lower()
                self.pumps[itemName] = pumpType

                #the group holds the zones served by this pump: it aggregates them for
                #the UI, the relay is driven by update_pump()
                #if not self.oh.item_exists(itemName):
                self.openhab.create_item("Group", itemName, label=label, tags=['Pump'], group_type='Switch', group_function='OR', group_function_params=['ON','OFF'], groups=['gIrrigation_pumps'])

                self.openhab.create_item(pumpType.capitalize(), f'{itemName}_switch', label=label, groups=['gIrrigation_pumps'])


        self.valves = []
        if 'valves' in cfg:
            for valve in cfg['valves']:
                v = IrrigationZone(valve)
                v.register_callback(self.zone_changed)
                self.valves.append(v)
                log.debug(f'Irrigation zone added: {v.name}')

#Rain sensors are declared in the configuration but the rain check is disabled
#(IrrigationZone.checkRain always returns True), so no item is created for them:
#creating one would overwrite a sensor item already defined elsewhere.
        self.rainSensors = []
        for key in ('rain_sensors', 'rainSensors'):
            if key in cfg:
                self.rainSensors = [rainSensor['name'] for rainSensor in cfg[key]]
                break

#align every pump with the zones we just forced closed
        for pumpName in self.pumps:
            self.update_pump(pumpName)

        """         self.moistureSensors = []
        if 'moistureSensors' in cfg:
            for moistureSensor in cfg['moistureSensors']:
                itemName = moistureSensor['name']
                label = itemName
                if 'label' in cfg:
                    label = moistureSensor['label']
                #if not self.oh.item_exists(itemName):
                self.openhab.create_item("Number", itemName, label=label, groups=['gIrrigation_moisture_sensors'])
                self.moistureSensors.append(itemName)
 """
#Cycle
        self.run.at(self.run.trigger.interval(start=None, interval=datetime.timedelta(seconds=60)), self.run_every_minute)


#Define callback functions
    def _notify_observers(self, parameter, old_value, new_value):
        for callback in self._callbacks:
            callback(parameter, old_value, new_value)

    def register_callback(self, callback):
        self._callbacks.append(callback)

#triggered by subclass' callbacks 
    def parameter_changed(self, parameter, old_value, new_value):
        if old_value != new_value:
            self._notify_observers(parameter, old_value, new_value)

#triggered by IrrigationZone when its valve actually changed state
    def zone_changed(self, parameter, zoneName, new_value):
        if parameter != 'status':
            return
        for valve in self.valves:
            if valve.name == zoneName and valve.pump is not None:
                self.update_pump(valve.pump)
                return

#The pump follows its zones: ON as soon as one of them is open. Computed here
#instead of relying on the group OR, which only sees switch-type valves: a
#number-type zone would never trigger it and the pump would stay off.
    def update_pump(self, pumpName):
        status = "OFF"
        for valve in self.valves:
            if valve.pump == pumpName and valve.status == "ON":
                status = "ON"
                break

        cmd = self.utils.convertSwitchToNumber(self.pumps.get(pumpName, 'switch'), status)
        if cmd is None:
            log.error(f'Pompa {pumpName}: tipo non gestito, comando {status} non inviato')
            return
        self.openhab.send_command(f'{pumpName}_switch', cmd)


    def run_every_minute(self):
        for valve in self.valves:
            try:
                self.check_valve(valve)
            except Exception:
                #a single broken zone must not stop the others
                log.exception(f'Errore nella gestione della zona irrigazione {valve.name}')

    def check_valve(self, valve):
        log.debug(f'Timer irrigazione {valve.name}, con management {valve.management}')
    #only run if management is auto
        if valve.management != 2:
            return
    #a cycle already in progress must not be restarted: it would reset the countdown
        if valve.status == "ON":
            return
    #if it's time to irrigate
        if valve.checkSchedule() != True:
            return
    #if it's not raining
        if valve.checkRain() != True:
            return
    #if soil it's not too wet
        """ if valve.checkMoisure() != True:
            return """
    #all ckecks were true
    #Start Valve
        valve.switch_on()


Irrigation()