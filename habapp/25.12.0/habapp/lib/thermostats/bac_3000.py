'''
    BAC 3000 object
'''

import logging


log = logging.getLogger('HABApp')

import HABApp
from HABApp.core.events import ValueChangeEventFilter

from thermostats.thermo_commons import ThermoCommons
from thermostats.utils import States, THUtils
from system.utils import Utils
from thermostats.temperature_sensors import TemperatureSensor
from thermostats.setpoint import Setpoint
import datetime

class BAC_3000(HABApp.Rule):
    def __init__(self, name, thConfig):
        super().__init__()

        self._callbacks = []

        self.states = States()
        self.utils = Utils()
        self.thutils = THUtils()
        self.commons = ThermoCommons()

        self.name = name
        
        self.ts = TemperatureSensor(thConfig, self.commons)
        self._temperature = self.ts.temperature
        self.synchronize_datetime()
            
        itemName = f'{str(self.name)}_mode'
        
        self._mode = float(self.utils.bindItem(
                                    itemName, 
                                    self.mode_changed, 
                                    ValueChangeEventFilter(), 1.0))

        self.sp = Setpoint(name)
        self._setpoint = 20.0
        #self.sp.register_callback(self.parameter_changed)

        #device fan speed item
        itemName = f'{str(self.name)}_fanstate'
        self._fanstate = float(self.utils.bindItem(
                                    itemName, 
                                    self.fanstate_changed, 
                                    ValueChangeEventFilter(), 0.0))

        #internal fan speed item
        self.openhab.create_item("Number", f'{self.name}_fancoils_internalSpeed', label='Gestione interna velocità', tags=[], groups=[self.name])
        self._internalSpeed = int(float(self.utils.bindItem(
                        f'{str(self.name)}_fancoils_internalSpeed', 
                        self.fancoils_internalSpeed_changed,
                        ValueChangeEventFilter(), 0.0)))

        itemName = f'{self.name}_internalManagement'
        self.listen_event(itemName, self.switchOnOff, ValueChangeEventFilter())

    def _notify_observers(self, parameter,old_value, new_value):
        for callback in self._callbacks:
            callback(parameter, old_value, new_value)

    def register_callback(self, callback):
        self._callbacks.append(callback)

    #triggered by subclass' callbacks 
    def parameter_changed(self, parameter, old_value, new_value):
        return

    def switchOnOff(self, event):
        if float(event.value) == 0.0:
            self.utils.sendCommandToItem(f'{self.name}_power', "OFF")
        else:
            self.utils.sendCommandToItem(f'{self.name}_power', "ON")

        self.synchronize_datetime()

#_mode
    @property
    def mode(self):
        return float(self._mode)

    @mode.setter
    def mode(self, new_value):
        new_value = float(new_value)
        old_value = self.mode
        if old_value != new_value:
            self._mode = float(new_value)

    #event from device -> send to OH
    def mode_changed(self, event):
        value = float(event.value)
        if value == 0.0:
            myState = self.states.internalModes()["COOL"]
        elif value == 1.0:
            myState = self.states.internalModes()["HEAT"]
        elif value == 2.0:
            myState = self.states.internalModes()["FAN"]
        self.mode = float(myState)

    #event from OH -> send to device
    def set_mode(self, value):
        value = float(value)
        if value != self.mode:
            retValue = 0
            if value == self.states.internalModes()["COOL"]:
                retValue = 0
            elif value == self.states.internalModes()["HEAT"]:
                retValue = 1
            elif value == self.states.internalModes()["FAN"]:
                retValue = 2
            else:
                #otherwise do nothing
                return

            log.debug(f'set mode to {self.name}: {retValue}')
            self.utils.sendCommandToItem(f'{self.name}_mode', retValue)

#Fan State (Speed)
    @property
    def fanstate(self):
        return float(self._fanstate)

    @fanstate.setter
    def fanstate(self, new_value):
        new_value = float(new_value)
        old_value = self.fanstate
        if old_value != new_value:
            self._fanstate = new_value
            #if value is changed, change internal value accordingly
            self.utils.sendCommandToItem(str(f'{self.name}_fancoils_internalSpeed'), new_value)

    @property
    def internalSpeed(self):
        return float(self._internalSpeed)

    @internalSpeed.setter
    def internalSpeed(self, new_value):
        new_value = float(new_value)
        old_value = self.internalSpeed
        if old_value != new_value:
        #if self.fanstate != new_value:
            self._internalSpeed = new_value
            #if value is changed, change device value accordingly
            self.set_fanstate(new_value)

    #event from device: translate it and apply to _[variable]
    def fanstate_changed(self, event):
        value = float(event.value)
        myState = self.states.internalFanSpeeds()["AUTO"]
        if value == 3.0:
            myState = self.states.internalFanSpeeds()["LOW"]
        elif value == 2.0:
            myState = self.states.internalFanSpeeds()["MID"]
        elif value == 1.0:
            myState = self.states.internalFanSpeeds()["HIGH"]
        elif value == 0.0:
            myState = self.states.internalFanSpeeds()["AUTO"]

        self.fanstate = float(myState)

    #event from logic -> send to device
    def fancoils_internalSpeed_changed(self, event):
        log.debug(f'Fancoils {event.name} speed changed from {event.old_value} to {event.value}')
        self.internalSpeed = float(int(event.value))

    #event from logic -> send to device
    def set_fanstate(self, value):
        value = float(value)
        
        if value == self.states.internalFanSpeeds()["AUTO"]:
            retValue = 0
        elif value == self.states.internalFanSpeeds()["LOW"]:
            retValue = 3
        elif value == self.states.internalFanSpeeds()["MID"]:
            retValue = 2
        elif value == self.states.internalFanSpeeds()["HIGH"]:
            retValue = 1
        elif value == self.states.internalFanSpeeds()["OFF"]:
            retValue = 3
        
        self.utils.sendCommandToItem(f'{self.name}_fanstate', retValue)

    def synchronize_datetime(self):       
        self.utils.sendCommandToItem(f'{self.name}_minute', int(str(datetime.datetime.now().time()).split(":")[1]))
        self.utils.sendCommandToItem(f'{self.name}_hour', int(str(datetime.datetime.now().time()).split(":")[0]))
        self.utils.sendCommandToItem(f'{self.name}_weekDay', 1 + int(str(datetime.datetime.today().weekday()).split(":")[0]))
