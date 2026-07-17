'''

'''

import logging
log = logging.getLogger('HABApp')

import HABApp
from HABApp.core.events import ValueChangeEventFilter,  ValueUpdateEventFilter

from thermostats.thermo_commons import ThermoCommons
from thermostats.utils import States
from system.utils import Utils

class FANCOIL_VIESSMANN(HABApp.Rule):
    def __init__(self, name, fancoilName, coolHeat):
        super().__init__()

        self._callbacks = []

        self.states = States()
        self.utils = Utils()
        self.commons = ThermoCommons()

        self.name = name
        self.fancoilName = fancoilName
        self.coolHeat = coolHeat

        #listen to thermostat status change
        self.listen_event(f'{self.name}_internalMode', self.thermostat_mode_changed, ValueChangeEventFilter())
        #listen to setpoint update
        self.listen_event(f'{self.name}_setpoint', self.setpoint_updated, ValueUpdateEventFilter())
        #listen to temperature update
        self.listen_event(f'{self.name}_temperature', self.temperature_updated, ValueUpdateEventFilter())

        #listen to speed change
        self.listen_event(f'{self.name}_fancoils_speed', self.speed_changed, ValueChangeEventFilter())
        #listen to season change
        self.listen_event("season", self.season_changed, ValueUpdateEventFilter())

    def _notify_observers(self, parameter,old_value, new_value):
        for callback in self._callbacks:
            callback(parameter, old_value, new_value)

    def register_callback(self, callback):
        self._callbacks.append(callback)

    #triggered by subclass' callbacks 
    def parameter_changed(self, parameter, old_value, new_value):
        return

    def thermostat_mode_changed(self, event):
        if self.oh.get_item(f'{self.fancoilName}_statusbits').state == 'NULL':
            return
        
        fcStatus = int(self.oh.get_item(f'{self.fancoilName}_statusbits').state)

        value = float(event.value)
        if value == self.states.internalModes()["OFF"]:
            t_or = 0b0000000010000000 #bit7 = 1
            retValue = fcStatus | t_or
            self.openhab.send_command(f'{self.fancoilName}_statusbits', retValue)
        else:
            if self.coolHeat != None:
                t_and = 0b1111111101111111 #bit7 = 0
                retValue = fcStatus & t_and

                if self.coolHeat == 'cool' and self.commons.season.lower() == "s":
                    self.openhab.send_command(f'{self.fancoilName}_statusbits', retValue)
                    return

                if self.coolHeat == 'heat' and self.commons.season.lower() == "w":
                    self.openhab.send_command(f'{self.fancoilName}_statusbits', retValue)
                    return

            t_or = 0b0000000010000000 #bit7 = 1
            retValue = fcStatus | t_or
            self.openhab.send_command(f'{self.fancoilName}_statusbits', retValue)


    def setpoint_updated(self, event):
        self.openhab.send_command(f'{self.fancoilName}_setpoint', event.value)

    def temperature_updated(self, event):
        self.openhab.send_command(f'{self.fancoilName}_temperature', event.value)

    def speed_changed(self, event):
        speed = float(event.value)

        if self.oh.get_item(f'{self.fancoilName}_statusbits').state == 'NULL':
            return

        fcStatus = int(self.oh.get_item(f'{self.fancoilName}_statusbits').state)
        t_and = 0b1111111111111111 #means "do nothing"
        retValue = fcStatus & t_and 

        if speed == 0:
            t_and = 0b1111111111111000 #bit 0,1,2 = 0
            retValue = fcStatus & t_and
            t_or = 0b0000000010000000 #bit7 = 1
            retValue = retValue | t_or
            #self.openhab.send_command(f'{self.fancoilName}_statusbit7', 1)
            #self.openhab.send_command(f'{self.fancoilName}_statusbit0', 0)
            #self.openhab.send_command(f'{self.fancoilName}_statusbit1', 0)
            #self.openhab.send_command(f'{self.fancoilName}_statusbit2', 0)

        elif speed == 1:
            t_and = 0b1111111101111000 #bit7 = 0, bit 0,1,2 = 0
            retValue = fcStatus & t_and
            t_or = 0b0000000000000001 #bit 0 = 1
            retValue = retValue | t_or
            #self.openhab.send_command(f'{self.fancoilName}_statusbit7', 0)
            #self.openhab.send_command(f'{self.fancoilName}_statusbit0', 1)
            #self.openhab.send_command(f'{self.fancoilName}_statusbit1', 0)
            #self.openhab.send_command(f'{self.fancoilName}_statusbit2', 0)

        elif speed == 2:
            t_and = 0b1111111101111000 #bit7 = 0, bit 0,1,2 = 0
            retValue = fcStatus & t_and
            t_or = 0b0000000000000010 #bit 1 = 1
            retValue = retValue | t_or
            #self.openhab.send_command(f'{self.fancoilName}_statusbit7', 0)
            #self.openhab.send_command(f'{self.fancoilName}_statusbit0', 0)
            #self.openhab.send_command(f'{self.fancoilName}_statusbit1', 1)
            #self.openhab.send_command(f'{self.fancoilName}_statusbit2', 0)
        elif speed == 3:
            t_and = 0b1111111101111000 #bit7 = 0, bit 0,1,2 = 0
            retValue = fcStatus & t_and
            t_or = 0b0000000000000011 #bit 0,1 = 1
            retValue = retValue | t_or

            #self.openhab.send_command(f'{self.fancoilName}_statusbit7', 0)
            #self.openhab.send_command(f'{self.fancoilName}_statusbit0', 1)
            #self.openhab.send_command(f'{self.fancoilName}_statusbit1', 1)
            #self.openhab.send_command(f'{self.fancoilName}_statusbit2', 0)

        self.openhab.send_command(f'{self.fancoilName}_statusbits', retValue)

    def season_changed(self, event):
        if self.oh.get_item(f'{self.fancoilName}_statusbits').state == 'NULL':
            return

        fcStatus = int(self.oh.get_item(f'{self.fancoilName}_statusbits').state)
        t_and = 0b1111111111111111 #means "do nothing"
        retValue = fcStatus & t_and 

        if str(event.value).lower() == "w":
            t_and = 0b1011111111111111 #bit14 = 0
            retValue = fcStatus & t_and
            t_or = 0b0010000000000000 #bit13 = 1
            retValue = retValue | t_or
            #self.openhab.send_command(f'{self.fancoilName}_statusbit13', 1) #W
            #self.openhab.send_command(f'{self.fancoilName}_statusbit14', 0) #S

        else:
            t_and = 0b1101111111111111 #bit13 = 0
            retValue = fcStatus & t_and
            t_or = 0b0100000000000000 #bit14 = 1
            retValue = retValue | t_or
            #self.openhab.send_command(f'{self.fancoilName}_statusbit13', 0) #W
            #self.openhab.send_command(f'{self.fancoilName}_statusbit14', 1) #S

        self.openhab.send_command(f'{self.fancoilName}_statusbits', retValue)
