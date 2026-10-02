'''
    HEATIT Z-Wave thermostats - classe base comune
    Base condivisa da Z-TRM6 (alimentato, rele' a bordo) e Z-Temp3 (a batterie, comanda valvole).

    I due dispositivi espongono la stessa interfaccia Z-Wave:
        Thermostat Mode         0=OFF, 1=HEAT, 2=COOL, 11(0x0B)=ECO
        Thermostat Setpoint     3 setpoint (heating, cooling, eco), 5-40 gradi a passi di 0.5
        Thermostat Operating    0=idle, 1=heating (anche in ECO), 2=cooling
        Sensor Multilevel       temperatura (e umidita' sullo Z-Temp3)

    Modalita' ECO: non e' gestita dall'impianto ARFEA (che ha gia' le fasce orarie), quindi
    viene letta come HEAT. Il device resta in ECO col suo setpoint dedicato finche' non e'
    l'impianto a cambiare modo: cosi' chi la imposta a muro non se la vede annullare.

    Entrambi i device regolano da soli (Z-TRM6 il rele' a bordo, Z-Temp3 le valvole associate
    in gruppo 2): l'impianto gli passa modo e setpoint e sono loro ad aprire e chiudere.
    Per gli stati che l'impianto NON esprime col setpoint - la finestra aperta - il termostato
    chiama apply_internalState() ad ogni ciclo e qui si spegne il device.

    NB: fermare il device abbassandogli il setpoint (come fa la sua funzione Open Window
    Detection) NON si puo' fare: la classe Setpoint rilegge il setpoint del device come una
    modifica fatta dall'utente a muro e porterebbe tutto l'impianto in manuale a 5 gradi.
'''

import logging
log = logging.getLogger('HABApp')

import HABApp
from HABApp.core.events import ValueChangeEventFilter

from thermostats.thermo_commons import ThermoCommons
from thermostats.utils import States
from system.utils import Utils
from thermostats.temperature_sensors import TemperatureSensor
from thermostats.setpoint import Setpoint

class HeatitZWave(HABApp.Rule):
    #valori del channel thermostat_mode del binding zwave
    DEVICE_OFF = 0.0
    DEVICE_HEAT = 1.0
    DEVICE_COOL = 2.0
    DEVICE_ECO = 11.0

    def __init__(self, name, thConfig, temperatureItem=None):
        '''
            temperatureItem = item da usare per la regolazione. None = default della
            classe TemperatureSensor, cioe' [nome termostato]_temperature
        '''
        super().__init__()

        self._callbacks = []

        self.states = States()
        self.utils = Utils()
        self.commons = ThermoCommons()

        self.name = name
        self.thConfig = thConfig

        #ultimo modo riportato dal device e flag "l'ho spento io" (finestra aperta):
        #vanno valorizzati prima di agganciare l'item, che puo' richiamare subito mode_changed
        self._deviceMode = None
        self._forced = False

        #create item if not exist
        tsConfig = thConfig
        if temperatureItem != None and 'temperature_sensor' not in thConfig:
            tsConfig = dict(thConfig)
            tsConfig['temperature_sensor'] = temperatureItem
        self.ts = TemperatureSensor(tsConfig, self.commons)
        self._temperature = self.ts.temperature

        #il modo arriva dal device: l'item e' quello legato al channel thermostat_mode.
        #Se non c'e' il termostato nasce lo stesso (temperatura, setpoint e pianificazione
        #restano utili) ma non puo' comandare il device: meglio dirlo una volta con un
        #messaggio che spiega cosa collegare, che tirare un comando al minuto contro un
        #item inesistente e riempire il log di 404 senza mai dire perche'.
        modeItem = f'{str(self.name)}_mode'
        self._hasModeItem = self.oh.item_exists(modeItem)
        if not self._hasModeItem:
            log.warning(f'{self.name}: item {modeItem} assente, il termostato non comandera\' il '
                        f'dispositivo. Collega il channel del modo (zwave-js: thermostat-mode-mode-N, '
                        f'zwave classico: thermostat_mode) a un item Number con questo nome, '
                        f'poi riavvia HABApp')

        self._deviceMode = self.to_float(self.utils.bindItem(
                                    modeItem,
                                    self.mode_changed,
                                    ValueChangeEventFilter(), self.DEVICE_HEAT))
        self._mode = self.device_to_internal(self._deviceMode)

        #stato dell'uscita riportato dal device (channel thermostat_state): sola lettura
        self._operatingstate = float(self.utils.bindItem(
                                    f'{str(self.name)}_operatingstate',
                                    self.operatingstate_changed,
                                    ValueChangeEventFilter(), 0.0))

        #2 setpoint: caldo e freddo. La classe Setpoint aggancia solo quelli che esistono
        self.sp = Setpoint(name, 2)
        self._setpoint = 20.0
        self._setpoint_heating = self.sp.setpoint_heating

    def _notify_observers(self, parameter, old_value, new_value):
        for callback in self._callbacks:
            callback(parameter, old_value, new_value)

    def register_callback(self, callback):
        self._callbacks.append(callback)

    #triggered by subclass' callbacks
    def parameter_changed(self, parameter, old_value, new_value):
        return

#_mode
    @property
    def mode(self):
        return float(self._mode)

    @mode.setter
    def mode(self, new_value):
        old_value = self.mode
        self._mode = float(new_value)
        self._notify_observers("mode", old_value, new_value)

    '''
    Traduzioni fra i modi del device e quelli interni dell'impianto.
    Per OFF/HEAT/COOL i valori coincidono, ECO viene regolato come HEAT
    '''
    def device_to_internal(self, value):
        value = self.to_float(value)
        if value == self.DEVICE_OFF:
            return self.states.internalModes()["OFF"]
        if value == self.DEVICE_COOL:
            return self.states.internalModes()["COOL"]
        #HEAT, ECO e qualunque altro modo non gestito
        return self.states.internalModes()["HEAT"]

    def internal_to_device(self, value):
        value = float(value)
        if value == self.states.internalModes()["OFF"]:
            return self.DEVICE_OFF
        if value == self.states.internalModes()["COOL"] or value == self.states.internalModes()["COOL_ECONOMY"]:
            return self.DEVICE_COOL
        if value == self.states.internalModes()["HEAT"] or value == self.states.internalModes()["HEAT_ECONOMY"]:
            return self.DEVICE_HEAT
        #DRY, FAN, AUTO non esistono su questi device: meglio spegnere che regolare a caso
        return self.DEVICE_OFF

    #event from device -> translate it and send to OH
    def mode_changed(self, event):
        value = self.to_float(event.value)
        if value == None:
            return

        self._deviceMode = value

        if self._forced:
            #e' il device che sta rispondendo allo spegnimento per finestra aperta,
            #non una scelta dell'utente: l'impianto deve restare come l'ha lasciato
            return

        self.mode = self.device_to_internal(value)

    #event from OH -> translate it and send to device
    def set_mode(self, value):
        value = float(value)
        if value != self.mode:
            if not self._hasModeItem:
                self._mode = value
                return
            retValue = self.internal_to_device(value)
            log.debug(f'set mode to {self.name}: {retValue}')
            self.utils.sendCommandToItem(f'{self.name}_mode', retValue)

#_operatingstate: stato dell'uscita riportato dal device (0=fermo, 1=riscalda, 2=raffresca)
    @property
    def operatingstate(self):
        return float(self._operatingstate)

    @operatingstate.setter
    def operatingstate(self, new_value):
        self._operatingstate = float(new_value)

    def operatingstate_changed(self, event):
        self.operatingstate = float(event.value)

    '''
    Chiamata dal termostato ad ogni ciclo (thermo_thermostat.set_actuators).
    Il device regola da solo, quindi:
        - a finestra aperta lo spengo, altrimenti continua a scaldare per conto suo.
          Modo e gestione dell'impianto NON si toccano: l'utente ritrova le sue impostazioni
          appena la finestra si richiude
        - negli altri casi riallineo il modo del device a quello dell'impianto, cosi' un
          comando perso via radio o un cambio fatto a HABApp fermo si recupera al ciclo dopo
    '''
    def apply_internalState(self, internalMode, internalState):
        if not self._hasModeItem:
            return

        if internalState == self.states.internalStates()["WINDOWSTOP"]:
            target = self.DEVICE_OFF
            self._forced = True
        else:
            target = self.internal_to_device(internalMode)
            self._forced = False

        if self.device_mode_matches(target):
            return

        log.debug(f'allineo il modo del device {self.name}: da {self._deviceMode} a {target}')
        self.utils.sendCommandToItem(f'{self.name}_mode', target)

    '''
    True se il device sta gia' facendo quello che chiede l'impianto.
    ECO vale come HEAT: e' riscaldamento, cambia solo il setpoint che usa il device
    '''
    def device_mode_matches(self, target):
        if self._deviceMode == None:
            return False

        if target == self.DEVICE_HEAT and self._deviceMode == self.DEVICE_ECO:
            return True

        return self._deviceMode == target

    '''
    Aggancia un item solo se esiste: gli item opzionali (sensori aggiuntivi, allarmi, batteria)
    li crea chi collega i channel del device, non l'impianto
    '''
    def bind_optional(self, itemName, function):
        if not self.oh.item_exists(itemName):
            return None

        return self.utils.bindItem(itemName, function, ValueChangeEventFilter(), None)

    '''
    Converte lo stato di un item in float. None se non e' un numero (NULL, UNDEF, item assente)
    '''
    def to_float(self, value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
