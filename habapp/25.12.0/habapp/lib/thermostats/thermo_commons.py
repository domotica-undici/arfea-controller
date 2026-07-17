import logging
log = logging.getLogger('HABApp')

import HABApp
from HABApp.openhab.events import ItemStateChangedEventFilter
from system.utils import Utils

'''
    Commons
'''
class ThermoCommons(HABApp.Rule):
    def __init__(self):
        super().__init__()

        self._callbacks = []

        self.utils = Utils()

        #Load generator items
        #self.heatGenerator = cfg['generators']['heat']
        #self.coolGenerator = cfg['generators']['cool']
        #self.run.soon(self.init_items)

        '''
    Create items to manage thermoregulation
    '''
    #def init_items(self):
        #Thermoregulation
        itemName="gThermostats"
        if not self.openhab.item_exists(itemName):
            self.openhab.create_item("Group", itemName, label='Termostati', category='', tags=[], groups=[], group_type='String', group_function='', group_function_params=[])

        itemName="onoffvalves_heat"
        if not self.openhab.item_exists(itemName):
            self.openhab.create_item("Group", itemName, label='Valvole riscaldamento', category='sani_valve_50', tags=['Status', 'Switch'], groups=[], group_type='Switch', group_function='AND', group_function_params=['ON', 'OFF'])

        itemName="onoffvalves_cool"
        if not self.openhab.item_exists(itemName):
            self.openhab.create_item("Group", itemName, label='Valvole raffrescamento', category='sani_valve_50', tags=['Status', 'Switch'], groups=[], group_type='Switch', group_function='AND', group_function_params=['ON', 'OFF'])

        #Raggruppa i sensori di temperatura, usato per l'interazione con HECOS
        itemName="gTemperatureSensors"
        if not self.openhab.item_exists(itemName):
            self.openhab.create_item("Group", itemName, label='Sensori di temperatura', category='', tags=[], groups=[], group_type='Number')

        #Raggruppa i setpoint, usato per l'interazione con HECOS
        itemName="gThermoSetpoint"
        if not self.openhab.item_exists(itemName):
            self.openhab.create_item("Group", itemName, label='Setpoint', category='', tags=[], groups=[], group_type='Number')

        itemName="thermoSetup"
        if not self.openhab.item_exists(itemName):
            self.openhab.create_item("String", itemName, label='Pianificazione oraria', groups=['gPersistence'])

        self._thermoSetup = str(self.utils.bindItem(
                                            itemName,
                                            self.set_thermoSetup,
                                            ItemStateChangedEventFilter(), "[]"))

        itemName="season"
        if not self.openhab.item_exists(itemName):
            self.openhab.create_item("String", itemName, label='Stagione', groups=['gPersistence'])
        self._season = str(self.utils.bindItem(
                                    itemName,
                                    self.set_season,
                                    ItemStateChangedEventFilter(), "W"))

        itemName="deltaSetpoint"
        if not self.openhab.item_exists(itemName):
            self.openhab.create_item("Number", itemName, label='Delta Setpoint', category='', tags=[], groups=['gPersistence'])
            self.openhab.set_metadata(itemName, "stateDescription", "displayState", {'pattern': '%.1f °C'} )
        self._deltaSetpoint = float(self.utils.bindItem(itemName, self.set_deltaSetpoint, ItemStateChangedEventFilter(), 0.2))

        itemName="antiFreezeTemp"
        if not self.openhab.item_exists(itemName):
            self.openhab.create_item("Number", itemName, label='T. Antigelo', category='', tags=[], groups=['gPersistence'])
            self.openhab.set_metadata(itemName, "stateDescription", "displayState", {'pattern': '%.1f °C'} )
        self._antiFreezeTemp = float(self.utils.bindItem(itemName, self.set_antiFreezeTemp, ItemStateChangedEventFilter(), 6.0))

        itemName="windowStopThermo"
        if not self.openhab.item_exists(itemName):
            self.openhab.create_item("Number", itemName, label='Fermo su finestra aperta', category='', tags=[], groups=['gPersistence'])
            self.openhab.set_metadata(itemName, "stateDescription", "displayState", {'pattern': '%.1f s'} )
        self._windowStopThermo = float(self.utils.bindItem(itemName, self.set_windowStopThermo, ItemStateChangedEventFilter(), 30.0))

    def _notify_observers(self, parameter,old_value, new_value):
        for callback in self._callbacks:
            callback(parameter, old_value, new_value)

    def register_callback(self, callback):
        self._callbacks.append(callback)

    @property
    def thermoSetup(self):
        return str(self._thermoSetup)

    @thermoSetup.setter
    def thermoSetup(self, new_value):
        old_value = self._thermoSetup
        self._thermoSetup = str(new_value)
        self._notify_observers("thermoSetup", old_value, new_value)

    def set_thermoSetup(self, event):
        self.thermoSetup = str(event.value)

    @property
    def season(self):
        return str(self._season)

    @season.setter
    def season(self, new_value):
        old_value = self._season
        self._season = str(new_value)
        self._notify_observers("season", old_value, new_value)

    def set_season(self, event):
        self.season = str(event.value)
    
    @property
    def deltaSetpoint(self):
        return float(self._deltaSetpoint)

    @deltaSetpoint.setter
    def deltaSetpoint(self, new_value):
        old_value = self._deltaSetpoint
        self._deltaSetpoint = float(new_value)
        self._notify_observers("deltaSetpoint", old_value, new_value)

    def set_deltaSetpoint(self, event):
        self.deltaSetpoint = float(event.value)

    @property
    def antiFreezeTemp(self):
        return float(self._antiFreezeTemp)

    @antiFreezeTemp.setter
    def antiFreezeTemp(self, new_value):
        old_value = self._antiFreezeTemp
        self._antiFreezeTemp = float(new_value)
        self._notify_observers("antiFreezeTemp", old_value, new_value)

    def set_antiFreezeTemp(self, event):
        self.antiFreezeTemp = float(event.value)
    
    @property
    def windowStopThermo(self):
        return float(self._windowStopThermo)

    @windowStopThermo.setter
    def windowStopThermo(self, new_value):
        old_value = self._windowStopThermo
        self._windowStopThermo = float(new_value)
        self._notify_observers("windowStopThermo", old_value, new_value)

    def set_windowStopThermo(self, event):
        self.windowStopThermo = float(event.value)
