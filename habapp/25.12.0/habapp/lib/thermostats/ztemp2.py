'''
    HEATIT Z-TEMP2 object
    This class manage an HEATIT Z-TEMP2 thermostat

    About the items:
        1) if there is a device with the THING named the same as described in thermo.yml and the THING 
            is defined into config/thing_ztemp2.yml-> habapp has already created items and bound them to the device's channel
            example thermo.yml: 
              - name: TermostatoMatrimoniale
                model: ztemp2
        OR
        2) if there is not a physycal device this class should not be called and no ztemp2 defined in thermo.yml

'''

import logging
log = logging.getLogger('HABApp')

import HABApp
from HABApp.core.events import ValueChangeEventFilter

from thermostats.thermo_commons import ThermoCommons
from thermostats.utils import States
from system.utils import Utils
from thermostats.temperature_sensors import TemperatureSensor
from thermostats.humidity_sensors import HumiditySensor
from thermostats.setpoint import Setpoint

class ZTEMP2(HABApp.Rule):
    def __init__(self, name, thConfig):
        super().__init__()

        self._callbacks = []

        self.states = States()
        self.utils = Utils()
        self.commons = ThermoCommons()

        self.name = name
        
        #create item if not exist
        self.ts = TemperatureSensor(thConfig, self.commons)
        self._temperature = self.ts.temperature

        #create item if not exist
        self.hs = HumiditySensor(thConfig, self.commons)
        self._relhumidity = self.hs.relhumidity

        self._mode = float(self.utils.bindItem(
                                    f'{str(self.name)}_mode', 
                                    self.mode_changed, 
                                    ValueChangeEventFilter(), 1.0))

        """ self.operatingstate = float(self.utils.bindItem(
                                    f'{str(self.name)}_operatingstate', 
                                    self.operatingstate_changed, 
                                    ValueChangeEventFilter(), 0.0)) """

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
        self._mode = new_value
        self._notify_observers("mode", old_value, new_value)
        
 
    #event from device -> translate it and send to OH
    def mode_changed(self, event):
        value = float(event.value)
        if value == 0.0:
            myState = self.states.internalModes()["OFF"]
        elif value == 1.0:
            #ZTemp2 can manage only a season so let's workaround
            #to manage heat and cool from season value
            ws = self.commons.season
            if str(ws).lower() == "w":
                myState = self.states.internalModes()["HEAT"]
            else:
                myState = self.states.internalModes()["COOL"]

        self.mode = float(myState)

    #event from OH -> translate it and send to device
    def set_mode(self, value):
        value = float(value)
        if value != self.mode:
            retValue = 0
            if value == self.states.internalModes()["OFF"]:
                retValue = 0
            elif value == self.states.internalModes()["HEAT"] or value == self.states.internalModes()["COOL"]:
                #ZTemp2 can manage only a season so let's workaround
                #to manage heat and cool from season value
                retValue = 1
            else:
                #otherwise turn off
                retValue = 0

            log.debug(f'set mode to {self.name}: {retValue}')
            self.utils.sendCommandToItem(f'{self.name}_mode', retValue)
