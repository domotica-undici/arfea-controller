'''
    Virtual Thermostat object
    This class create a thermostat collecting sensors and actuators spread into environment.
    It is used if i have not a real thermostat device.
    Temperature has to be readed from any temperature sensor, as well as humidity

    Actuation is managed by Thermostat class and not here

    About the items:
        1) if there is a device with the THING named the same as described in thermo.yml and the THING 
            is defined into config/mh7.yml-> habapp has already created items and bound them to the device's channel
            example thermo.yml: 
              - name: TermostatoMatrimoniale
                model: mh7
        OR
        2) if there is not a physycal device this class should not be called and no MH7 defined in thermo.yml

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

class Virtual(HABApp.Rule):
    def __init__(self, name, thConfig, commons, states, utils):
        super().__init__()

        self._callbacks = []

        #self.states = States()
        #self.utils = Utils()
        self.states = states
        self.utils = utils
        self.commons = ThermoCommons()

        self.name = name
        self.thConfig = thConfig

        #Mode is a virtual item, not bound to a phisical device so it must be created
        itemName = f'{str(self.name)}_mode'
        #if not self.oh.item_exists(itemName):
        self.openhab.create_item('Number', itemName, label='funzionamento', tags=['Control', 'Switch'], groups=[name])
      
        self._mode = float(self.utils.bindItem(
                                    itemName, 
                                    self.mode_changed, 
                                    ValueChangeEventFilter(), 1.0))

        self.ts = TemperatureSensor(thConfig, self.commons)
        self._temperature = self.ts.temperature

        if 'humidity' in thConfig:
            self.hs = HumiditySensor(thConfig, self.commons)
            self._relhumidity = self.hs.relhumidity

        self.sp = Setpoint(name)
        self._setpoint = 20.0
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
        myState = new_value
        self._mode = float(myState)
        #send the value to Thermostat class to update OH
        self._notify_observers("mode", old_value, myState)
 
    #event from device -> (it's virtual, so no need to) translate it and send to OH
    def mode_changed(self, event):
        self.mode = float(event.value)

    #event from OH -> (it's virtual, so no need to) translate it and send to mode of device
    def set_mode(self, value):
        value = float(value)
        if value != self.mode:
            retValue = value
            log.debug(f'set mode to {self.name}: {retValue}')
            self.utils.sendCommandToItem(f'{self.name}_mode', retValue)