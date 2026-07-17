import logging
log = logging.getLogger('HABApp')

import HABApp

class Utils(HABApp.Rule):
    def __init__(self):
        super().__init__()

    '''
    Get openHAB item and binds it to a function triggered on state change
    based on variable createIfNotExist:
        if true = create item and set default value
        if false do not create item and return None
    '''
    def bindItem(self, itemName, function=None, event=None, defaultValue=None):
        if self.openhab.item_exists(itemName):
            myItem = self.openhab.get_item(itemName)
            self.listen_event(itemName, function, event)
            ret = None
            if (myItem.state == None or myItem.state == 'NULL') and defaultValue != None:
                ret = defaultValue
                self.sendUpdateToItem(itemName, defaultValue)
            else:
                ret = myItem.state
        else:
            ret = defaultValue

        return ret
    '''
    Send a command to an item
    '''
    """ def sendUpdateToItem(th, itemName, itemValue):
        self.openhab.post_update(f'{th.name}_{itemName}', itemValue)
        th.__dict__[f'{th.name}_{itemName}'] = itemValue """

    def sendUpdateToItem(self, itemName, itemValue):
        self.openhab.post_update(itemName, itemValue)

    def sendCommandToItem(self, itemName, itemValue):
        self.openhab.send_command(itemName, itemValue)
    '''
    Convert on/off from switch to number
    '''
    def convertSwitchToNumber(self, type, status):
        if type == 'switch':
            return status
        if type == 'number':
            if status == "ON":
                return 1
            else:
                return 0
    '''
    Convert a state read back from an item into ON/OFF, based on the item type.
    Returns None when the state carries no usable value (NULL/UNDEF, wrong type)
    '''
    def convertNumberToSwitch(self, type, status):
        if status is None or status == 'NULL' or status == 'UNDEF':
            return None
        if type == 'switch':
            return str(status)
        if type == 'number':
            try:
                return "ON" if float(status) != 0 else "OFF"
            except (TypeError, ValueError):
                return None

    def set_stateDescription_metadata(self, itemName, pattern):
        self.openhab.set_metadata(itemName, "stateDescription", "displayState", {"pattern": str(pattern)} )