'''
    MCO HOME MH8 object
    This class manage one MCO HOME - MH8 Fancoil thermostat

    About the items:
        1) if there is a device with the THING named the same as described in thermo.yml and the THING 
            is defined into config/mh8.yml-> habapp has already created items and bound them to the device's channel
            example thermo.yml: 
              - name: TermostatoMatrimoniale
                model: mh8
        OR
        2) if there is not a physycal device this class should not be called and no MH8 defined in thermo.yml

'''

import logging
log = logging.getLogger('HABApp')

import HABApp
from HABApp.core.events import ValueChangeEventFilter

import json

from thermostats.thermo_commons import ThermoCommons
from thermostats.utils import States
from system.utils import Utils
from thermostats.temperature_sensors import TemperatureSensor
from thermostats.humidity_sensors import HumiditySensor
from thermostats.setpoint import Setpoint

class LSTAT(HABApp.Rule):
    def __init__(self, name, thConfig):
        super().__init__()

        self._callbacks = []

        self.states = States()
        self.utils = Utils()
        self.commons = ThermoCommons()

        self.name = name

        self.ts = TemperatureSensor(thConfig, self.commons)
        self._temperature = self.ts.temperature

        self.hs = HumiditySensor(thConfig, self.commons)
        self._relhumidity = self.hs.relhumidity

        #_mod items has not been created by the lstat.yml because there is no channel
        itemName = f'{str(self.name)}_mode'
        #this will put the sensor under the thermostat's group
        self.openhab.create_item('Number', itemName, label='Funzionamento', tags=[], groups=[name])


        self._mode = float(self.utils.bindItem(
                                    itemName, 
                                    self.mode_changed, 
                                    ValueChangeEventFilter(), 1.0))

        self.sp = Setpoint(name)
        self._setpoint = 20.0
        #self.sp.register_callback(self.parameter_changed)

        self._fanstate = float(self.utils.bindItem(
                                    f'{str(self.name)}_fanstate', 
                                    self.fanstate_changed, 
                                    ValueChangeEventFilter(), 0.0))

        #Listen to cool valve status changed
        #itemName = str(f'{self.name}_onoffvalves_cool')
        #self.listen_event(itemName, self.onoffvalves_changed, ValueChangeEventFilter())

        #Listen to heat valve status changed
        itemName = str(f'{self.name}_onoffvalves_heat')
        self.listen_event(itemName, self.onoffvalves_changed, ValueChangeEventFilter())
        
        #Listen to min e max setpoint changed
        itemName = str(f'{self.name}_tRanges')
        self.listen_event(itemName, self.tRanges_changed, ValueChangeEventFilter())

        #Listen for fan speed changed by logic
        itemName = str(f'{self.name}_fancoils_speed')
        self.listen_event(itemName, self.fancoils_speed_changed, ValueChangeEventFilter())

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
        #old_value = self.mode
        self._mode = new_value
        
    #event from device -> send to OH
    def mode_changed(self, event):
        return

    #event from OH -> send to device
    def set_mode(self, value):
        value = float(value)
        #change display accordingly
        retValue2 = 0
        retValue3 = 0

        myItem = f'{self.name}_symbol_direct_access2'
        if not self.oh.item_exists(myItem):
            self.oh.create_item('Number', myItem, label=myItem, tags=[], groups=[self.name])
        myDisplay2 = int(self.oh.get_item(myItem).state)

        myItem = f'{self.name}_symbol_direct_access3'
        if not self.oh.item_exists(myItem):
            self.oh.create_item('Number', myItem, label=myItem, tags=[], groups=[self.name])
        myDisplay3 = int(self.oh.get_item(myItem).state)
        t_and = 0b1111111111110000

        if value == self.states.internalModes()["OFF"]:
            #clean
            retValue2 = myDisplay2 & t_and
            retValue3 = myDisplay3 & t_and

        elif value == self.states.internalModes()["HEAT"]:
            #clean
            myDisplay2 = myDisplay2 & t_and
            #set correct value
            t_or = 0b0100 #icona caldo
            retValue2 = myDisplay2 | t_or

        elif value == self.states.internalModes()["COOL"]:
            #clean
            myDisplay2 = myDisplay2 & t_and
            #set correct value
            t_or = 0b0001 #icona freddo
            retValue2 = myDisplay2 | t_or

        elif value == self.states.internalModes()["FAN"]:
            #clean
            retValue2 = myDisplay2 & t_and


        log.debug(f'set {self.name} _symbol_direct_access2 to: {retValue2}')
        log.debug(f'set {self.name} _symbol_direct_access3 to: {retValue3}')
        self.utils.sendCommandToItem(f'{self.name}_symbol_direct_access2', retValue2)
        self.utils.sendCommandToItem(f'{self.name}_symbol_direct_access3', retValue3)

        self.mode = value

#Fan state
    @property
    def fanstate(self):
        return float(self._fanstate)

    @fanstate.setter
    def fanstate(self, new_value):
        #old_value = self._fanstate
        self._fanstate = new_value
        #self._notify_observers("fanstate", old_value, new_value)
    
    #event from device -> send to logic
    def fanstate_changed(self, event):
        value = int(event.value)

        myDisplay3 = int(self.oh.get_item(f'{self.name}_symbol_direct_access3').state)
        t_and = 0b1111111111111100
        myDisplay3 = myDisplay3 & t_and
        t_or = 0b0000000000000001 #mi accerto che il simbolo della ventola (1° bit sia acceso)
        myDisplay3 = myDisplay3 | t_or

        myState = self.states.internalFanSpeeds()["AUTO"]
        if value == 0:
            t_and = 0b1111111111111100
            myDisplay3 = myDisplay3 & t_and
            myState = self.states.internalFanSpeeds()["OFF"]
        elif value == 1:
            myState = self.states.internalFanSpeeds()["LOW"]
        elif value == 2:
            myState = self.states.internalFanSpeeds()["MID"]
        elif value == 3:
            myState = self.states.internalFanSpeeds()["HIGH"]
        
        self.fanstate = float(myState)
        #set on/off fan icon
        self.utils.sendCommandToItem(f'{self.name}_symbol_direct_access3', myDisplay3)
        #set thermostat fan state item
        self.utils.sendCommandToItem(f'{self.name}_fancoils_internalSpeed', value)

    #event from OH -> send to device
    def fancoils_speed_changed(self, event):
        self.utils.sendCommandToItem(f'{self.name}_fanstate', event.value)

    #event from OH -> send to device
    def onoffvalves_changed(self, event):
        #change display accordingly
        retValue3 = 0
        myDisplay3 = int(self.oh.get_item(f'{self.name}_symbol_direct_access3').state)
        t_and = 0b1111111111110011
        retValue3 = myDisplay3 & t_and

        if event.value == "ON":
            retValue3 = myDisplay3 & t_and
            t_or = 0b0100 #accendo il simbolo della valvola
            retValue3 = retValue3 | t_or

        log.debug(f'set {self.name} _symbol_direct_access3 to: {retValue3}')
        self.utils.sendCommandToItem(f'{self.name}_symbol_direct_access3', retValue3)

    #event from OH -> send to device
    def onoffvalves_heat_changed(self, event):
        #change display accordingly
        retValue3 = 0
        myDisplay3 = int(self.oh.get_item(f'{self.name}_symbol_direct_access3').state)
        t_and = 0b1111111111110011
        retValue3 = myDisplay3 & t_and

        if event.value == "ON":
            retValue3 = myDisplay3 & t_and
            t_or = 0b0100 #accendo il simbolo della valvola
            retValue3 = retValue3 | t_or

        log.debug(f'set {self.name} _symbol_direct_access3 to: {retValue3}')
        self.utils.sendCommandToItem(f'{self.name}_symbol_direct_access3', retValue3)

    def tRanges_changed(self,event):
        myvalues = json.loads(self.openhab.get_item(f'{self.name}_tRanges').state)

        """ if str(self.commons.season).lower() == "w":
            tmin = myvalues['wmin']
            tmax = myvalues['wmax']
        else:
            tmin = myvalues['smin']
            tmax = myvalues['smax'] """
        
        tmin = myvalues['min']
        tmax = myvalues['max'] 

        self.utils.sendCommandToItem(f'{self.name}_set_point_min_0', tmin)
        self.utils.sendCommandToItem(f'{self.name}_set_point_max_0', tmax)