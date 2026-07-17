import logging
log = logging.getLogger('HABApp')

import HABApp
from HABApp.core.events import ValueChangeEventFilter
import json
from threading import Timer

from loads.load import Load
from system.utils import Utils

"""
This class manage the loads logic: if the system knows what's the userd power in real time and knows what's the maximum allowed consumption
it's possible to unplug loads in case of over consumption. when a load is turned off, it's power will be estimated
and the procedure will check it before try to turn back on.
"""
class Loads(HABApp.Rule):
    def __init__(self):
        super().__init__()

#Receive callbacks from subclass events
        self._callbacks = []

        self.utils = Utils()

        self.run.soon(self.load_configuration)

    def load_configuration(self):
#Load configuration
        cfg = HABApp.DictParameter('loads')    # this will get the file content
        self.loads = []
        if "loads" in cfg:
            for load in cfg['loads']:
                ld = Load(load)
                #ld.register_callback(self.parameter_changed)
                self.loads.append(ld)
                log.debug(f'Load added: {self.loads[len(self.loads)-1].name}')

#Create items for Loads management
        itemName="gLoads"
        self.openhab.create_item("Group", itemName, label='Carichi', category='poweroutlet', tags=['PowerOutlet'], group_type='Switch', group_function='OR', group_function_params=['ON','OFF'])
        self.utils.set_stateDescription_metadata(itemName,"%s")

        #Real time consumption
        itemName="plantPowerConsumption"
        self.openhab.create_item("Number", itemName, label='Potenza impiegata', category='', tags=[], groups=[])
        #self.openhab.set_metadata(itemName, "stateDescription", "displayState", {'pattern': 'KW'} )
        self.utils.set_stateDescription_metadata(itemName,"%.1f KW")
        
        #Max available power for entire plant
        itemName="maxPower"
        self.openhab.create_item("Number", itemName, label='Pot. disponibile', category='', tags=[], groups=['gPersistence'])
        #self.openhab.set_metadata(itemName, "stateDescription", "displayState", {'pattern': 'KW'} )
        self.utils.set_stateDescription_metadata(itemName,"%.1f KW")

        self.openhab.post_update(itemName, 3.0)

        self._maxPower = float(self.utils.bindItem(
                            itemName, 
                            self.maxPower_changed, 
                            ValueChangeEventFilter(), 3.0))
        
        #Priority list
        itemName="loads_setup"
        self.openhab.create_item("String", itemName, label='Configurazione carichi', category='', tags=[], groups=['gPersistence'])
        self.utils.set_stateDescription_metadata(itemName,"%s")
        self.loads_setup = str(self.utils.bindItem(itemName, 
                            self.loads_setup_changed, 
                            ValueChangeEventFilter(), "[]"))
        #List of already configured load priority
        configuredLoads = json.loads(self.loads_setup)

        #listeners
        self.listen_event("plantPowerConsumption", self.loadsProcedure, ValueChangeEventFilter())

        
        self.buildArray(rebuild=False, configuredLoads=configuredLoads)
        #now i have all my loads into self.configuredLoads, so set priority to each load
        for load in self.loads:
            for configuredLoad in configuredLoads:
                if str(load.name) == configuredLoad['name']:
                    load._priority = configuredLoad['priority']

        

        self.unpluggedLoads = []
        self.estimateConsumption = False


#Define callback functions
    def _notify_observers(self, parameter, old_value, new_value):
        for callback in self._callbacks:
            callback(parameter, old_value, new_value)

    def register_callback(self, callback):
        self._callbacks.append(callback)

#triggered by subclass' callbacks 
    def parameter_changed(self, parameter, old_value, new_value):
        return
        if old_value != new_value:
            self._notify_observers(parameter, old_value, new_value)


    """
        Using Widgets it's impossible to properly format the string with the right syntax so let's do here.
        widget upodates loads_setup item with a mere list of loads: i catch that string and rebuild the correct JSON. 
        if the user does not pick all loads, the procedure adds the remaining loads at the end of the list
    """
    def loads_setup_changed(self, event):
        try:
            json.loads(event.value)
        except:
            self.buildArray(rebuild=True, configuredLoads=[], event=event)
            log.debug("Priorità carichi modificata correttamente")

    def buildArray(self, rebuild=False, configuredLoads=[], event=None):
        #loads present in my plant
        loads = self.openhab.get_item("gLoads").members

        i = 1
        if rebuild == True:
            #changed priority order via user interface
            values = event.value.split(',')
            for value in values:
                if value != 'undefined':
                    found = False
                    for configuredLoad in configuredLoads:
                        if str(value) == configuredLoad['name']:
                            found = True

                    if found == False:
                        configuredLoads.append({"name": value, "priority": i})
                        i += 1

        #if building for the first time: check if my load is present in priority configuration, if not, add as last
        #if rebuilding add missing loads
        for load in loads:
            found = False
            for configuredLoad in configuredLoads:
                if str(load.name) == configuredLoad['name']:
                    found = True

            if found == False:
                configuredLoads.append({"name": load.name, "priority": i})
                i += 1

        self.openhab.post_update("loads_setup", json.dumps(configuredLoads))
        self.configuredLoads = configuredLoads
        log.debug(f'configuredLoads: {configuredLoads}')

#_maxPower
    @property
    def maxPower(self):
        return float(self._maxPower)

    @maxPower.setter
    def maxPower(self, new_value):
        new_value = float(new_value)
        old_value = self.maxPower
        self._maxPower = new_value
        self._notify_observers("maxPower", old_value, new_value)
    
    def maxPower_changed(self, event):
        self.maxPower = float(event.value)

#triggered each time that changes plantPowerConsumption
    def loadsProcedure(self, event):
        total_consumption = float(event.value)
        log.debug("Plant consumption is {} KW, of {} KW available".format(total_consumption, self._maxPower))

        #estimate the consumption of the last unplugged load. this is done watching the current consumption and comparing it before unplug the last load
        if self.estimateConsumption == True and self.unpluggedLoads != []:
            list(self.unpluggedLoads)[-1]._theoricalConsumption = abs(list(self.unpluggedLoads)[-1]._consumptionWhenUnplugged - total_consumption)
            self.estimateConsumption = False
            log.debug("unpluggedLoads {} with theoricalConsumption {}".format(list(self.unpluggedLoads)[-1].name, list(self.unpluggedLoads)[-1]._theoricalConsumption))

        if total_consumption > self.maxPower:
            #act on configured loads because this list is correctly ordered. if a load isn't in this list, it will not be evaluated
            found = False
            for cld in self.configuredLoads:
                if found == False:
                    loads = [loads for loads in self.loads if loads.name == cld['name'] and loads._status == "ON"]
                    for ld in loads:
                        Timer(10, lambda: self.shutOff(ld)).start()
                        ld._consumptionWhenUnplugged = total_consumption
                        self.unpluggedLoads.append(ld)
                        self.estimateConsumption = True
                        log.debug("Unplugging load {} with power consumption {}".format(ld.name, total_consumption))
                        found = True
                        break
                
        else:
            if self.unpluggedLoads != []:
                ld = list(self.unpluggedLoads)[-1] #last unplugged load
                log.debug("Powering ON {} with current status {}".format(ld.name, ld._status))
                if total_consumption == 0 or total_consumption + ld._theoricalConsumption < self._maxPower:
                    ld._status = "ON"
                    ld._consumptionWhenUnplugged = 0.0
                    ld._theoricalConsumption = 0.0
                    log.debug("Reconnecting load {} with power consumption {}".format(ld.name, total_consumption))
                    self.openhab.send_command(ld.name, "ON")
                    self.unpluggedLoads.pop()

    def shutOff(self, ld):
        log.debug("Going to power OFF load {}".format(ld.name))
        ld._status = "OFF"
        self.openhab.send_command(ld.name, "OFF")

Loads()