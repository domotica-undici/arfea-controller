import logging
log = logging.getLogger('HABApp')

import HABApp

class States(HABApp.Rule):
    def __init__(self):
        super().__init__()

    '''
    This is how user want thermostat to be:
    OFF = Absolutely off
    MANUAL = managed manually (don't care about season or else)
    AUTO = based on scheduled and auto set mode basing on season
    is "internal" because will further properly set up relative devices
    '''
    def internalManagements(self):
        return {"OFF": 0.0, "MANUAL": 1.0, "AUTO": 2.0}

    '''
    This is how the thermostat must work
    is "internal" because will further properly set up relative devices
    '''
    def internalModes(self):
        return {"OFF": 0.0, "HEAT": 1.0, "COOL": 2.0, "AUTO": 3.0, "FAN": 6.0, "DRY": 8.0, "HEAT_ECONOMY": 11.0, "COOL_ECONOMY": 12.0, "AWAY": 13.0}

    '''
    This is to properly override or follow user settings. 
    '''
    def internalStates(self):
        return {
            "OFF": 0.0,
            "BELOW_HYSTERESIS": 1.0,
            "INTO_HYSTERESIS": 3.0,
            "OVER_HYSTERESIS": 5.0,
            "ANTIFREEZE": 7.0,
            "WINDOWSTOP": 13.0
            }

    '''
    currently not used
    '''
    def internalWorkingStates(self):
        return {
            "IDLE": 0.0,
            "HEATING": 1.0,
            "COOLING": 2.0,
            "DRYING": 3.0,
            "FAN": 4.0,
            "PENDING_HEAT": 5.0,
            "PENDING_COOL": 6.0,
            "ECON": 7.0
            }

    def internalFanSpeeds(self):
        return {"OFF": 0.0, "LOW": 1.0, "MID": 2.0, "HIGH": 3.0, "AUTO": 4.0, "MIDLOW": 5.0, "MIDHIGH": 6.0}

class THUtils(HABApp.Rule):
    def __init__(self):
        super().__init__()

    '''
    Get openHAB item and binds it to a function triggered on state change
    based on variable createIfNotExist:
        if true = create item and set default value
        if false do not create item and return None
    '''
    def bindItem(self, itemName, function=None, event=None, defaultValue=None):
        myItem = self.openhab.get_item(itemName)
        self.listen_event(itemName, function, event)
        ret = None
        if (myItem.state == None or myItem.state == 'NULL') and defaultValue != None:
            ret = defaultValue
            if self.openhab.item_exists(itemName):
                self.sendUpdateToItem(itemName, defaultValue)
        else:
            ret = myItem.state
        return ret


    def create_item(self, createType, itemName, groupName, label=None):
        # Skip creation if the primary item is already defined (e.g. in a textual .items file).
        # OpenHAB returns 405 on PUT /rest/items/<name> for provider-owned items and aborts the rule.
        if self.openhab.item_exists(itemName):
            return
        if label == None:
            label = itemName
        if createType == "thGroup":
            #HVAC Thermostat item. TAGS Is empty to not show thermostats OH interface CARDS.
            self.openhab.create_item("Group", itemName, label=label, category='', tags=[], groups=['gPersistence', 'gThermostats', groupName], group_function_params=[])
            self.openhab.set_metadata(itemName, "alexa", "Thermostat", {})
            #Thermostat management (off, man, auto)
            self.openhab.create_item("Number", f'{itemName}_internalManagement', label='Gestione', tags=[], groups=['gPersistence', itemName])
            #Thermostat mode (off, heat, cool...)
            self.openhab.create_item('Number', f'{itemName}_internalMode', label='Funzionamento', tags=[], groups=['gPersistence', itemName])
            self.openhab.set_metadata(f'{itemName}_internalMode', "alexa", "Thermostat.HeatingCoolingMode", {"OFF": "0.0","HEAT": "1.0","COOL": "2.0"})
            #Thermostat working state (off, heating, pending_XX...)
            self.openhab.create_item('Number', f'{itemName}_internalState', label='stato', tags=[], groups=[itemName])
            #Season schedule
            '''
            Winter and summer schedule for chrono setup. format is setpoint divided in quarter of hour starting from midnight:
            [{
                "sun": [20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20;20],
                "mon": [20 ...
                ....
                "sat": [20 ...
            }]

            [
                {"day": 0, "timeSet": [{"start": "10:00", "tset": 16.0}, {"start": "12:00", "tset": 24.0}, {"start": "14:00", "tset": 25.0}, {"start": "16:00", "tset": 26.0}, {"start": "18:00", "tset": 28.0}]},
                {"day": 1, "timeSet": [{"start": "10:00", "tset": 16.0}, {"start": "12:00", "tset": 24.0}, {"start": "14:00", "tset": 25.0}, {"start": "16:00", "tset": 26.0}, {"start": "18:00", "tset": 28.0}]},
                {"day": 2, "timeSet": [{"start": "10:00", "tset": 16.0}, {"start": "12:00", "tset": 24.0}, {"start": "14:00", "tset": 25.0}, {"start": "16:00", "tset": 26.0}, {"start": "18:00", "tset": 28.0}]},
                {"day": 3, "timeSet": [{"start": "10:00", "tset": 16.0}, {"start": "12:00", "tset": 24.0}, {"start": "14:00", "tset": 25.0}, {"start": "16:00", "tset": 26.0}, {"start": "18:00", "tset": 28.0}]},
                {"day": 4, "timeSet": [{"start": "10:00", "tset": 16.0}, {"start": "12:00", "tset": 24.0}, {"start": "14:00", "tset": 25.0}, {"start": "16:00", "tset": 26.0}, {"start": "18:00", "tset": 28.0}]},
                {"day": 5, "timeSet": [{"start": "10:00", "tset": 16.0}, {"start": "12:00", "tset": 24.0}, {"start": "14:00", "tset": 25.0}, {"start": "16:00", "tset": 26.0}, {"start": "18:00", "tset": 28.0}]},
                {"day": 6, "timeSet": [{"start": "10:00", "tset": 16.0}, {"start": "12:00", "tset": 24.0}, {"start": "14:00", "tset": 25.0}, {"start": "16:00", "tset": 26.0}, {"start": "18:00", "tset": 28.0}]}
            ]
            '''
            self.openhab.create_item('String', f'{itemName}_sSchedule', label='Pianificazione estiva', tags=[], groups=["gPersistence", itemName])
            self.openhab.create_item('String', f'{itemName}_wSchedule', label='Pianificazione invernale', tags=[], groups=["gPersistence", itemName])
            '''
            Temperature ranges: define min e max tempeature for both winter and summer. format is:
            [ wintermin, wintermax, summermin, summermax ]
            '''
            self.openhab.create_item('String', f'{itemName}_tRanges', label='Temperature limite', groups=["gPersistence", itemName])
            self.openhab.create_item('Number', f'{itemName}_setpoint', label='Setpoint', tags=['Setpoint', 'Temperature'], groups=["gPersistence", "gThermoSetpoint", itemName])
            self.openhab.set_metadata(f'{itemName}_setpoint', "alexa", "Thermostat.TargetTemperature", {'Scale': 'Celsius'})
            self.openhab.set_metadata(f'{itemName}_setpoint', "stateDescription", "displayState", {'pattern': '%.1f °C'})

            self.openhab.create_item('Number', f'{itemName}_dewpoint', label='Punto di rugiada', tags=['Status', 'Temperature'], groups=[itemName])
            self.openhab.set_metadata(f'{itemName}_dewpoint', "stateDescription", "displayState", {'pattern': '%.1f °C'})

            self.openhab.create_item('Number', f'{itemName}_override', label='Gestione prioritaria', tags=[], groups=["gPersistence", itemName])
            self.openhab.create_item('String', f'{itemName}_overrideExpire', label='Scadenza gestione prioritaria', tags=[], groups=[itemName])

        if createType == "windowsGroup":
            self.openhab.create_item('Group', itemName, label=label, category='window', tags=['Window'], groups=groupName, group_type='Contact', group_function='OR', group_function_params=['OPEN', 'CLOSED'])
            
        if createType == "window":
            self.openhab.create_item('Contact', itemName, label=f'{itemName}', category='window', tags=['Status'], groups=groupName)
            self.openhab.set_metadata(itemName, "stateDescription", "displayState", {'pattern': '%s'})
            self.openhab.post_update(itemName, 'CLOSED')

        if createType == "onoffvalvesGroup":
            self.openhab.create_item('Group', itemName, label=label, category='sani_valve_50', tags=['Status', 'Valve'], groups=groupName, group_type='Switch', group_function='AND', group_function_params=['ON', 'OFF'])
            self.openhab.set_metadata(itemName, "stateDescription", "displayState", {'pattern': '%s'})

        if createType == "onoffvalve":
            self.openhab.create_item('Switch', itemName, label=f'{label}', category='sani_valve_50', tags=['Status'], groups=groupName)
            self.openhab.set_metadata(itemName, "stateDescription", "displayState", {'pattern': '%s'})

        if createType == "analogvalvesGroup":
            self.openhab.create_item('Group', itemName, label=f'{label}', category='sani_valve_50', tags=['OpenLevel'], groups=groupName, group_type='Number', group_function='MAX')
            self.openhab.set_metadata(itemName, "stateDescription", "displayState", {'pattern': '%d %%'})

        if createType == "analogvalve":
            self.openhab.create_item('Number', itemName, label=f'{label}', category='sani_valve_50', tags=['OpenLevel'], groups=groupName)
            self.openhab.set_metadata(itemName, "stateDescription", "displayState", {'pattern': '%d %%'})
            
        if createType == "fancoilGroup":
            self.openhab.create_item('Group', itemName, label=label, category='Fan', tags=['Fan'], groups=groupName, group_type='Number') #, group_function='MIN'
            self.openhab.create_item('Number', f'{itemName}_speed', label=f'Velocità {itemName}', category='Fan', tags=[], groups=[itemName])
            self.openhab.set_metadata(f'{itemName}_speed', "stateDescription", "displayState", {'pattern': '%d'})
            self.openhab.create_item("Number", f'{itemName}_internalSpeed', label='Gestione interna velocità', tags=[], groups=[itemName])
            self.openhab.set_metadata(f'{itemName}_internalSpeed', "stateDescription", "displayState", {'pattern': '%d'})

        if createType == "fancoil":
            self.openhab.create_item('Group', itemName, label=label, category='Fan', tags=[], groups=groupName, group_type='Number') #, group_function='MIN'
            self.openhab.create_item('Switch', f'{itemName}_V1', label=f'{itemName} V1', category='', tags=[], groups=[itemName])
            self.openhab.create_item('Switch', f'{itemName}_V2', label=f'{itemName} V2', category='', tags=[], groups=[itemName])
            self.openhab.create_item('Switch', f'{itemName}_V3', label=f'{itemName} V3', category='', tags=[], groups=[itemName])

        if createType == "radiatorsGroup":
            self.openhab.create_item('Group', itemName, label=label, category='radiator', tags=['RadiatorControl'], groups=groupName)
        
        if createType == "radiator":
            self.openhab.create_item('Group', itemName, label=label, category='radiator', tags=['RadiatorControl'], groups=groupName)

        if createType == "temperature_sensor":
            self.openhab.create_item('Number', itemName, label='Sensore temperatura', tags=['Measurement', 'Temperature'], groups=groupName)
            self.openhab.set_metadata(itemName, "alexa", "Thermostat.CurrentTemperature", {'Scale': 'Celsius'})
            self.openhab.set_metadata(itemName, "stateDescription", "displayState", {'pattern': '%.1f °C'})

        if createType == "humidity_sensor":
            self.openhab.create_item('Number', itemName, label='Sensore umidità', tags=['Measurement', 'Humidity'], groups=groupName)
            self.openhab.set_metadata(itemName, "alexa", "Thermostat.CurrentHumidity", {})
            self.openhab.set_metadata(itemName, "stateDescription", "displayState", {'pattern': '%d %%'})

