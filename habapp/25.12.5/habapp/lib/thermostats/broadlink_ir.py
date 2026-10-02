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
from HABApp.mqtt.items import MqttItem

from thermostats.thermo_commons import ThermoCommons
from thermostats.utils import States
from system.utils import Utils
from thermostats.temperature_sensors import TemperatureSensor

class BROADLINK_IR(HABApp.Rule):
    def __init__(self, name, thConfig, fancoil):
        super().__init__()

        self._callbacks = []

        self.states = States()
        self.utils = Utils()
        self.commons = ThermoCommons()

        self.thConfig = thConfig
        self.name = name

        self._deviceID = fancoil['deviceID']
        self.my_mqtt_item = MqttItem.get_create_item(f'broadlink/{ self._deviceID }/')

        self.ts = TemperatureSensor(thConfig, self.commons)
        self._temperature = self.ts.temperature

        itemName = f'{self.name}_internalManagement'
        self.listen_event(itemName, self.switchOnOff, ValueChangeEventFilter())

    def switchOnOff(self, event):
        if float(event.value) == 0.0:
            self.my_mqtt_item.publish("off")
        else:
            self.my_mqtt_item.publish("on")