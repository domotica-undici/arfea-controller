'''
    OnOff Valve object
    This class manages heating and cooling on/off valves belonging to a thermostat
    Used when physical or virtual thermosat has no actuators on board
'''

'''
    OnOff valve object
    This class manages ON/OFF valves belonging to a thermostat
    Used when physical or virtual thermosat has valves to manage

    About the items:
    Define the name of the item(s) in thermo.yml
        example thermo.yml: 
            - name: TermostatoMatrimoniale
              model: mh8
              ....
              onoffvalves:
                heat:
                  - valvolaMatrimionialeCaldo
                cool:
                  - valvolaMatrimionialeFreddo
    
    then habapp will
      1) create a group for heat and one for cool to manage all valves related to a thermostat at once
      2) create an item for each valve defined.
      
    YOU MUST: bind the item to the channel of the proper Thing
'''

import logging
log = logging.getLogger('HABApp')

import HABApp
from thermostats.utils import THUtils

class OnOffValve(HABApp.Rule):
    def __init__(self, thConfig=None, commons=None):
        super().__init__()

        self.thutils = THUtils()

        self.name = str(thConfig['name'])

        '''
        Heating and cooling ON/OFF Valves, divided by two respectives groups
        '''
        if 'onoffvalves' in thConfig:
            groups = [str(thConfig['ambient'])]

            if 'cool' in thConfig['onoffvalves']:
              groups.append(str(f'{self.name}_onoffvalves_cool'))
              
            if 'heat' in thConfig['onoffvalves']:
              groups.append(str(f'{self.name}_onoffvalves_heat'))


            if 'cool' in thConfig['onoffvalves']:
                label='Valvole raffrescamento'
                self.thutils.create_item('onoffvalvesGroup', str(f'{self.name}_onoffvalves_cool'), ['onoffvalves_cool', str(thConfig['ambient'])], label)

                for valve in thConfig['onoffvalves']['cool']:
                    #this will bind the valve to the thermostat valves's group
                    if 'label' not in valve:
                        valve['label'] = valve['name']
                    self.thutils.create_item('onoffvalve', str(valve['name']), groups, str(valve['label']))

            if 'heat' in thConfig['onoffvalves']:
                label='Valvole riscaldamento'
                self.thutils.create_item('onoffvalvesGroup', str(f'{self.name}_onoffvalves_heat'), ['onoffvalves_heat', str(thConfig['ambient'])], label)

                for valve in thConfig['onoffvalves']['heat']:
                    #this will bind the valve to the thermostat valves's group
                    if 'label' not in valve:
                      valve['label'] = valve['name']
                    self.thutils.create_item('onoffvalve', str(valve['name']), groups, str(valve['label']))
