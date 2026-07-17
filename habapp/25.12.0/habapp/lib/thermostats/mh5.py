'''
    MCO HOME MH5 object
    This class manage one MCO HOME - MH5 Fancoil thermostat

    About the items:
        1) if there is a device with the THING named the same as described in thermo.yml and the THING 
            is defined into config/mh5.yml-> habapp has already created items and bound them to the device's channel
            example thermo.yml: 
              - name: TermostatoMatrimoniale
                model: MH5
        OR
        2) if there is not a physycal device this class should not be called and no MH5 defined in thermo.yml

'''

import logging
log = logging.getLogger('HABApp')

import HABApp
from HABApp.core.events import ValueChangeEventFilter

#from thermostats.utils import States
#from system.utils import Utils
from thermostats.temperature_sensors import TemperatureSensor
from thermostats.setpoint import Setpoint

class MH5(HABApp.Rule):
    def __init__(self, name, thConfig, commons, states, utils):
        super().__init__()

        self._callbacks = []

        #self.states = States()
        #self.utils = Utils()
        self.states = states
        self.utils = utils
        self.commons = commons

        self.name = name

        self.ts = TemperatureSensor(thConfig, self.commons)
        self._temperature = self.ts.temperature

        self._mode = float(self.utils.bindItem(
                                    f'{str(self.name)}_mode', 
                                    self.mode_changed, 
                                    ValueChangeEventFilter(), 1.0))

        """ self.operatingstate = float(self.utils.bindItem(
                                    f'{str(self.name)}_operatingstate', 
                                    self.operatingstate_changed, 
                                    ValueChangeEventFilter(), 0.0)) """

        self.sp = Setpoint(name, 2)
        self._setpoint = 20.0
        #self._setpoint_cooling = self.sp.setpoint_cooling
        #self._setpoint_heating = self.sp.setpoint_heating
        #self.sp.register_callback(self.parameter_changed)

        #device fan speed item
        self._fanmode = float(self.utils.bindItem(
                                    f'{str(self.name)}_fanmode', 
                                    self.fanmode_changed, 
                                    ValueChangeEventFilter(), 0.0))

        self._fanstate = float(self.utils.bindItem(
                                    f'{str(self.name)}_fanstate', 
                                    self.fanstate_changed, 
                                    ValueChangeEventFilter(), 0.0))

        #internal fan speed item
        self.openhab.create_item("Number", f'{self.name}_fancoils_internalSpeed', label='Gestione interna velocità', tags=[], groups=[self.name])
        self._internalSpeed = int(float(self.utils.bindItem(
                        f'{str(self.name)}_fancoils_internalSpeed', 
                        self.fancoils_internalSpeed_changed,
                        ValueChangeEventFilter(), 0.0)))


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
        #send the value to Thermostat class to update OH
        self._notify_observers("mode", old_value, new_value)

    #event from device -> send to OH
    def mode_changed(self, event):
        value = float(event.value)
        if value == 0.0:
            myState = self.states.internalModes()["OFF"]
        elif value == 1.0:
            myState = self.states.internalModes()["HEAT"]
        elif value == 2.0:
            myState = self.states.internalModes()["COOL"]
        elif value == 6.0:
            myState = self.states.internalModes()["FAN"]
        else:
            myState = self.states.internalModes()["OFF"]
        self.mode = float(myState)

    #event from OH -> send to device
    def set_mode(self, value):
        value = float(value)
        if value != self.mode:
            retValue = 0
            if value == self.states.internalModes()["OFF"]:
                retValue = 0
            elif value == self.states.internalModes()["HEAT"]:
                retValue = 1
            elif value == self.states.internalModes()["COOL"]:
                retValue = 2
            elif value == self.states.internalModes()["FAN"]:
                retValue = 6
            else:
                #otherwise turn off
                retValue = 0

            log.debug(f'set mode to {self.name}: {retValue}')
            self.utils.sendCommandToItem(f'{self.name}_mode', retValue)

        
    """def operatingstate_changed(self, event):
        return
        value = float(event.value)
        myState = self.states.internalWorkingStates()["IDLE"]
        if value == 0:
            myState = self.states.internalWorkingStates()["IDLE"]
        elif value == 1:
            myState = self.states.internalWorkingStates()["HEATING"]
        elif value == 2:
            myState = self.states.internalWorkingStates()["COOLING"]
        elif value == 3:
            myState = self.states.internalWorkingStates()["FAN"]
        elif value == 4:
            myState = self.states.internalWorkingStates()["PENDING_HEAT"]
        elif value == 5:
            myState = self.states.internalWorkingStates()["PENDING_COOL"]
        elif value == 6:
            myState = self.states.internalWorkingStates()["ECON"]

        if myState != self.operatingstate:
            self.operatingstate = myState
            #self.utils.sendCommandToItem(f'{str(self.name)}_internalWorkingState', myState)
    
    def set_operatingstate(self, value):
        if value != self.operatingstate:
            if value == self.states.internalWorkingStates()["IDLE"]:
                retValue = 0
            elif value == self.states.internalWorkingStates()["HEATING"]:
                retValue = 1
            elif value == self.states.internalWorkingStates()["COOLING"]:
                retValue = 2
            elif value == self.states.internalWorkingStates()["FAN"]:
                retValue = 3
            elif value == self.states.internalWorkingStates()["PENDING_HEAT"]:
                retValue = 4
            elif value == self.states.internalWorkingStates()["PENDING_COOL"]:
                retValue = 5
            elif value == self.states.internalWorkingStates()["ECON"]:
                retValue = 6

            self.operatingstate = value
            self.utils.sendCommandToItem(f'{self.name}_operatingstate', retValue) """

#Fan Mode (Speed)
    @property
    def fanmode(self):
        return float(self._fanmode)

    @fanmode.setter
    def fanmode(self, new_value):
        new_value = float(new_value)
        old_value = self.fanmode
        if old_value != new_value:
            self._fanmode = new_value
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
            self._internalSpeed = new_value
            #if value is changed, change device value accordingly
            self.set_fanmode(new_value)

    #event from device
    def fanmode_changed(self, event):
        value = float(event.value)
        myState = self.states.internalFanSpeeds()["AUTO"]
        if value == 1.0:
            myState = self.states.internalFanSpeeds()["LOW"]
        elif value == 5.0:
            myState = self.states.internalFanSpeeds()["MID"]
        elif value == 3.0:
            myState = self.states.internalFanSpeeds()["HIGH"]
        elif value == 0.0:
            myState = self.states.internalFanSpeeds()["AUTO"]

        self.fanmode = float(myState)

    #event from logic -> send to device
    def fancoils_internalSpeed_changed(self, event):
        log.debug(f'Fancoils {event.name} speed changed from {event.old_value} to {event.value}')
        self.internalSpeed = float(int(event.value))

    #event from logic -> send to device
    def set_fanmode(self, value):
        value = float(value)
        if value != self.fanmode:
            if value == self.states.internalFanSpeeds()["LOW"]:
                retValue = 1
            elif value == self.states.internalFanSpeeds()["MID"]:
                retValue = 5
            elif value == self.states.internalFanSpeeds()["HIGH"]:
                retValue = 3
            elif value == self.states.internalFanSpeeds()["AUTO"]:
                retValue = 0
            
            self.utils.sendCommandToItem(f'{self.name}_fanmode', retValue)

#Fan state
    @property
    def fanstate(self):
        return float(self._fanstate)

    @fanstate.setter
    def fanstate(self, new_value):
        old_value = self._fanstate
        self._fanstate = new_value
        self._notify_observers("fanstate", old_value, new_value)
    
    #event from device
    def fanstate_changed(self, event):
        value = float(event.value)
        myState = self.states.internalFanSpeeds()["AUTO"]
        if value == 1.0:
            myState = self.states.internalFanSpeeds()["LOW"]
        elif value == 3.0:
            myState = self.states.internalFanSpeeds()["MID"]
        elif value == 2.0:
            myState = self.states.internalFanSpeeds()["HIGH"]
        
        self.fanstate = float(myState)

"""  
    #event from logic -> send to device
    def set_fanstate(self, value):
        value = float(value)
        if value != self.fanstate:
            retValue = 0
            if value == self.states.internalFanSpeeds()["LOW"]:
                retValue = 1
            elif value == self.states.internalFanSpeeds()["MID"]:
                retValue = 3
            elif value == self.states.internalFanSpeeds()["HIGH"]:
                retValue = 2

            self.utils.sendCommandToItem(f'{self.name}_fanstate', retValue)
 """