'''
    HEATIT Z-TRM6 object
    This class manage an HEATIT Z-TRM6 thermostat

    E' un termostato ALIMENTATO A 230V con RELE' A BORDO (16A, max 3600W): il carico
    (cavo scaldante, radiante elettrico, contattore, valvola di zona) e' collegato al device,
    che lo comanda da solo con il proprio setpoint. L'impianto ARFEA non ha quindi un attuatore
    da pilotare: gli passa modo e setpoint e lo spegne quando la logica dice di fermarsi ma il
    setpoint non basta a dirlo, cioe' a finestra aperta (vedi HeatitZWave.apply_internalState).
    Se al termostato servono anche valvole esterne, si dichiarano come su ogni altro modello
    (onoffvalves/analogvalves in thermo.yml) e vengono comandate dalla logica dell'impianto.

    Sensori: interno, esterno cablato, pavimento. Quale usare per la regolazione lo decide il
    parametro 2 del device ("sensor mode"); la stessa scelta va detta all'impianto con la
    chiave 'sensor' (default: interno).

    About the items:
        1) if there is a device with the THING named the same as described in thermo.yml and the THING
            is defined into config/thing_ztrm6.yml-> habapp has already created items and bound them to the device's channel
            example thermo.yml:
              - name: TermostatoMatrimoniale
                model: ztrm6
                ambient: matrimoniale
                sensor: floor          #internal (default), external, floor
        OR
        2) if there is not a physycal device this class should not be called and no ztrm6 defined in thermo.yml

    Channel del binding zwave -> item attesi (thing type thermofloor_trm6_00_000):
        sensor_temperature2             -> [nome]_temperature            (sensore interno)
        sensor_temperature3             -> [nome]_temperature_external   (sensore esterno cablato)
        sensor_temperature4             -> [nome]_temperature_floor      (sensore a pavimento)
        thermostat_mode1                -> [nome]_mode
        thermostat_state1               -> [nome]_operatingstate
        thermostat_setpoint_heating1    -> [nome]_setpoint_heating
        thermostat_setpoint_cooling1    -> [nome]_setpoint_cooling
        meter_watts1                    -> [nome]_power
        meter_kwh1                      -> [nome]_energy
        alarm_heat1                     -> [nome]_alarm_heat             (sovratemperatura)
        alarm_power1                    -> [nome]_alarm_power            (sovraccarico)

    Gli item opzionali (sensori aggiuntivi, allarmi) vengono agganciati solo se esistono gia':
    se il sensore a pavimento non e' cablato non ha senso avere l'item.
'''

import logging
log = logging.getLogger('HABApp')

import HABApp
from HABApp.core.events import ValueChangeEventFilter

from thermostats.heatit_zwave import HeatitZWave

class ZTRM6(HeatitZWave):
    def __init__(self, name, thConfig):
        #sensore usato per la regolazione: deve corrispondere al parametro 2 del device
        sensor = 'internal'
        if 'sensor' in thConfig:
            sensor = str(thConfig['sensor']).lower()

        if sensor == 'external':
            temperatureItem = f'{str(name)}_temperature_external'
        elif sensor == 'floor':
            temperatureItem = f'{str(name)}_temperature_floor'
        else:
            #interno: e' l'item di default della classe TemperatureSensor
            sensor = 'internal'
            temperatureItem = None

        super().__init__(name, thConfig, temperatureItem)

        self._sensor = sensor
        log.debug(f'{self.name}: Z-TRM6 in regolazione sul sensore {self._sensor}')

        #sensori non usati per la regolazione: solo lettura, e solo se cablati/agganciati.
        #Quello scelto per la regolazione lo segue gia' la classe TemperatureSensor
        self._temperature_external = None
        if self._sensor != 'external':
            self._temperature_external = self.to_float(
                    self.bind_optional(f'{str(self.name)}_temperature_external', self.temperature_external_changed))

        self._temperature_floor = None
        if self._sensor != 'floor':
            self._temperature_floor = self.to_float(
                    self.bind_optional(f'{str(self.name)}_temperature_floor', self.temperature_floor_changed))

        #misura di consumo a bordo: il device la fornisce sempre
        self.create_metering_items()

        self._power = self.to_float(self.utils.bindItem(
                                    f'{str(self.name)}_power',
                                    self.power_changed,
                                    ValueChangeEventFilter(), 0.0))

        #sicurezze del device: sovratemperatura (Err6) e sovraccarico (Err7). Il rele' viene
        #aperto dal device, qui si tiene traccia perche' il termostato smette di scaldare
        self.bind_optional(f'{str(self.name)}_alarm_heat', self.alarm_heat_changed)
        self.bind_optional(f'{str(self.name)}_alarm_power', self.alarm_power_changed)

    def create_metering_items(self):
        itemName = f'{str(self.name)}_power'
        if not self.oh.item_exists(itemName):
            self.openhab.create_item('Number', itemName, label='Potenza assorbita', tags=['Measurement', 'Power'], groups=[self.name])
            self.utils.set_stateDescription_metadata(itemName, '%.0f W')

        itemName = f'{str(self.name)}_energy'
        if not self.oh.item_exists(itemName):
            self.openhab.create_item('Number', itemName, label='Energia consumata', tags=['Measurement', 'Energy'], groups=['gPersistence', self.name])
            self.utils.set_stateDescription_metadata(itemName, '%.2f kWh')

#temperature secondarie: None se il sensore non e' cablato
    @property
    def temperature_external(self):
        if self._sensor == 'external':
            return self.ts.temperature
        return self._temperature_external

    def temperature_external_changed(self, event):
        self._temperature_external = self.to_float(event.value)

    @property
    def temperature_floor(self):
        if self._sensor == 'floor':
            return self.ts.temperature
        return self._temperature_floor

    def temperature_floor_changed(self, event):
        self._temperature_floor = self.to_float(event.value)

#potenza assorbita dal carico collegato al rele'
    @property
    def power(self):
        return self._power

    def power_changed(self, event):
        self._power = self.to_float(event.value)

#allarmi del device
    def alarm_heat_changed(self, event):
        if str(event.value) == 'ON':
            log.warning(f'{self.name}: il Z-TRM6 ha rilevato una sovratemperatura e ha aperto il rele\'')

    def alarm_power_changed(self, event):
        if str(event.value) == 'ON':
            log.warning(f'{self.name}: il Z-TRM6 ha rilevato un sovraccarico e ha aperto il rele\'')
