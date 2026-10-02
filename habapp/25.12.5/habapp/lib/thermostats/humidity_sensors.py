'''
    Humidity sensor object
    This class manages a Humidity sensor belonging to a thermostat
    Used with virtual thermosat

    About the items:
        1) physical devices with embedded humidity sensor: do not add sensor in thermo.yml
        or
        2) physical devices without embedded humidity sensor or virtual thermostat: add sensor in thermo.yml
            example thermo.yml: 
              - name: TermostatoMatrimoniale
                model: mh8
                ...
                humidity: umiditaMatrimoniale

'''

import logging
log = logging.getLogger('HABApp')

import HABApp
from HABApp.core.events import ValueChangeEventFilter
from thermostats.utils import THUtils
from system.utils import Utils

class HumiditySensor(HABApp.Rule):
    def __init__(self, thConfig=None, commons=None):
        super().__init__()

        self.utils = Utils()
        self.thutils = THUtils()

        self.name = str(thConfig['name'])

        if 'humidity' in thConfig:
            #if is defined in thermo.yml
            itemName = thConfig['humidity']
        else:
            #if is not defined in thermo.yml
            itemName = f'{str(self.name)}_relhumidity'
        
        #if not self.oh.item_exists(itemName):
        #this will put the sensor under the thermostat's group
        self.thutils.create_item('humidity_sensor', itemName, [str(thConfig['ambient']), self.name])

        self._itemName = itemName

        self._relhumidity = float(self.utils.bindItem(
                            itemName, 
                            self.relhumidity_changed, 
                            ValueChangeEventFilter(), 0.0))

    @property
    def get_name(self):
        return str(self._itemName)

#_relhumidity
    @property
    def relhumidity(self):
        return float(self._relhumidity)

    @relhumidity.setter
    def relhumidity(self, new_value):
        new_value = float(new_value)
        old_value = self.relhumidity
        if old_value != new_value:
            self._relhumidity = new_value

    #Triggered by item value change
    def relhumidity_changed(self, event):
        self.relhumidity = float(event.value)