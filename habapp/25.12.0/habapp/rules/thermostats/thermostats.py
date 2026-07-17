'''
    Main class contains whole thermo plant configuration
'''
# HABApp:
#   reloads on:
#    - params/thermo.yml

import logging
log = logging.getLogger('HABApp')

import HABApp
from system.utils import Utils

from thermostats.thermo_thermostat import Thermostat
from thermostats.thermo_commons import ThermoCommons
from thermostats.utils import States
from datetime import timedelta

class ThermoPlant(HABApp.Rule):
    def __init__(self):
        super().__init__()

#Receive callbacks from subclass events
        self._callbacks = []

        self.states = States()
        self.utils = Utils()
        self.commons = ThermoCommons()

        self.run.soon(self.load_configuration)

    '''
    Appends thermostat to my plant
    '''
    def load_configuration(self):
        #Items used to update thermostat schedule using info from Thermostat Widget
        itemName = "thermoSetupTemp"
        if not self.openhab.item_exists(itemName):
            self.openhab.create_item("String", itemName, label='')

#Load configuration from yaml file
        cfg = HABApp.DictParameter('thermo')

#Load generators
        self._hasCoolGenerators = False
        self._hasHeatGenerators = False
        if 'generators' in cfg:
            if 'cool' in cfg['generators']:
                myItem = cfg['generators']['cool'][0]
                itemName = str(myItem['name'])
                if not self.openhab.item_exists(itemName):
                    self.openhab.create_item(myItem['type'].capitalize(), itemName, label='Generatore freddo')

                self._hasCoolGenerators = True
                self.coolGenerator = {'name': myItem['name'], 'type': myItem['type']}

            if 'heat' in cfg['generators']:
                myItem = cfg['generators']['heat'][0]
                itemName = str(myItem['name'])
                if not self.openhab.item_exists(itemName):
                    self.openhab.create_item(myItem['type'].capitalize(), itemName, label='Generatore caldo', tags=['Boiler'])

                self._hasHeatGenerators = True
                self.heatGenerator = {'name': myItem['name'], 'type':myItem['type']}

#Load plant pumps
        self._hasCoolPumps = False
        self._hasHeatPumps = False
        if 'pumps' in cfg:
            if 'cool' in cfg['pumps']:
                self.coolPumps = []
                for pump in cfg['pumps']['cool']:
                    itemName = str(pump['name'])
                    if not self.openhab.item_exists(itemName):
                        self.openhab.create_item(pump['type'].capitalize(), itemName, label=itemName)
                    pump = {'name': itemName, 'type': pump['type'], 'status': 'OFF'}
                    self.coolPumps.append(pump)
                self._hasCoolPumps = True


            if 'heat' in cfg['pumps']:
                self.heatPumps = []
                for pump in cfg['pumps']['heat']:
                    itemName = str(pump['name'])
                    if not self.openhab.item_exists(itemName):
                        self.openhab.create_item(pump['type'].capitalize(), itemName, label=itemName)
                    pump = {'name': itemName, 'type': pump['type'], 'status': 'OFF'}
                    self.heatPumps.append(pump)
                self._hasHeatPumps = True

#Load thermostats
        self.thermostats = []
        if 'thermostats' in cfg:
            for thConfig in cfg['thermostats']:
                th = Thermostat(thConfig, self.commons)
                th.register_callback(self.parameter_changed)
                self.thermostats.append(th)

#Only when configuration is loaded so start cycle
        #self.run.every_minute(self.run_every_minute)
        """ self.run.at(
            start_time='00:00:07', interval=timedelta(minutes=1), 
            callback=self.run_every_minute
        ) """
        self.run.at(self.run.trigger.interval(start=None, interval=timedelta(seconds=60)), self.run_every_minute)
        

#Define callback functions
    def _notify_observers(self, parameter, old_value, new_value):
        for callback in self._callbacks:
            callback(parameter, old_value, new_value)

    def register_callback(self, callback):
        self._callbacks.append(callback)

#triggered by subclass' callbacks 
    def parameter_changed(self, parameter, objectParameter, new_value):
        if objectParameter != new_value:
            log.debug(f'thermostat {parameter} changed from {objectParameter} to {new_value}')
            #triggered by Thermostat class to check the whole thermostat for the status to have
            if parameter == 'internalManagement' or parameter == 'window':
                self.run_every_minute()

    '''
    Thermoregulation cycle
    '''
    def run_every_minute(self):
        for th in self.thermostats:
            th.runThermostat()
            #now my thermostat is in right internalMode and internalState, so actuate
            log.debug("I'm on thermostat: {} with internalManagement {}, internalMode {}, internalState: {}".format(th.name, th.internalManagement, th.internalMode, th.internalState))

        #now that everything is set up, run generators actuation
        self.runGenerators()

    def runGenerators(self):
        runHeat = False
        runCool = False

        if 'coolPumps' in self.__dict__:
            for pump in self.coolPumps:
                pump['status'] = 'OFF'

        if 'heatPumps' in self.__dict__:
            for pump in self.heatPumps:
                pump['status'] = 'OFF'

        #at least one thermostat is calling -> start generator
        for th in self.thermostats:
            log.debug(f'Run generators. TH: {th.name}')
            
            if 'internalState' in th.__dict__ and th.internalState == self.states.internalStates()["ANTIFREEZE"]:
                runHeat = True

                if 'heatPumps' in self.__dict__:
                    for pump in self.heatPumps:
                        pump['status'] = 'ON'

                break

            if '_callHeatGenerator' in th.__dict__:
                runHeat = (runHeat or th._callHeatGenerator) and self.commons.season.lower() == "w"

            if '_callCoolGenerator' in th.__dict__:
                runCool = (runCool or th._callCoolGenerator) and self.commons.season.lower() == "s"
                
            if 'heatPumps' in th.__dict__:
                #if runHeat also run all pumps belonging to this thermostat (if any)
                pumpLst = [pmp for pmp in self.heatPumps if pmp['name'] in th.heatPumps]
                for pump in pumpLst:
                    #the first thermostat who setp pump on make other th skip cycle
                    if pump['status'] == 'OFF':
                        if th._callHeatGenerator == True:
                            value = 'ON'
                        else:
                            value = 'OFF'
                        pump['status'] = value
                    

            if 'coolPumps' in th.__dict__:
                #if runCool also run all pumps belonging to this thermostat (if any)
                pumpLst = [pmp for pmp in self.coolPumps if pmp['name'] in th.coolPumps]
                for pump in pumpLst:
                    #the first thermostat who setp pump on make other th skip cycle
                    if pump['status'] == 'OFF':
                        if th._callCoolGenerator == True:
                            value = 'ON'
                        else:
                            value = 'OFF'
                        pump['status'] = value

            #if th.internalState == th.states.internalStates()["WINDOWSTOP"]:
            # do nothing

        if 'heatPumps' in self.__dict__:
            for pump in self.heatPumps:
                self.run_pumps_apply(pump, pump['status'])

        if 'coolPumps' in self.__dict__:
            for pump in self.coolPumps:
                self.run_pumps_apply(pump, pump['status'])

        if self._hasHeatGenerators:
            self.run_generators_apply(self.heatGenerator, runHeat)

        if self._hasCoolGenerators:
            self.run_generators_apply(self.coolGenerator, runCool)

    def run_pumps_apply(self, who, toRun):
        cmd = self.utils.convertSwitchToNumber(who['type'], toRun)
        currentState = self.openhab.get_item(who['name']).state
        if currentState != cmd:
            self.utils.sendCommandToItem(who['name'], cmd)

    def run_generators_apply(self, who, toRun):
        #toRun = None means do nothing.
        if toRun != None:
            if toRun:
                cmd = self.utils.convertSwitchToNumber(who['type'], "ON")
            else:
                cmd = self.utils.convertSwitchToNumber(who['type'], "OFF")
                
            currentState = self.openhab.get_item(who['name']).state
            if currentState != cmd:
                self.utils.sendCommandToItem(who['name'], cmd)

ThermoPlant()
