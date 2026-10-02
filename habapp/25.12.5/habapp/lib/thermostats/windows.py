'''
    Window object
    This class manage the group of windows belonging to a thermostat
    Used to stop thermoregulation when almost a window is open for over windowStopThermo seconds

    The procedure CREATES a window group bound to thermostat
    About the windows:
        1) if there is a device with the THING named the same as described in thermo.yml and the THING 
            is defined into config/[device].yml-> habapp has already created items and bound them to the device's channel
            example thermo.yml: 
              - name: TermostatoMatrimoniale
                model: mh8
                ...
                windows:
                  - finestraMatrimoniale
        OR
        2) if there is not a physycal device and/or the item named the same as described in thermo.yml -> procedure create the item

'''

import logging

log = logging.getLogger('HABApp')

import HABApp
from HABApp.core.events import ValueChangeEventFilter
from thermostats.thermo_commons import ThermoCommons
from thermostats.utils import THUtils
from system.utils import Utils

class Window(HABApp.Rule):
    def __init__(self, thConfig=None, commons=None):
        super().__init__()

        self._callbacks = []

        self.utils = Utils()
        self.thutils = THUtils()
        self.commons = ThermoCommons()
        self.name = str(thConfig['name'])
        self.tools = self.get_rule('Tools') #load with this syntax otherwise timers won't works

        # windowPass is False AFTER timer dalay and will set thermostat OFF
        self.windowPass = True

        #used to store thermostat status before stop it because of window open
        self.internalMode = None
        self.internalState = None
    
        '''
        Windows for thermostat. Used to stop climating when one or more windows is opened over defined time
        '''
        if 'windows' in thConfig:
            #Create windows group
            self.thutils.create_item('windowsGroup', str(f'{self.name}_windows'), ['gDoorWindowSensors'])
            #now check the state
            itemName = f'{str(self.name)}_windows'
            #bind to my group to trigger changes
            self.utils.bindItem(
                            itemName, 
                            self.windowsGroup_state_changed, 
                            ValueChangeEventFilter())
            #add windows items: create if not exist
            for window in thConfig['windows']:
                #this will bind the window to the thermostat window's group
                self.thutils.create_item('window', str(window), [itemName, str(thConfig['ambient'])])

            self._gotWindows = True
            self.tools.createTimer(f'{str(self.name)}_windows', self.commons.windowStopThermo, self.stop_thermo_on_window_open)
        else:
            self._gotWindows = False

    def _notify_observers(self, parameter,old_value, new_value):
        for callback in self._callbacks:
            callback(parameter, old_value, new_value)

    def register_callback(self, callback):
        self._callbacks.append(callback)
    
    #triggered by subclass' callbacks 
    def parameter_changed(self, parameter, old_value, new_value):
        return

    #Triggered by group item value change
    def windowsGroup_state_changed(self, event):
        self.run_windowCheck(event.value)

#WindowPass
    @property
    def windowPass(self):
        return self._windowPass

    @windowPass.setter
    def windowPass(self, new_value):
        self._windowPass = new_value

#internalMode
    @property
    def internalMode(self):
        return self._internalMode

    @internalMode.setter
    def internalMode(self, new_value):
        self._internalMode = new_value

#internalState
    @property
    def internalState(self):
        return self._internalState

    @internalState.setter
    def internalState(self, new_value):
        self._internalState = new_value


    def run_allwindowCheck(self):
        myItem = self.openhab.get_item(f'{str(self.name)}_windows')
        self.run_windowCheck(myItem.state)
            

    def run_windowCheck(self, value):
        if value == None or value == 'NULL' or str(value).upper() == 'CLOSED':
            #no open windows
            self.windowPass = True
            self._notify_observers("window", None, self.windowPass)
        else:
            self.windowPass = False
            self.tools.startCountdown(f'{str(self.name)}_windows')
        
    def stop_thermo_on_window_open(self):
        self._notify_observers("window", None, self.windowPass)
        