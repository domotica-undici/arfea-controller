'''
    HEATIT Z-TEMP3 object
    This class manage an HEATIT Z-TEMP3 thermostat

    E' il successore dello Z-TEMP2: termostato A BATTERIE (2xAAA, o alimentazione esterna
    2.7-3.3VDC) SENZA uscite a bordo, pensato per impianti ad acqua. Il carico e' sempre una
    valvola comandata altrove, in due modi che convivono:
        a) valvole dell'impianto ARFEA (onoffvalves/analogvalves in thermo.yml), comandate
           dalla logica di HABApp -> il device fa solo da sonda + interfaccia a muro;
        b) valvole agganciate in associazione Z-Wave diretta (gruppo 2 -> Heatit Z-Water,
           ZM Single Relay, ecc.) -> regola il device, con il setpoint che gli passa l'impianto.
           Attenzione: su Z-Water/Z-Water2 vanno usati solo i rele' da 1 a 7.

    A differenza dello Z-TEMP2 questo device gestisce OFF/HEAT/COOL/ECO come modi distinti,
    quindi non serve piu' dedurre il modo dalla stagione: la mappatura e' diretta.

    About the items:
        1) if there is a device with the THING named the same as described in thermo.yml and the THING
            is defined into config/thing_ztemp3.yml-> habapp has already created items and bound them to the device's channel
            example thermo.yml:
              - name: TermostatoMatrimoniale
                model: ztemp3
                ambient: matrimoniale
                onoffvalves:
                  heat:
                    - name: valvolaRiscMatrimoniale
                      label: Valvola riscaldamento matrimoniale
        OR
        2) if there is not a physycal device this class should not be called and no ztemp3 defined in thermo.yml

    Channel del binding zwave -> item attesi (thing type thermofloor_heatitz-temp3_00_000):
        sensor_temperature              -> [nome]_temperature
        sensor_relhumidity              -> [nome]_relhumidity
        thermostat_mode                 -> [nome]_mode
        thermostat_state                -> [nome]_operatingstate
        thermostat_setpoint_heating     -> [nome]_setpoint_heating
        thermostat_setpoint_cooling     -> [nome]_setpoint_cooling
        battery-level                   -> [nome]_battery
'''

import logging
log = logging.getLogger('HABApp')

import HABApp

from thermostats.heatit_zwave import HeatitZWave
from thermostats.humidity_sensors import HumiditySensor

class ZTEMP3(HeatitZWave):
    def __init__(self, name, thConfig):
        super().__init__(name, thConfig)

        #create item if not exist
        self.hs = HumiditySensor(thConfig, self.commons)

        #livello batteria: l'item lo crea chi collega il channel, qui si segnala solo
        #quando scende, perche' a batteria scarica il termostato smette di riportare
        self._battery = self.to_float(self.bind_optional(f'{str(self.name)}_battery', self.battery_changed))

#umidita' relativa: la sonda e' a bordo del device.
#La property serve anche al termostato, che da qui capisce di poter calcolare il punto di rugiada
    @property
    def relhumidity(self):
        return self.hs.relhumidity

#livello batteria
    @property
    def battery(self):
        return self._battery

    def battery_changed(self, event):
        self._battery = self.to_float(event.value)
        if self._battery != None and self._battery <= 20.0:
            log.warning(f'{self.name}: batteria del Z-TEMP3 al {self._battery}%, va sostituita')
