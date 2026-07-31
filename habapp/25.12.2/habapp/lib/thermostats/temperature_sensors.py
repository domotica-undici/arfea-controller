'''
    Temperature sensor object
    This class manages a temperature sensor belonging to a thermostat
    Used with virtual thermosat

    About the items:
        1) physical devices with embedded Temperature sensor: do not add sensor in thermo.yml
        or
        2) physical devices without embedded Temperature sensor or virtual thermostat: add sensor in thermo.yml
            example thermo.yml: 
              - name: TermostatoMatrimoniale
                model: mh8
                ...
                temperature: temperaturaMatrimoniale

'''

import logging
log = logging.getLogger('HABApp')

import HABApp
from HABApp.core.events import ValueChangeEventFilter
from thermostats.utils import THUtils
from system.utils import Utils

class TemperatureSensor(HABApp.Rule):
    def __init__(self, thConfig=None, commons=None):
        super().__init__()

        self.utils = Utils()
        self.thutils = THUtils()

        self.name = str(thConfig['name'])

        if 'temperature_sensor' in thConfig:
            #if is defined in thermo.yml
            itemName = thConfig['temperature_sensor']
        else:
            #if is not defined in thermo.yml
            itemName = f'{str(self.name)}_temperature'

        #this will put the sensor under the thermostat's group
        self.thutils.create_item('temperature_sensor', itemName, [str(thConfig['ambient']), "gTemperatureSensors", self.name])

        self._itemName = itemName

        self._temperature = float(self.utils.bindItem(
                            itemName, 
                            self.temperature_changed, 
                            ValueChangeEventFilter(), 0.0))

    @property
    def get_name(self):
        return str(self._itemName)
        
#_temperature
    @property
    def temperature(self):
        return float(self._temperature)

    @temperature.setter
    def temperature(self, new_value):        
        new_value = float(new_value)
        old_value = self.temperature
        if old_value != new_value:
            self._temperature = new_value

    #Triggered by item value change
    def temperature_changed(self, event):
        self.temperature = float(event.value)