import logging
log = logging.getLogger('HABApp')

import HABApp
from HABApp.core.events import ValueChangeEventFilter

"""
This class represents a load. for an item to be managed here it must be into the group named as "gLoads"
"""
class Load(HABApp.Rule):
    def __init__(self, load):
        super().__init__()

        self.name = load['name']

        self.label = self.name
        if 'label' in load:
            self.label = load['label']

#Create item for load
        itemName = self.name
        self.openhab.create_item("Switch", itemName, label=self.label, tags=['WhiteGood'], groups=["gLoads"])
        self.openhab.send_command(itemName, "ON")

        self._type = load['type']
        self._status = "ON"
        self._priority = None #load['priority']
        self._consumptionWhenDisconnected = 0.0
        self._theoricalConsumption = 0.0

        self.listen_event(itemName, self.load_status_changed, ValueChangeEventFilter())

    def load_status_changed(self, event):
        status = str(event.value)
        self.nonc(status)

#used to convert onoff to the right value wether load is a switch of type NO (normally open) or NC (normally closed)
    def nonc(self, status):
        if self._type == "NO":
            self.openhab.send_command(self.name, status)
        if self._type == "NC":
            if status == "ON":
                self.openhab.send_command(self.name, "OFF")
            if status == "OFF":
                self.openhab.send_command(self.name, "ON")
