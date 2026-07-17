'''
    Thermostat object
    this class represent a thermostat with it's propterties. there are sensors, actuators etc
    here there are the logics to fully manage the thermostat
'''

import logging
log = logging.getLogger('HABApp')

import HABApp
from HABApp.core.events import ValueChangeEventFilter,  ValueUpdateEventFilter
import json
import datetime
from system.utils import Utils
from thermostats.utils import States, THUtils

class Thermostat(HABApp.Rule):
    def __init__(self, thConfig=None, commons=None):
        super().__init__()

        self._callbacks = []
        
        self.states = States()
        self.utils = Utils()
        self.thutils = THUtils()
        self.thConfig = thConfig
        self.commons = commons
        self.tools = self.get_rule('Tools') #load with this syntax otherwise timers won't works

        #this is because this class must be instantiated with paramenters, else gets error
        if self.thConfig == None:
            return

        '''
        Set thermostat name from configuration file
        name must be only countnuos name ex. TermostatoSala no whitespaces, underscore etc allowed
        '''
        self.name = str(self.thConfig['name'])
        if 'label' in self.thConfig:
            self.label = str(self.thConfig['label'])
        else:
            self.label = self.name
        
        '''
        Set thermostat ambient
        '''
        self.ambient = str(self.thConfig['ambient'])
        if self.ambient == None:
            log.error("Can't add Thermostat because missing ambient in {} config".format(self.name))
            return

        '''
        Create internal items
        '''
        
        self.thutils.create_item('thGroup', str(self.name), self.ambient, self.label)

        #Load device, sensor and actuators. Create item if not exists
        self.get_device()
        
        self.get_actuators()

        """
            "internal" variables are used from OH to adapt any thermostat type to a common working situation. 
            by using "internal" items the use can work in OH with alway the same interface, despite the brand and model of thermostat used
        """

        #internalManagement = is for OH and means how the user wants to run this tehermostat (OFF, MAN, AUTO)
        self._internalManagement = float(self.utils.bindItem(
                                            f'{str(self.name)}_internalManagement', 
                                            self.internalManagement_changed, 
                                            ValueChangeEventFilter(), 0.0))

        #internalMode = is for OH and means working mode, referred to heating, cooling etc
        self._internalMode = float(self.utils.bindItem(
                                            f'{str(self.name)}_internalMode', 
                                            self.internalMode_changed, 
                                            ValueChangeEventFilter(), 1.0))

        #is for OH and is used to restore previous internal mode after procedure exited WINDOWSTOP or ANTIFREEZE
        self._oldInternalMode = None 

        #internalState = is for OH and means the thermostat situation: may be in antiFreeze as well as running histeresys cycle
        self._internalState = float(self.utils.bindItem(
                                            f'{str(self.name)}_internalState', 
                                            self.internalState_changed, 
                                            ValueChangeEventFilter(), 1.0))
               
        #used to "flag" if this thermostat nedd to enable generator
        self._callHeatGenerator = False
        self._callCoolGenerator = False
        
        #used to restore previous internal state after procedure exited WINDOWSTOP or ANTIFREEZE
        self._oldInternalState = None
        self._oldFanSpeed = None
        self._oldSetPoint = None
        
        #dewpoint item is created at thermostat creation in utils
        self._dewPoint = float(self.utils.bindItem(
                                    f'{str(self.name)}_dewpoint', 
                                    self.dewPoint_changed, 
                                    ValueChangeEventFilter(), 0.0))

        #_sSchedule = Summer scheduler settings
        myItem = str(self.utils.bindItem(f'{str(self.name)}_sSchedule', 
                                            self._sSchedule_changed, 
                                            ValueChangeEventFilter(), '[{"day": 0, "timeSet": []},{"day": 1, "timeSet": []},{"day": 2, "timeSet": []},{"day": 3, "timeSet": []},{"day": 4, "timeSet": []},{"day": 5, "timeSet": []},{"day": 6, "timeSet": []}]'))
        self._sSchedule = json.loads(myItem)
        
        #_wSchedule = Winter scheduler settings
        myItem = str(self.utils.bindItem(f'{str(self.name)}_wSchedule', 
                                            self._wSchedule_changed, 
                                            ValueChangeEventFilter(), '[{"day": 0, "timeSet": []},{"day": 1, "timeSet": []},{"day": 2, "timeSet": []},{"day": 3, "timeSet": []},{"day": 4, "timeSet": []},{"day": 5, "timeSet": []},{"day": 6, "timeSet": []}]'))
        self._wSchedule = json.loads(myItem)

        self.listen_event("thermoSetupTemp", self.thermoSetupTemp_updated, ValueUpdateEventFilter())

        #al cambio stagione riallinea internalMode (HEAT/COOL) in base a internalManagement
        self.listen_event("season", self._season_changed, ValueChangeEventFilter())

        #Temperature ranges: min and max setpoint for winter and summer
        """ myItem = str(self.utils.bindItem(f'{str(self.name)}_tRanges', 
                                            self.tRanges_changed, 
                                            ValueChangeEventFilter(), '{"wmin":16.0, "wmax":25.0, "smin":19.0, "smax":32.0}')) """
        myItem = str(self.utils.bindItem(f'{str(self.name)}_tRanges', 
                                        self.tRanges_changed, 
                                        ValueChangeEventFilter(), '{"min":16.0, "max":25.0}'))

        self._tRanges = json.loads(myItem)

        self._override = float(self.utils.bindItem(f'{str(self.name)}_override', 
                                    self.override_changed, 
                                    ValueChangeEventFilter(),  0.0))

        self.get_sensors()

#Load thermostat pumps
        if 'pumps' in self.thConfig:
            if 'cool' in self.thConfig['pumps']:
                self.coolPumps = self.thConfig['pumps']['cool']

            if 'heat' in self.thConfig['pumps']:
                self.heatPumps = self.thConfig['pumps']['heat']

        self._hasWindows = False
        if self.oh.item_exists(str(f'{self.name}_windows')):
            self._hasWindows = True

        self._hasOnoffvalves_cool = False
        if self.oh.item_exists(str(f'{self.name}_onoffvalves_cool')):
            self._hasOnoffvalves_cool = True

        self._hasOnoffvalves_heat = False
        if self.oh.item_exists(str(f'{self.name}_onoffvalves_heat')):
            self._hasOnoffvalves_heat = True

        self._hasAnalogvalves = False
        if self.oh.item_exists(str(f'{self.name}_analogvalves')):
            self._hasAnalogvalves = True

        self._hasFancoils = False
        if self.oh.item_exists(str(f'{self.name}_fancoils')):
            self._hasFancoils = True
            self._oldFanSpeed = self.fancoils.internalSpeed

        self._hasRadiators = False
        if self.oh.item_exists(str(f'{self.name}_radiators')):
            self._hasRadiators = True
            self._oldSetPoint = self.radiators.setpoint

#First run check
        if self._hasWindows == True:
            self.windows.run_allwindowCheck()
            if self.windows.internalState == None:
                self.windows.internalState = self.internalState
        

    def _notify_observers(self, parameter, old_value, new_value):
        for callback in self._callbacks:
            callback(parameter, old_value, new_value)

    def register_callback(self, callback):
        self._callbacks.append(callback)

    #triggered by subclass' callbacks 
    def parameter_changed(self, parameter, old_value, new_value):
        if old_value != new_value:
            log.debug(f'thermo_thermostat {parameter} changed from {old_value} to {new_value}')

            if parameter == 'mode':
                #mode is changed by device
                self.internalMode = new_value

            elif parameter == 'window':
                self.start_stop_on_window_change(new_value)

#Run common actions and updates thermostat whith current values
    def runThermostat(self):
        #with no temperature skip to next thermostat
        if 'device' in dir(self):
            
            if self.device.ts.temperature == None:
                log.debug("Skipping cycle on thermostat: {} because temperature null".format(self.name))
                return
            
            #with no humidity value skip DewPoint calculation
            if 'relhumidity' in dir(self.device) != None:
                self.calculateDewPoint()

            #if there is an override, get the remaining time
            if f'{str(self.name)}_override' in self.tools.timers:
                overrideExpire = self.tools.getRemainingTimer(f'{str(self.name)}_override')
                self.utils.sendUpdateToItem(f'{self.name}_overrideExpire', overrideExpire)

            if self.internalManagement == self.states.internalManagements()["OFF"]:
                self.internalMode = self.states.internalModes()["OFF"]
                self.internalState = self.states.internalStates()["OFF"]
                self.run_hysteresis()
            else:
                self.checkAntiFreeze()

                if self.internalManagement == self.states.internalManagements()["MANUAL"]:
                    self.run_hysteresis()

                if self.internalManagement == self.states.internalManagements()["AUTO"]:
                    #load right setpoint from schedule settings
                    self.load_schedule()
                    self.run_hysteresis()

            self.set_actuators()
        
#Check if temperature is over or below anti freeeze
    def checkAntiFreeze(self):
        antiFreezeTemp = float(self.commons.antiFreezeTemp)
        if str(self.commons.season).lower() == "w" and self.device.ts.temperature <= antiFreezeTemp:
            log.debug("AntiFreeze check not passed for thermostat {}".format(self.name))
            self._oldInternalMode = self.internalMode
            self._oldInternalState = self.internalState
            self.internalMode = self.states.internalModes()["HEAT"]
            self.internalState = self.states.internalStates()["ANTIFREEZE"]
        else:
            log.debug("AntiFreeze check passed for thermostat {}".format(self.name))
            if self._oldInternalMode != None:
                self.internalMode = self._oldInternalMode
                self._oldInternalMode = None
            if self._oldInternalState != None:
                self.internalState = self._oldInternalState
                self._oldInternalState = None

#Stop and start thermostat on windows status
    def start_stop_on_window_change(self, new_value):
        log.info(f'sono in start_stop_on_window_change per il termostato {self.name} con self.internalState: {self.internalState} e new_value {new_value}')
        #se sono gia spento, o in antifreeze salto la prcedura
        if self.internalState == self.states.internalStates()["OFF"] or \
            self.internalState == self.states.internalStates()["ANTIFREEZE"]:
            return
        else:
            #Finestre chiuse => new_value = true
            log.info(f'Ho ricevuto un cambio di stato per le finestre del termostato {self.name} con valore {new_value}')
            #received from Windows class, new_value is False when almost a window is open
            #notify change also to main class (ThermoPlant) to apply thermostat logic
            if new_value == False:
                log.info("Almost a window is open for thermostat {} and InternalMode: {}".format(self.name, self.internalMode))
                #record the thermostat state at the moment that window is opened
                self.windows.internalState = self.internalState
                self.internalState = self.states.internalStates()["WINDOWSTOP"]
                if hasattr(self, '_hasFancoils') and self._hasFancoils:
                    self._oldFanSpeed = self.fancoils.internalSpeed

                if hasattr(self, '_hasRadiators') and self._hasRadiators:
                    self._oldSetPoint = self.radiators.setpoint
                    
            else:
                #restore prevoius status
                log.info("All windows are closed for thermostat {}, internalState to restore: {}".format(self.name, self.windows.internalState))

                if self.windows.internalState != None:
                    self.internalState = self.windows.internalState
                    self.windows.internalState = None

                    if hasattr(self, '_hasFancoils') and self._hasFancoils:
                        self.fancoils.internalSpeed = self._oldFanSpeed

                    if hasattr(self, '_hasRadiators') and self._hasRadiators:
                        self.radiators.setpoint = self._oldSetPoint

            #notify to trigger minute cycle immedialtely
            self._notify_observers("window", None, new_value)

    '''
    Check where ambient temperature is, regarding the setpoint
    '''
    def run_hysteresis(self):
        #stay in ANTIFREEZE or WINDOWSTOP if any
        if self.internalState == self.states.internalStates()["WINDOWSTOP"] or self.internalState == self.states.internalStates()["ANTIFREEZE"]:
            return

        #if temperature is between the range, do not change status
        delta = self.commons.deltaSetpoint
        if self.device.sp.setpoint != None:
            if self.internalMode == self.states.internalModes()["HEAT"]: #winter
                if self.device.ts.temperature > self.device.sp.setpoint + delta: 
                    self.internalState = self.states.internalStates()["OVER_HYSTERESIS"]
                    self._callHeatGenerator = False
                    self._callCoolGenerator = False
                elif self.device.ts.temperature < self.device.sp.setpoint - delta: 
                    self.internalState = self.states.internalStates()["BELOW_HYSTERESIS"]
                    self._callHeatGenerator = True
                    self._callCoolGenerator = False
                else:
                    self.internalState = self.states.internalStates()["INTO_HYSTERESIS"]

            elif self.internalMode == self.states.internalModes()["COOL"]: 
                if self.device.ts.temperature > self.device.sp.setpoint + delta: 
                    self.internalState = self.states.internalStates()["BELOW_HYSTERESIS"]
                    self._callCoolGenerator = True
                    self._callHeatGenerator = False
                elif self.device.ts.temperature < self.device.sp.setpoint - delta: 
                    self.internalState = self.states.internalStates()["OVER_HYSTERESIS"]
                    self._callCoolGenerator = False
                    self._callHeatGenerator = False
                else:
                    self.internalState = self.states.internalStates()["INTO_HYSTERESIS"]
            else:
                #off, dry, fan
                self.internalState = self.states.internalStates()["OFF"]
                self._callHeatGenerator = False
                self._callCoolGenerator = False

#Loads time schedules
    def load_schedule(self):
        schedule = json.loads(self.openhab.get_item(f'{self.name}_{self.commons.season.lower()}Schedule').state)
        # weekday 0 = Monday
        if len(schedule) > 0:
            weekday = datetime.datetime.today().weekday()
            todayTimeset = schedule[weekday]['timeSet']
            newSetpoint = None
            slotNr = len(todayTimeset)
            if slotNr > 0:
                for i in range(0, slotNr):
                    todConfig = todayTimeset[i]
                    h = int(todConfig['start'].split(':')[0])
                    m = int(todConfig['start'].split(':')[1])

                    #newSetpoint will be populated only if now is after the start of my timeSlot
                    if datetime.datetime.now().time() >= datetime.time(h, m):
                        newSetpoint = float(todConfig['tset'])

                if newSetpoint == None:
                    #means that now is before the first timeSlot of today. i need to retrieve the setpoint from the last timeSlot of yesterday
                    yesterday = weekday-1
                    #if weekday = 0 then yesterday = -1
                    if yesterday == -1:
                        yesterday = 6 #sunday

                    yesterdayTimeset = schedule[yesterday]['timeSet']
                    slotNr = len(yesterdayTimeset)
                    if slotNr > 0:
                        newSetpoint = float(yesterdayTimeset[slotNr-1]['tset'])

                if newSetpoint != None:
                    self.device.sp.updateOHsetpoint(newSetpoint, "OH")

#internalManagement
    @property
    def internalManagement(self):
        return float(self._internalManagement)

    @internalManagement.setter
    def internalManagement(self, new_value):
        new_value = float(new_value)
        old_value = self.internalManagement
        if old_value != new_value:
            self._internalManagement = new_value
            self.set_mode()
            #send callback to main class to instantly run every_minute cycle
            self._notify_observers("internalManagement", old_value, new_value)
    
    def internalManagement_changed(self, event):
        self.internalManagement = float(event.value)

    def set_mode(self):
        if self.internalManagement == self.states.internalManagements()["OFF"]:
            new_value = self.states.internalModes()["OFF"]

        if self.internalManagement == self.states.internalManagements()["MANUAL"]:
            if self.device.mode == 0.0:
                self.set_mode_upon_season()
                return
            else:
                new_value = self.device.mode

        if self.internalManagement == self.states.internalManagements()["AUTO"]:  
            #set right mode based on season value
            self.set_mode_upon_season()
            return
        
        self.utils.sendCommandToItem(f'{str(self.name)}_internalMode', new_value)

    def set_mode_upon_season(self):
        if str(self.commons.season).lower() == "w":
            new_value = self.states.internalModes()["HEAT"]
        else:
            new_value = self.states.internalModes()["COOL"]

        self.utils.sendCommandToItem(f'{str(self.name)}_internalMode', new_value)

    def _season_changed(self, event):
        #ritardo di 1s per assicurarsi che ThermoCommons.set_season abbia già aggiornato self.commons.season
        log.debug(f'Season changed to {event.value} for {self.name}: re-evaluating mode')
        self.run.countdown(1, self.set_mode).reset()


#internalMode
    @property
    def internalMode(self):
        return float(self._internalMode)

    @internalMode.setter
    def internalMode(self, new_value):
        new_value = float(new_value)
        old_value = self.internalMode
        if old_value != new_value:
            self._internalMode = float(new_value)
            self.utils.sendCommandToItem(f'{str(self.name)}_internalMode', new_value)
            self.set_management() 

    #Triggered by item value change
    def internalMode_changed(self, event):
        if event != None:
            new_value = float(event.value)
            self.internalMode = new_value
            #send mode to device
            self.device.set_mode(new_value)

    #Align management in OH reflecting device mode
    def set_management(self):
        if self.internalMode == self.states.internalModes()["OFF"]:
            retValue = self.states.internalManagements()["OFF"]
        else:
            if self.internalManagement == self.states.internalManagements()["MANUAL"] or \
               self.internalManagement == self.states.internalManagements()["AUTO"]:
                retValue = self.internalManagement #leave unchanged
            else:    
                retValue = self.states.internalManagements()["MANUAL"]
            
        self.utils.sendCommandToItem(f'{str(self.name)}_internalManagement', retValue)    

#internalState
    @property
    def internalState(self):
        return float(self._internalState)

    @internalState.setter
    def internalState(self, new_value):
        self._internalState = new_value
        
    #Triggered by item value change
    def internalState_changed(self, event):
        self.internalState = float(event.value)

#sSchedule
    @property
    def sSchedule(self):
        return self._sSchedule

    @sSchedule.setter
    def sSchedule(self, new_value):
        try:
            self._sSchedule = json.loads(new_value)

        except ValueError as e:
            self._sSchedule = []

    #Triggered by item value change
    def _sSchedule_changed(self, event):
        self.sSchedule = str(event.value)

#wShedule
    @property
    def wSchedule(self):
        return self._wSchedule

    @wSchedule.setter
    def wSchedule(self, new_value):
        try:
            self._wSchedule = json.loads(new_value)
        except ValueError as e:
            self._wSchedule = []

    #Triggered by item value change
    def _wSchedule_changed(self, event):
        self.wSchedule = str(event.value)

#tRanges
    @property
    def tRanges(self):
        return self._tRanges

    @tRanges.setter
    def tRanges(self, new_value):
        try:
            self._tRanges = json.loads(new_value)
        except ValueError as e:
            self.tRanges = {}

    #Triggered by item value change
    def tRanges_changed(self, event):
        self.tRanges = str(event.value)

    def override_changed(self, event):
        self.override = float(event.value)

    @property
    def override(self):
        return float(self._override)

    @override.setter
    def override(self, new_value):
        try:
            if new_value > 0:
                endTime =  float(new_value) * 3600
                self.tools.createTimer(f'{str(self.name)}_override', endTime, self.restore_after_override)
                #start countDown
                self.tools.startCountdown(f'{str(self.name)}_override')
                overrideExpire = self.tools.getRemainingTimer(f'{str(self.name)}_override')
            else:
                self.tools.cancelTimer(f'{str(self.name)}_override')
                overrideExpire = "0"

            self.utils.sendUpdateToItem(f'{self.name}_overrideExpire', overrideExpire)

        except ValueError as e:
            self.utils.sendCommandToItem(f'{str(self.name)}_override', 0)

    def restore_after_override(self):
        self.utils.sendCommandToItem(f'{str(self.name)}_internalManagement', self.states.internalManagements()["AUTO"])
        self.utils.sendCommandToItem(f'{str(self.name)}_override', 0)
        self.utils.sendUpdateToItem(f'{str(self.name)}_overrideExpire', "0")

#_dewPoint
    @property
    def dewPoint(self):
        return float(self._dewPoint)

    @dewPoint.setter
    def dewPoint(self, new_value):
        new_value = float(new_value)
        old_value = self.dewPoint
        if old_value != new_value:
            self._dewPoint = new_value
    
    #Triggered by item value change
    def dewPoint_changed(self, event):
        self.dewPoint = float(event.value)
    
    #Get dew point for thermostat
    def calculateDewPoint(self):
        """
            https://it.wikipedia.org/wiki/Punto_di_rugiada

            Dew Point formula: 
            Td = dew point temperature
            T = ambient temperature in Celsius
            H = relative humidity exposed in %

            Td = ((H/100) ^ (1/8)) * (112 + (0.9*T)) + (0.1*T) -112
        """
        if not self.device.hs.relhumidity == None:
            Td = ((self.device.hs.relhumidity/100.0) ** (1.0/8.0)) * (112.0 + (0.9 * self.device.ts.temperature)) + (0.1 * self.device.ts.temperature) - 112.0
            #self.set_dewPoint(round(Td, 2))
            self.utils.sendCommandToItem(f'{str(self.name)}_dewpoint', round(Td, 2))
            log.debug(f'{self.name}: Umidita: {self.device.hs.relhumidity}, Temperatura: {self.device.ts.temperature}, Dew point {self.dewPoint}')


#Load device
    '''
    Loads class representing my thermostat device, allowing my routine to properly work with correct device values
    a thermostat is mean a device that allow user to get at least temperature and is able to set setpoint
    '''
    def get_device(self):
        if 'model' in self.thConfig:
            self.model = str(self.thConfig['model']).lower()
        else:
            self.model = 'virtual'

        if self.model == 'mh8':
            from thermostats.mh8 import MH8
            myDevice = MH8(self.name, self.thConfig, self.commons, self.states, self.utils)
        elif self.model == 'mh7':
            from thermostats.mh7 import MH7
            myDevice = MH7(self.name, self.thConfig)
        elif self.model == 'mh5':
            from thermostats.mh5 import MH5
            myDevice = MH5(self.name, self.thConfig, self.commons, self.states, self.utils)
        elif self.model == 'ir2900':
            from thermostats.ir2900 import IR2900
            myDevice = IR2900(self.name, self.thConfig, self.commons, self.states, self.utils)
        elif self.model == "ztemp2":
            from thermostats.ztemp2 import ZTEMP2
            myDevice = ZTEMP2(self.name, self.thConfig)
        elif self.model == "lstat":
            from thermostats.lstat import LSTAT
            myDevice = LSTAT(self.name, self.thConfig)
        elif self.model == "bac_3000":
            from thermostats.bac_3000 import BAC_3000
            myDevice = BAC_3000(self.name, self.thConfig)
        elif self.model == "DANFOSS_014G0160":
            from thermostats.danfoss_014G0160 import DANFOSS_014G0160
            myDevice = DANFOSS_014G0160(self.name, self.thConfig)
        else: #even if model is defined as "virtual"
            from thermostats.virtual import Virtual
            myDevice = Virtual(self.name, self.thConfig, self.commons, self.states, self.utils)
        
        myDevice.register_callback(self.parameter_changed)
        self.device = myDevice

#Get thermostat's sensors
    def get_sensors(self):
        if 'windows' in self.thConfig:
            from thermostats.windows import Window
            w = Window(self.thConfig, self.commons)
            w.register_callback(self.parameter_changed)
            self.windows = w
        else:
            self.windows = None

#Get thermostat's actuators
    def get_actuators(self):
        if 'onoffvalves' in self.thConfig:
            from thermostats.onoff_valves import OnOffValve
            self.onoff_valves = OnOffValve(self.thConfig, self.commons)

        if 'analogvalves' in self.thConfig:
            from thermostats.analog_valves import AnalogValve
            self.analogValves = AnalogValve(self.thConfig, self.commons)

        if 'fancoils' in self.thConfig:
            from thermostats.fancoils import Fancoil
            self.fancoils = Fancoil(self.thConfig, self.commons)

        if 'radiators' in self.thConfig:
            from thermostats.radiators import Radiator
            self.radiators = Radiator(self.thConfig, self.commons)

#Set thermostat's actuators (valves, radiators, fancoils etc)
    def set_actuators(self):
        """ if self._hasOnoffvalves_cool:
            self.set_onoffValves2("cool")
        if self._hasOnoffvalves_heat:
            self.set_onoffValves2("heat") """

        if self._hasOnoffvalves_cool or self._hasOnoffvalves_heat:
            self.set_onoffValves()
        if self._hasAnalogvalves:
            self.set_analogValves()
        if self._hasFancoils:
            self.set_fancoils()
        if self._hasRadiators:
            self.set_radiators()

    """
        Actuate onoff valves
        if thermostat is off => valves are off
        else valves are open following the histeresys logic.
    """
    def set_onoffValves2(self, which):
        #DA RIVEDERE: fa fare on/off alla valvola opposta alla modalità (es. se cool fa fare on/off alla heat)
        myItem = str(f'{self.name}_onoffvalves_{which}')
        currentGroupState = self.openhab.get_item(myItem).state
        if self.internalMode == 0.0:
            if currentGroupState != 'OFF': self.utils.sendCommandToItem(myItem, 'OFF')
        else:
            #HEAT
            if self.internalMode == 1.0:
                #be sure to turn off cool valves, if any and if aren't off already
                if self._hasOnoffvalves_cool:
                    newState = 'OFF'
                    otherGroupName = str(f'{self.name}_onoffvalves_cool')
                    currentOtherGropState = self.openhab.get_item(otherGroupName).state
                    if newState != currentOtherGropState: self.utils.sendCommandToItem(otherGroupName, newState)
            #COOL
            elif self.internalMode == 2.0:
                #be sure to turn off heat valves, if any and if aren't off already
                if self._hasOnoffvalves_heat:
                    newState = 'OFF'
                    otherGroupName = str(f'{self.name}_onoffvalves_heat')
                    currentOtherGropState = self.openhab.get_item(otherGroupName).state
                    if newState != currentOtherGropState: self.utils.sendCommandToItem(otherGroupName, newState)
            else:
                #DRY AND FAN MODE ARE NOT MANAGED
                if currentGroupState != 'OFF': self.utils.sendCommandToItem(myItem, 'OFF')
                return

            if self.internalState == self.states.internalStates()["OFF"]:
                if currentGroupState != 'OFF': self.utils.sendCommandToItem(myItem, 'OFF')

            elif self.internalState == self.states.internalStates()["BELOW_HYSTERESIS"]:
                newState = 'ON'
                if newState != currentGroupState: self.utils.sendCommandToItem(str(f'{self.name}_onoffvalves_{which}'), newState)
            #elif self.internalState == self.states.internalStates()["INTO_HYSTERESIS"]: no nothing
            elif self.internalState == self.states.internalStates()["OVER_HYSTERESIS"]:
                newState = 'OFF'
                if newState != currentGroupState: self.utils.sendCommandToItem(str(f'{self.name}_onoffvalves_{which}'), newState)
            elif self.internalState == self.states.internalStates()["ANTIFREEZE"]:
                newState = 'ON'
                if newState != currentGroupState: self.utils.sendCommandToItem(str(f'{self.name}_onoffvalves_{which}'), newState)
            elif self.internalState == self.states.internalStates()["WINDOWSTOP"]:
                newState = 'OFF'
                if newState != currentGroupState: self.utils.sendCommandToItem(str(f'{self.name}_onoffvalves_{which}'), newState)

    def set_onoffValves(self):

        #currentCoolGroupState = None
        #currentHeatGroupState = None

        #if self._hasOnoffvalves_cool: currentCoolGroupState = self.openhab.get_item(str(f'{self.name}_onoffvalves_cool')).state
        #if self._hasOnoffvalves_heat: currentHeatGroupState = self.openhab.get_item(str(f'{self.name}_onoffvalves_heat')).state


        if self.internalMode == 0.0:
            newState = 'OFF'
            if self._hasOnoffvalves_cool: self.utils.sendCommandToItem(str(f'{self.name}_onoffvalves_cool'), newState)
            if self._hasOnoffvalves_heat: self.utils.sendCommandToItem(str(f'{self.name}_onoffvalves_heat'), newState)
        else:
            #HEAT
            if self.internalMode == 1.0:
                valveType = 'heat'
                #currentGroupState = currentHeatGroupState
            #COOL
            elif self.internalMode == 2.0:
                valveType = 'cool'
                #currentGroupState = currentCoolGroupState
            else:
            #DRY AND FAN MODE ARE NOT MANAGED
                newState = 'OFF'
                if self._hasOnoffvalves_cool: self.utils.sendCommandToItem(str(f'{self.name}_onoffvalves_cool'), newState)
                if self._hasOnoffvalves_heat: self.utils.sendCommandToItem(str(f'{self.name}_onoffvalves_heat'), newState)
                return

            if self.internalState == self.states.internalStates()["OFF"]:
                newState = 'OFF'
                #if self._hasOnoffvalves_cool and newState != currentCoolGroupState: self.utils.sendCommandToItem(str(f'{self.name}_onoffvalves_cool'), newState)
                #if self._hasOnoffvalves_heat and newState != currentHeatGroupState: self.utils.sendCommandToItem(str(f'{self.name}_onoffvalves_heat'), newState)
                if self._hasOnoffvalves_cool: self.utils.sendCommandToItem(str(f'{self.name}_onoffvalves_cool'), newState)
                if self._hasOnoffvalves_heat: self.utils.sendCommandToItem(str(f'{self.name}_onoffvalves_heat'), newState)
            else:
                if self.internalState == self.states.internalStates()["BELOW_HYSTERESIS"]:
                    newState = 'ON'
                    #if currentGroupState != None and newState != currentGroupState: self.utils.sendCommandToItem(str(f'{self.name}_onoffvalves_{valveType}'), newState)
                #elif self.internalState == self.states.internalStates()["INTO_HYSTERESIS"]: #do nothing
                elif self.internalState == self.states.internalStates()["OVER_HYSTERESIS"]:
                    newState = 'OFF'
                    #if currentGroupState != None and newState != currentGroupState: self.utils.sendCommandToItem(str(f'{self.name}_onoffvalves_{valveType}'), newState)
                elif self.internalState == self.states.internalStates()["ANTIFREEZE"]:
                    newState = 'ON'
                    #if newState != currentGroupState: self.utils.sendCommandToItem(str(f'{self.name}_onoffvalves_{valveType}'), newState)
                elif self.internalState == self.states.internalStates()["WINDOWSTOP"]:
                    newState = 'OFF'
                    #if currentGroupState != None and newState != currentGroupState: self.utils.sendCommandToItem(str(f'{self.name}_onoffvalves_{valveType}'), newState)
                else:
                    newState = 'OFF'

                #se per questo termostato non esiste il gruppo valvole del modo corrente
                #(es. impianto solo riscaldamento mentre siamo in mode cool), non c'è nulla da fare
                has_valves = self._hasOnoffvalves_cool if valveType == 'cool' else self._hasOnoffvalves_heat
                if not has_valves:
                    return

                valves = self.openhab.get_item(str(f'{self.name}_onoffvalves_{valveType}')).members
                for valve in valves:
                    self.utils.sendCommandToItem(valve.name, newState)

    """
        Actuate analog valves
        if thermostat is off => valve closed
        else valve is proportionally opened with 100% at delta between temperature and setpoint >= 3 °C
        on setpoint reached, valve is opened at 10%
    """
    def set_analogValves(self):
        if self.internalMode == 0.0:
            value = 0
        else:
            if self.internalState == self.states.internalStates()["OFF"]:
                value = 0
            elif self.internalState == self.states.internalStates()["BELOW_HYSTERESIS"] or self.internalState == self.states.internalStates()["INTO_HYSTERESIS"]:
                absVal = abs(self.device.ts.temperature - self.device.sp.setpoint)
                if absVal != 0:
                    value = 100 / (3 / absVal)
                    if value > 100:
                        value = 100
                else:
                    value = 10
            elif self.internalState == self.states.internalStates()["OVER_HYSTERESIS"]:
                value = 5
            elif self.internalState == self.states.internalStates()["ANTIFREEZE"]:
                value = 100
            elif self.internalState == self.states.internalStates()["WINDOWSTOP"]:
                value = 5

        currentGroupState = self.openhab.get_item(str(f'{self.name}_analogvalves')).state

        if value != currentGroupState: 
            self.utils.sendCommandToItem(str(f'{self.name}_analogvalves'), round(value, 2))

    """
        Actuate fancoils
        if thermostat is off => fancoil is off
        else speed is proportionally higher with 3 at delta between temperature and setpoint >= 3 °C
        on setpoint reached, speed is 1

        [nome termostato]_fancoils_internalSpeed = velocità impostata dall'utente bassa, media alta, auto)
        [nome termostato]_fancoils_speed = velocità delle ventole fancoil. se l'utente imposta bassa, media, alta allora è uguale ad internalspeed, se auto prenderà un valore dipendente dal delta T mentre internalSpeed rimane su auto
    """
    def set_fancoils(self):
        if self.internalMode == 0.0:
            value = 0.0
        else:
            if self.internalState == self.states.internalStates()["OFF"]:
                value = 0.0
            elif self.internalState == self.states.internalStates()["BELOW_HYSTERESIS"] or self.internalState == self.states.internalStates()["INTO_HYSTERESIS"]:
                if self.fancoils.internalSpeed == self.states.internalFanSpeeds()["AUTO"]:
                    absVal = abs(self.device.ts.temperature - self.device.sp.setpoint)
                    if absVal != 0:
                        value = 1.0
                        if absVal > 1.75:
                            value = 2.0
                        if absVal > 2.75:
                            value = 3.0
                    else:
                        value = 1.0
                else:
                    value = self.fancoils.internalSpeed

            elif self.internalState == self.states.internalStates()["OVER_HYSTERESIS"]:
                value = 0.0
            elif self.internalState == self.states.internalStates()["ANTIFREEZE"]:
                value = 3.0
            elif self.internalState == self.states.internalStates()["WINDOWSTOP"]:
                value = 0.0
                
        currentGroupState = self.openhab.get_item(str(f'{self.name}_fancoils_speed')).state

        log.debug(f'set {self.name} speed from {currentGroupState} to {value}')

        if currentGroupState == 'UNDEF' or value != currentGroupState:
            self.utils.sendCommandToItem(str(f'{self.name}_fancoils_speed'), value)
    
    def set_radiators(self):
        log.debug(f'Imposto i radiatori per {self.name} con internalMode: {self.internalMode} ed internalState: {self.internalState}')
        #pass to generic radiator class, mode and setpoint
        if self.internalMode == 0.0:
            self.radiators.set_mode(self.internalMode)
        else:
            if self.internalState == self.states.internalStates()["OFF"]:
                self.radiators.set_mode(self.states.internalModes()["OFF"])
            elif self.internalState == self.states.internalStates()["BELOW_HYSTERESIS"] or self.internalState == self.states.internalStates()["INTO_HYSTERESIS"] or self.internalState == self.states.internalStates()["OVER_HYSTERESIS"]:
                self.radiators.set_mode(self.states.internalModes()["HEAT"])
            elif self.internalState == self.states.internalStates()["ANTIFREEZE"]:
                self.radiators.set_mode(self.states.internalModes()["HEAT"])
            elif self.internalState == self.states.internalStates()["WINDOWSTOP"]:
                #self.radiators.set_mode(self.states.internalModes()["OFF"])
                #provo ad abbassare il setpoint invece che spegnere così risparmio batteria
                self.radiators.set_setpoint(16.0)
                return

            self.radiators.set_setpoint(self.device.sp.setpoint)

    '''
    This function is used by user interface widget "widget_termostato" to update a thermostat schedule
    '''
    def thermoSetupTemp_updated(self, event):
        log.debug(f'thermoSetupTemp_updated: {event.name} + {event.value}')

        if str(event.value) != '' and not 'undefined' in str(event.value):
            parameters = str(event.value).split(';')
            myFunction = parameters[0]
            thermostat = parameters[1]
            selectedSeason = str(parameters[2]).lower()

            schedule = json.loads(self.openhab.get_item(f'{thermostat}_{selectedSeason.lower()}Schedule').state)
            log.debug(f'schedule: {schedule}')

            if myFunction == 'add':
                selectedDays = list(parameters[3])
                setPoint = parameters[4]
                startTime = parameters[5]

                if len(schedule) > 0:
                    startTimeH = int(startTime.split(':')[0])
                    startTimeM = int(startTime.split(':')[1])
                    slotPosition = 0
                    newSlot = {"start": startTime, "tset": float(setPoint)}
                    for day in selectedDays:
                        day = int(day)
                        todayTimeset = schedule[day]['timeSet']
                        slotNr = len(todayTimeset)
                        for i in range(0, slotNr):
                            todConfig = todayTimeset[i]
                            h = int(todConfig['start'].split(':')[0])
                            m = int(todConfig['start'].split(':')[1])

                            #slotPosition will be incremented only if startTimeH,startTimeM is after the start of an already existing timeSlot
                            if datetime.time(startTimeH, startTimeM) == datetime.time(h, m):
                                return
                            
                            if datetime.time(startTimeH, startTimeM) > datetime.time(h, m):
                                slotPosition = i+1
                            
                            #no h,m found means this slot is before the first timeSlot of today.
                            #so slotPosition wil still be 0

                        todayTimeset.insert(slotPosition, newSlot)

            
            if myFunction == 'del':
                slot = int(parameters[3])
                day = int(parameters[4])
                todayTimeset = schedule[day]['timeSet']
                if len(schedule) > 0:
                    schedule[day]['timeSet'].pop(slot)

            #add or remove, now my schedule is ok, so update item
            schedule = json.dumps(schedule)
            self.openhab.post_update(f'{thermostat}_{selectedSeason.lower()}Schedule', schedule)
