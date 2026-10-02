'''
    Fancoil object
    This class manages fancoil and it's speeds belonging to a thermostat
    Used when physical or virtual thermosat has no actuators on board

    About the items:
    firts: create item in OH. The name of the item must be the same as described in thermo.yml
        example thermo.yml: 
            - name: TermostatoMatrimoniale
              model: mh8
              ....
              fancoils:
                - fancoilMatrimoniale

    then habapp will manage the valve (then the item bound to a thing) by itself
'''

import logging
log = logging.getLogger('HABApp')

import HABApp
from HABApp.core.events import ValueChangeEventFilter
from thermostats.thermo_commons import ThermoCommons
from thermostats.utils import States, THUtils
from system.utils import Utils

class Fancoil(HABApp.Rule):
    def __init__(self, thConfig=None, commons=None):
        super().__init__()

        self.states = States()
        self.utils = Utils()
        self.thutils = THUtils()
        self.commons = ThermoCommons()

        self.thConfig = thConfig

        self.name = str(thConfig['name'])

        '''
        Fancoil. Just give the fancoil name and the procedure will also create item for speed
        '''
        self._heatFancoilList = []
        self._coolFancoilList = []
        #self._fancoilList = []

        if 'fancoils' in thConfig:
            #this group manages all fancoils in thermostat
            self.thutils.create_item('fancoilGroup', str(f'{self.name}_fancoils'), ['fancoils', str(thConfig['ambient'])])
            #bind to my group to trigger changes

            #[nome termostato]_fancoils_internalSpeed = velocità impostata dall'utente bassa, media alta, auto)
            #[nome termostato]_fancoils_speed = velocità delle ventole fancoil. se l'utente imposta bassa, media, alta allora è uguale ad internalspeed, se auto prenderà un valore dipendente dal delta T mentre internalSpeed rimane su auto

            self._speed = float(self.utils.bindItem(
                            f'{str(self.name)}_fancoils_speed', 
                            self.fancoils_speed_changed,
                            ValueChangeEventFilter(), 0.0))
            
            self._internalSpeed = float(self.utils.bindItem(
                            f'{str(self.name)}_fancoils_internalSpeed', 
                            self.fancoils_internalSpeed_changed,
                            ValueChangeEventFilter(), 0.0))

                #this is the fancoil structure: 
                #    the group representing the fancoil
                #    the number representing the speed set
                #    three switches cabled to the three fancoil speeds
                #this will bind the fancoil to the thermostat fancoil's group

            for fancoilType in thConfig['fancoils']:
                for fancoil in thConfig['fancoils'][fancoilType]:
                    if fancoilType == 'cool':
                        fancoilList = self._coolFancoilList
                    if fancoilType == 'heat':
                        fancoilList = self._heatFancoilList

                    fancoilList.append(fancoil['name'])
                    self.get_device(fancoil, fancoilType)
                    self.thutils.create_item('fancoil', fancoil['name'], [str(f'{self.name}_fancoils'), str(thConfig['ambient'])])

            """ if 'cool' in thConfig['fancoils']:
                for fancoil in thConfig['fancoils']['cool']:
                    self._coolFancoilList.append(fancoil['name'])
                    self.get_device(fancoil, 'cool')
                    self.thutils.create_item('fancoil', fancoil['name'], [str(f'{self.name}_fancoils'), str(thConfig['ambient'])])

            if 'heat' in thConfig['fancoils']:
                for fancoil in thConfig['fancoils']['heat']:
                    self._heatFancoilList.append(fancoil['name'])
                    self.get_device(fancoil, 'heat')
                    self.thutils.create_item('fancoil', fancoil['name'], [str(f'{self.name}_fancoils'), str(thConfig['ambient'])]) """

        log.debug(f'Fancoil list (cool): {self._coolFancoilList}')
        log.debug(f'Fancoil list (heat): {self._heatFancoilList}')

    #triggered by subclass' callbacks 
    def parameter_changed(self, parameter, old_value, new_value):
        return

    def get_device(self, fancoil, coolHeat=None):
        if 'model' in fancoil:
            self.model = str(fancoil['model']).lower()
        else:
            self.model = 'virtual'

        if self.model == 'viessmann':
            from thermostats.fancoilViessmann import FANCOIL_VIESSMANN
            myOptions = FANCOIL_VIESSMANN(self.name, fancoil['name'], coolHeat)
        elif self.model == 'daikin_ac':
            from thermostats.daikin_ac import DAIKIN_AC
            myOptions = DAIKIN_AC(self.name, self.thConfig, fancoil)
        elif self.model == "broadlink_ir":
            from thermostats.broadlink_ir import BROADLINK_IR
            myOptions = BROADLINK_IR(self.name, self.thConfig, fancoil)
        else: #even if model is defined as "virtual"
            myOptions = None
        
        if myOptions != None:
            #myOptions.register_callback(self.parameter_changed)
            self.device = myOptions
        else:
            self.device = None

    @property
    def speed(self):
        return float(self._speed)

    @speed.setter
    def speed(self, new_value):
        new_value = float(new_value)
        old_value = self._speed
        if old_value != new_value:
            self._speed = new_value
            #self.utils.sendCommandToItem(str(f'{self.name}_fancoils_speed'), new_value)
            
    @property
    def internalSpeed(self):
        return float(self._internalSpeed)

    @internalSpeed.setter
    def internalSpeed(self, new_value):
        new_value = float(new_value)
        old_value = self.internalSpeed
        if old_value != new_value:
            self._internalSpeed = new_value
            #self.utils.sendCommandToItem(str(f'{self.name}_fancoils_internalSpeed'), new_value)

    """
    Speed has been set up by the logic, updating the _fancoils group. so, for each fancoil, i need to set the speed accordingly
    """
    def fancoils_internalSpeed_changed(self, event):
        log.debug(f'Fancoils {event.name} speed changed from {event.old_value} to {event.value}')
        self.internalSpeed = float(event.value)

    def fancoils_speed_changed(self, event):
        ws = self.commons.season
        if str(ws).lower() == "w":
            mylist = self._heatFancoilList
        else:
            mylist = self._coolFancoilList
            
        for item in mylist:
            #turn off all the relays to unload the motor
            #Fancoil speeds must run once at time so be sure there is only one is running
            for v in range(1,4):
                self.utils.sendCommandToItem(f'{item}_V{v}', 'OFF')

            #turn on the right relay waiting 1 second delay
            if float(event.value) > 0:
                self.changeSpeed_countdown = self.run.countdown(1, self.changeSpeed, item, event.value)
                log.debug(f'Fancoil {item} speed changed to {event.value}') #from {event.old_value}
                self.changeSpeed_countdown.reset()

    def changeSpeed(self, myFanCoil, v):
        log.debug(f'Fancoil {myFanCoil} speed changed from event.old_value to {v}')
        self.utils.sendCommandToItem(f'{myFanCoil}_V{v}', 'ON')
