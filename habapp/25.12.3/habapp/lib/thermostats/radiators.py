'''
    Radiator object
    This class manages radio controlled radiator heads belonging to a thermostat
    This class is a middle class to bridge between a radiator object and multiple devices:
    a radiator can be managed by different kind of devices: fibaro head, netatmo, etc...
    so this is a common class that will manage the right device. ths device is defined in thermo.yml

    This is a special device because radiator heads are both a thermostat and an actuator: has temperature, setpoint, mode like a thermostat but also actuates


    About the items:
        this is not a physical device so this class will only create a group of radiators if any is defined in thermo.yml
        all real radiators will belong to this group
'''

import logging
log = logging.getLogger('HABApp')

import HABApp
from thermostats.utils import States
from thermostats.utils import THUtils
from thermostats.fgt001 import FGT001
from thermostats.danfoss_lc13 import LC13

class Radiator(HABApp.Rule):
    def __init__(self, thConfig=None, commons=None):
        super().__init__()

        self._callbacks = []

        self.states = States()
        self.thutils = THUtils()
                
        self.name = str(thConfig['name'])

        self._radiatorsList = []
        self._setpoint = 21.0
        
        if 'radiators' in thConfig:
            #this group manages all radiators in thermostat
            #self.thutils.create_item('radiatorsGroup', str(f'{self.name}_radiators'), ['radiators', str(thConfig['ambient'])])
            self.thutils.create_item('radiatorsGroup', str(f'{self.name}_radiators'), ['radiators'])

            for radiator in thConfig['radiators']:
                #add radiator to radiator's group
                self.thutils.create_item('radiator', str(radiator["name"]), [str(f'{self.name}_radiators')])

                t_model = str(radiator['model']).lower()

                if t_model == 'fgt001':
                    #items are already created
                    t_radiator = FGT001(radiator["name"], thConfig)
                if t_model == 'lc13':
                    #items are already created
                    t_radiator = LC13(radiator["name"], thConfig)

                self._radiatorsList.append(t_radiator)

        log.debug(f'Radiators list: {self._radiatorsList}')

#_mode
    #event from logic -> send to device
    def set_mode(self, value):
        value = float(value)
        for radiator in self._radiatorsList:
            log.debug(f'Imposto la modalità per {radiator.name} a {value}')
            radiator.set_mode(value)

#_setpoint
    @property
    def setpoint(self):
        return float(self._setpoint)

    @setpoint.setter
    def setpoint(self, new_value):
        old_value = self.setpoint
        if old_value != float(new_value):
            self._setpoint = float(new_value)
            self.set_setpoint(new_value)

    #event from logic -> send to device
    #this function is called by thermo_thermostat -> set_actuators -> set_radiators
    def set_setpoint(self, value):
        value = float(value)
        for radiator in self._radiatorsList:
            log.debug(f'Imposto il setpoint per {radiator.name} a {value}')
            radiator.sp.setpoint_changed(value)
