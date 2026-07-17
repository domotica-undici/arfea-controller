'''
    Setpoint management class
    This class manages setpoint in communication between OH and the real device

    About the items:
        there always be a device with the THING named the same as described in thermo.yml and the THING 
        is defined into config/[device].yml-> habapp has already created items and bound them to the device's channel

'''

import logging


log = logging.getLogger('HABApp')

import HABApp
import json

from thermostats.thermo_commons import ThermoCommons
from HABApp.core.events import ValueChangeEventFilter
from thermostats.utils import States
from system.utils import Utils

class Setpoint(HABApp.Rule):
    def __init__(self, name=None, setpointsNr = 1):
        """
            setpointsNr = how many setpoionts to manage
            1 = only heat
            2 = heat + cool
            4 = heat + cool + dry + fan
        """
        super().__init__()
        
        
        self._callbacks = []

        self.states = States()  
        self.utils = Utils()
        self.commons = ThermoCommons()
        self.tools = self.get_rule('Tools') #load with this syntax otherwise timers won't works
        self.name = name  

        itemName = f'{str(self.name)}_setpoint'
        self._setpoint = float(self.utils.bindItem(
                                        itemName, 
                                        self.setpoint_changed, 
                                        ValueChangeEventFilter(),
                                        20.0))


        itemName = f'{str(self.name)}_setpoint_heating'
        self._setpoint_heating = 0.0
        if self.oh.item_exists(itemName):
            self._setpoint_heating = float(self.utils.bindItem(
                                        itemName, 
                                        self.setpoint_heating_changed, 
                                        ValueChangeEventFilter(),
                                        20.0))

        if setpointsNr > 1:
            itemName = f'{str(self.name)}_setpoint_cooling'
            self._setpoint_cooling = 0.0
            if self.oh.item_exists(itemName):
                self._setpoint_cooling = float(self.utils.bindItem(
                                            itemName, 
                                            self.setpoint_cooling_changed, 
                                            ValueChangeEventFilter(),
                                            20.0))


        if setpointsNr > 2:
            self._setpoint_dry = 0.0
            itemName = f'{str(self.name)}_setpoint_dry'
            if self.oh.item_exists(itemName):
                self._setpoint_dry = float(self.utils.bindItem(
                                            itemName, 
                                            self.setpoint_dry_changed, 
                                            ValueChangeEventFilter(),
                                            20.0))
        
            self._setpoint_fan = 0.0
            itemName = f'{str(self.name)}_setpoint_fan'
            if self.oh.item_exists(itemName):
                self._setpoint_fan = float(self.utils.bindItem(
                                            itemName, 
                                            self.setpoint_fan_changed, 
                                            ValueChangeEventFilter(),
                                            20.0))

        try:
            self.tools.createTimer(f'{str(self.name)}_setpoint', 5, self.update_device_setpoint)

            myItem = self.openhab.get_item(f'{str(self.name)}_tRanges')
            if myItem.state == 'NULL':
                self.tRanges = []
            else:
                self.tRanges = json.loads(myItem.state)
        except:
            self.tRanges = []

    def _notify_observers(self, parameter,old_value, new_value):
        for callback in self._callbacks:
            callback(parameter, old_value, new_value)

    def register_callback(self, callback):
        self._callbacks.append(callback)

#setpoint -----------------------------------------------------------------------------------------------------------------------------------------------------------
    """
    This is used to understand from where setpoint is changed: from OH or from device
    is important because this will change the internalMode following this behaviour:
        -> changed from device will put internalMode in MAN
        -> changed from OH will not do anything
    """
    def updateOHsetpoint(self, new_value, whoUpdate):
        if whoUpdate == "device":
            self.utils.sendCommandToItem(f'{str(self.name)}_internalManagement', "1.0")

        self.utils.sendCommandToItem(f'{str(self.name)}_setpoint', new_value)

    @property
    def setpoint(self):
        return float(self._setpoint)

    @setpoint.setter
    def setpoint(self, new_value):
        old_value = self.setpoint
        new_value = float(new_value)
        if old_value != new_value:
            self._setpoint = new_value
            self.tools.startCountdown(f'{str(self.name)}_setpoint')

    #event both from OH -> send to device and device -> send to OH
    def setpoint_changed(self, event):
        new_value = 0.0
        if type(event) is float:
            new_value = event
        else:
            new_value = float(event.value)

        #chech tRanges
        try:
            if new_value < float(self.tRanges['min']):
                new_value = float(self.tRanges['min'])
            
            if new_value > float(self.tRanges['max']):
                new_value = float(self.tRanges['max'])

        except:
            log.debug(f'set_ricevuto mylog_setpoint')

        log.debug(f'set_ricevuto mylog_setpoint')
        self.setpoint = new_value
       
    def update_device_setpoint(self):
        ws = self.commons.season.lower()
      
        if str(ws).lower() == "s" and '_setpoint_cooling' in dir(self):
            self.set_setpoint_cooling()

        if str(ws).lower() == "w" and '_setpoint_heating' in dir(self):
            self.set_setpoint_heating()

    def set_setpoint_cooling(self):
        log.debug(f'set setpoint_cooling to {self.name}: {self.setpoint}')
        itemName = f'{str(self.name)}_setpoint_cooling'
        if self.oh.item_exists(itemName):
            self.utils.sendCommandToItem(itemName, self.setpoint)

    def set_setpoint_heating(self):
        log.debug(f'set setpoint_heating to {self.name}: {self.setpoint}')
        itemName = f'{str(self.name)}_setpoint_heating'
        if self.oh.item_exists(itemName):
            self.utils.sendCommandToItem(itemName, self.setpoint)

    """ def set_setpoint_dry(self):
        log.debug(f'set setpoint_dry to {self.name}: {self.setpoint}')
        itemName = f'{str(self.name)}_setpoint_dry'
        if self.oh.item_exists(itemName):
            self.utils.sendCommandToItem(itemName, self.setpoint)

    def set_setpoint_fan(self):
        log.debug(f'set setpoint_fan to {self.name}: {self.setpoint}')
        itemName = f'{str(self.name)}_setpoint_fan'
        if self.oh.item_exists(itemName):
            self.utils.sendCommandToItem(itemName, self.setpoint) """
        

#_setpoint_cooling
    @property
    def setpoint_cooling(self):
        return float(self._setpoint_cooling)

    @setpoint_cooling.setter
    def setpoint_cooling(self, new_value):
        old_value = self.setpoint_cooling
        self._setpoint_cooling = float(new_value)
        log.debug(f'setpoint_cooling updated: oldSet {old_value}, newSet {new_value}')
        if self.setpoint != self.setpoint_cooling:
            log.debug(f'setpoint_cooling changed: oldSet {old_value}, newSet {new_value}')
            self.updateOHsetpoint(new_value, "device")
         
    #event from device
    def setpoint_cooling_changed(self, event):
        self.setpoint_cooling = float(event.value)

#_setpoint_heating
    @property
    def setpoint_heating(self):
        return float(self._setpoint_heating)

    @setpoint_heating.setter
    def setpoint_heating(self, new_value):
        old_value = self.setpoint_heating
        self._setpoint_heating = new_value
        log.debug(f'setpoint_heating updated: oldSet {old_value}, newSet {new_value}')
        if self.setpoint != self.setpoint_heating:
            log.debug(f'setpoint_heating changed: oldSet {old_value}, newSet {new_value}')
            self.updateOHsetpoint(new_value, "device")
            
    #event from device
    def setpoint_heating_changed(self, event):
        self.setpoint_heating = float(event.value)

#_setpoint_dry
    @property
    def setpoint_dry(self):
        return float(self._setpoint_dry)

    @setpoint_dry.setter
    def setpoint_dry(self, new_value):
        old_value = self.setpoint_dry
        self._setpoint_dry = new_value
        log.debug(f'setpoint_dry updated: oldSet {old_value}, newSet {new_value}')
        if self.setpoint != self.setpoint_dry:
            log.debug(f'setpoint_dry changed: oldSet {old_value}, newSet {new_value}')
            self.updateOHsetpoint(new_value, "device")
         
    #event from device
    def setpoint_dry_changed(self, event):
        self.setpoint_dry = float(event.value)

#_setpoint_fan
    @property
    def setpoint_fan(self):
        return float(self._setpoint_fan)

    @setpoint_fan.setter
    def setpoint_fan(self, new_value):
        old_value = self.setpoint_fan
        self._setpoint_fan = new_value
        log.debug(f'setpoint_fan updated: oldSet {old_value}, newSet {new_value}')
        if self.setpoint != self.setpoint_fan:
            log.debug(f'setpoint_fan changed: oldSet {old_value}, newSet {new_value}')
            self.updateOHsetpoint(new_value, "device")
         
    #event from device
    def setpoint_fan_changed(self, event):
        self.setpoint_fan = float(event.value)
