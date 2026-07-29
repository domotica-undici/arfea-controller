'''
    FIBARO FGT-001 HEAD object
    This class manage one FIBARO FGT-001 HEAD for Radiators

    About the items:
        1) if there is a device with the THING named the same as described in thermo.yml and the THING 
            is defined into config/fgt001.yml-> habapp has already created items and bound them to the device's channel
            example thermo.yml: 
              - name: TermostatoMatrimoniale
                model: xx
                ....
                radiators:
                  - name: radiatoreMatrimoniale
                    model: fgt001
        OR
        2) if there is not a physycal device this class should not be called and no fgt001 defined in thermo.yml

'''

import logging
log = logging.getLogger('HABApp')

import HABApp
from HABApp.core.events import ValueChangeEventFilter

from thermostats.thermo_commons import ThermoCommons
from thermostats.utils import States
from system.utils import Utils
from thermostats.temperature_sensors import TemperatureSensor
from thermostats.setpoint import Setpoint

class FGT001(HABApp.Rule):
    def __init__(self, name, thConfig):
        super().__init__()

        self._callbacks = []

        self.states = States()
        self.utils = Utils()
        self.commons = ThermoCommons()

        self.name = name
        
        self.ts = TemperatureSensor(thConfig, self.commons)
        self._temperature = self.ts.temperature

        self._mode = float(self.utils.bindItem(
                                    f'{str(self.name)}_mode', 
                                    self.mode_changed, 
                                    ValueChangeEventFilter(), 1.0))

        self.sp = Setpoint(name)
        self._setpoint = 20.0
        self._setpoint_heating = self.sp.setpoint_heating
        #self.sp.register_callback(self.parameter_changed)
        
    def _notify_observers(self, parameter,old_value, new_value):
        for callback in self._callbacks:
            callback(parameter, old_value, new_value)

    def register_callback(self, callback):
        self._callbacks.append(callback)

    #triggered by subclass' callbacks 
    def parameter_changed(self, parameter, old_value, new_value):
        return

#_mode
    @property
    def mode(self):
        return float(self._mode)

    @mode.setter
    def mode(self, new_value):
        old_value = self.mode
        myState = self.states.internalModes()["OFF"]
        
        if new_value == 0.0:
            myState = self.states.internalModes()["OFF"]
        elif new_value == 1.0:
            myState = self.states.internalModes()["HEAT"]
            
        self._mode = float(myState)
        self._notify_observers("mode", old_value, myState)
 
    #event from device -> translate it and send to internalMode
    def mode_changed(self, event):
        self.mode = float(event.value)

    #event from logic -> translate it and send to mode of device
    def set_mode(self, new_value):
        new_value = float(new_value)
        old_value = self._mode
        self._mode = new_value
        if old_value != new_value:
            retValue = 0
            if new_value == self.states.internalModes()["OFF"]:
                retValue = 0
            elif new_value == self.states.internalModes()["HEAT"]:
                retValue = 1
            else:
                #otherwise turn off
                retValue = 0

            log.debug(f'set mode to {self.name}: {retValue}')
            self.utils.sendCommandToItem(f'{self.name}_mode', retValue)
