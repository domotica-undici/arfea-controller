'''
    Analog valve object
    This class manages 0/10 valves belonging to a thermostat
    Used when physical or virtual thermosat has valves to manage

    About the items:
    Define the name of the item(s) in thermo.yml
        example thermo.yml: 
            - name: TermostatoMatrimoniale
              model: mh8
              ....
              analogvalves:
                - valve1
                - valve2
    
    then habapp will
      1) create a group to manage all valves related to a thermostat at once
      2) create an item for each valve defined.
      
    YOU MUST: bind the item to the channel of the proper Thing
'''

import logging
log = logging.getLogger('HABApp')

import HABApp
from thermostats.utils import THUtils

class AnalogValve(HABApp.Rule):
    def __init__(self, thConfig=None, commons=None):
        super().__init__()
        
        self.thutils = THUtils()

        self.name = str(thConfig['name'])

        if 'analogvalves' in thConfig:
            #this group manages all analogvalves in thermostat
            self.thutils.create_item('analogvalvesGroup', str(f'{self.name}_analogvalves'), ['analogvalves', str(thConfig['ambient'])])

            for valve in thConfig['analogvalves']:
                #this item is the analog valve 
                #this will bind the valve to the thermostat valves's group
                self.thutils.create_item('analogvalve', str(valve['name']), [str(f'{self.name}_analogvalves'), str(thConfig['ambient'])], str(valve['label']))