'''
    Main class contains common support_tool
'''

import logging
log = logging.getLogger('HABApp')

import HABApp
import datetime

class Tools(HABApp.Rule):
    def __init__(self):
        super().__init__()
        self.object = None 
        self.timers = {}

    def startCountdown(self, name):
        log.debug(f'resetto il timer {name}')
        if name in self.timers:
            self.timers[name].reset()

    def createTimer(self, name, countdownTime, function):
        self.cancelTimer(name)
        self.timers[name]=self.run.countdown(countdownTime, function)

    #cancel the job
    def cancelTimer(self, name):
        if name in self.timers:
            self.timers[name].cancel()
            self.timers.pop(name)

    #Stops the countdown so it can be started again with a call to reset
    def pauseTimer(self, name):
        if name in self.timers:
            self.timers[name].stop()

    def getRemainingTimer(self, name):
        if name in self.timers:
            if self.timers[name].remaining() != None:
                # Data e ora attuale
                now = datetime.datetime.now()

                # Data e ora specificata
                specific_time = self.timers[name].next_run_datetime

                # Calcolare la differenza
                difference = specific_time - now

                # Convertire la differenza in ore e minuti
                hours, remainder = divmod(difference.total_seconds(), 3600)
                minutes, _ = divmod(remainder, 60)

                # Formattare la differenza in HH:mm
                formatted_difference = f"{int(hours):02}:{int(minutes):02}"

                

                """ ret = self.timers[name].remaining().seconds
                h = int(ret  / 3600)
                m = int((ret % 3600) / 60)
                ret = f'{h}:{m}' """
                return formatted_difference
            else:
                return "0"
        else:
            return "0"

Tools()
