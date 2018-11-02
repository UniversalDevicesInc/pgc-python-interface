#!/usr/bin/env python
"""
Python Interface for PGC NodeServers
by Einstein.42 (James Milne) milne.james@gmail.com
"""

from copy import deepcopy
import json
import time
import logging
import logging.handlers
import __main__ as main
import os
# from os.path import join, expanduser
try:
    import queue
except ImportError:
    import Queue as queue
import re
import sys
import select
import time
import socket
import struct
from threading import Thread
from .pythonjsonlogger import jsonlogger
import warnings

# from polyinterface import __features__

SOCKETFILE = '/tmp/pgsocket.sock'
LOGFILE = '/tmp/pglog.sock'

class JsonFormatter(jsonlogger.JsonFormatter, object):
    def __init__(self,
                 fmt="%(name) %(processName) %(filename) %(funcName) %(levelname) %(lineno) %(module) %(threadName) %(message)",
                 datefmt="%Y-%m-%dT%H:%M:%SZ%z",
                 style='%',
                 *args, **kwargs):
        # self._extra = extra
        jsonlogger.JsonFormatter.__init__(self, fmt=fmt, datefmt=datefmt, *args, **kwargs)

    def process_log_record(self, log_record):
        # Enforce the presence of a timestamp
        log_record["timestamp"] = int(time.time()*1000)
        return super(JsonFormatter, self).process_log_record(log_record)

class SysLogJsonHandler(logging.handlers.SysLogHandler, object):
    # Override constructor
    def __init__(self, address=('localhost', logging.handlers.SYSLOG_UDP_PORT),
                 facility=logging.handlers.SysLogHandler.LOG_USER, socktype=None, prefix=""):
        super(SysLogJsonHandler, self).__init__(address, facility, socktype)
        self._prefix = prefix
        if self._prefix != "":
            self._prefix = prefix + ": "

    # Override format method to handle prefix
    def format(self, record):
        return self._prefix + super(SysLogJsonHandler, self).format(record)

def warning_on_one_line(message, category, filename, lineno, file=None, line=None):
    return '{}:{}: {}: {}'.format(filename, lineno, category.__name__, message)

def setup_log():
    # Log Location
    # path = os.path.dirname(sys.argv[0])
    log_filename = '/app/logs/debug.txt'
    log_level = logging.DEBUG  # Could be e.g. "DEBUG" or "WARNING"

    # ### Logging Section ################################################################################
    logging.captureWarnings(True)
    logger = logging.getLogger(__name__)
    logger.propagate = False
    warnlog = logging.getLogger('py.warnings')
    warnings.formatwarning = warning_on_one_line
    logger.setLevel(log_level)
    # Set the log level to LOG_LEVEL
    # Make a handler that writes to a file,
    # making a new file at midnight and keeping 3 backups
    handler = logging.handlers.TimedRotatingFileHandler(log_filename, when="midnight", backupCount=7)
    handler2 = SysLogJsonHandler(LOGFILE, socktype=socket.SOCK_STREAM)
    # Format each log message like this
    formatter = JsonFormatter()
    # formatter = logging.Formatter('[%(threadName)-8.8s] [%(levelname)-7s]: %(message)s')
    # formatter = logging.Formatter('{"log": {"thread": "%(threadName)s", "levelname": "%(levelname)s", "msg": "%(message)s"}}')
    # Attach the formatter to the handler
    handler.setFormatter(formatter)
    handler2.setFormatter(formatter)
    # Attach the handler to the logger
    logger.addHandler(handler)
    logger.addHandler(handler2)
    warnlog.addHandler(handler)
    return logger

LOGGER = setup_log()

class Interface(object):

    # pylint: disable=too-many-instance-attributes
    # pylint: disable=unused-argument

    __exists = False

    def __init__(self, name = False):
        if self.__exists:
            warnings.warn('Only one Interface is allowed.')
            return
        self.init = None
        self.config = None
        self.loop = None
        self.inQueue = queue.Queue()
        self._threads = {}
        self._threads['socket'] = Thread(target = self._message, name='Interface')
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.isyVersion = None
        self.__configObservers = []
        self.__stopObservers = []
        Interface.__exists = True
        self.running = True

    def onConfig(self, callback):
        """
        Gives the ability to bind any methods to be run when the config is received.
        """
        self.__configObservers.append(callback)

    def onStop(self, callback):
        """
        Gives the ability to bind any methods to be run when the stop command is received.
        """
        self.__stopObservers.append(callback)

    def _message(self):
        self.sock.connect(SOCKETFILE)
        buffer = ''
        messages = []
        while self.running:
            try:
                data = self.sock.recv(7168)
                payload = data.decode('utf-8')
                messages = payload.split('\n')
                if messages[len(messages) - 1] == '':
                    if buffer is not '':
                        messages[0] = '{}{}'.format(buffer, messages[0])
                        buffer = ''
                    messages.pop()
                else:
                    buffer += messages.pop()
                for msg in messages:
                    if msg is not '' or msg is not None:
                        parsed_msg = json.loads(msg)
                        inputCmds = ['query', 'command', 'result', 'status', 'shortPoll', 'longPoll', 'delete']
                        for key in parsed_msg:
                            if key == 'init':
                                LOGGER.debug('Recieved Message: init')
                                self.init = parsed_msg[key]
                                self.inConfig(parsed_msg[key])
                            elif key == 'config':
                                LOGGER.debug('Recieved Message: config')
                                self.inConfig(parsed_msg[key])
                            elif key == 'stop':
                                LOGGER.debug('Received stop from Polyglot... Shutting Down.')
                                self.stop()
                            elif key in inputCmds:
                                LOGGER.debug('Received Message: {}'.format(parsed_msg))
                                self.inQueue.put(parsed_msg)
                            else:
                                LOGGER.error('Invalid command received in message from Polyglot: {}'.format(key))
            except (ValueError, json.decoder.JSONDecodeError) as err:
                LOGGER.error('Received Payload Error: {} :: {}'.format(err, repr(msg)), exc_info=True)

    def start(self):
        """
        The client start method. Starts the thread for the MQTT Client
        and publishes the connected message.
        """
        for _, thread in self._threads.items():
            thread.start()
        self.send({ 'connected': True })

    def stop(self):
        """
        The client stop method. If the client is currently connected
        stop the thread and disconnect. Publish the disconnected
        message if clean shutdown.
        """
        try:
            self.running = False
            for watcher in self.__stopObservers:
                watcher()
            sys.exit(0)
        except KeyError as e:
            LOGGER.exception('KeyError in gotConfig: {}'.format(e), exc_info=True)

    def send(self, message):
        if not isinstance(message, dict):
            warnings.warn('payload not a dictionary')
            return False

        self.sock.sendall((json.dumps(message) + '\n').encode('utf-8'))

    def addNode(self, node):
        """
        Add a node to the NodeServer

        :param node: Dictionary of node settings. Keys: address, name, nodedefid, primary, and drivers are required.
        """
        LOGGER.info('Adding node {}({})'.format(node.name, node.address))
        message = {
            'addnode': {
                'address': node.address,
                'name': node.name,
                'nodedefid': node.id,
                'primary': node.primary,
                'drivers': node.drivers,
                'isController': True if hasattr(node, 'isController') else False
            }
        }
        self.send(message)

    def removeNode(self, address):
        """
        Delete a node from the NodeServer

        :param node: Dictionary of node settings. Keys: address, name, nodedefid, primary, and drivers are required.
        """
        LOGGER.info('Removing node {}'.format(address))
        message = {
            'removenode': {
                'address': address
            }
        }
        self.send(message)

    def saveCustomData(self, data):
        """
        Send custom dictionary to Polyglot to save and be retrieved on startup.

        :param data: Dictionary of key value pairs to store in Polyglot database.
        """
        LOGGER.info('Sending customData to Polyglot.')
        message = { 'customdata': data }
        self.send(message)

    def saveCustomParams(self, data):
        """
        Send custom dictionary to Polyglot to save and be retrieved on startup.

        :param data: Dictionary of key value pairs to store in Polyglot database.
        """
        LOGGER.info('Sending customParams to Polyglot.')
        message = { 'customparams': data }
        self.send(message)

    def saveNotices(self, data):
        """
        Add custom notice to front-end for this NodeServers

        :param data: String of characters to add as a notification in the front-end.
        """
        LOGGER.info('Sending notices to Polyglot.')
        message = { 'notices': data }
        self.send(message)

    def restart(self):
        """
        Send a command to Polyglot to restart this NodeServer
        """
        LOGGER.info('Asking Polyglot to restart me.')
        message = {
            'restart': {}
        }
        self.send(message)

    def installprofile(self):
        LOGGER.info('Sending Install Profile command to Polyglot.')
        message = { 'installprofile': {} }
        self.send(message)

    def getNode(self, address):
        """
        Get Node by Address of existing nodes.
        """
        try:
            for node in self.config['nodes']:
                if node['address'] == address:
                    return node
            return False
        except KeyError:
            LOGGER.error('Usually means we have not received the config yet.', exc_info=True)
            return False

    def inConfig(self, config):
        """
        Save incoming config received from Polyglot to Interface.config and then do any functions
        that are waiting on the config to be received.
        """
        try:
            self.config = config
            self.isyVersion = config['isyVersion']
            # LOGGER.debug('Received config. ISY Version: {}'.format(self.isyVersion))
            for watcher in self.__configObservers:
                watcher(config)

        except KeyError as e:
            LOGGER.error('KeyError in gotConfig: {}'.format(e), exc_info=True)


class Node(object):
    """
    Node Class for individual devices.
    """
    def __init__(self, controller, primary, address, name):
        try:
            self.controller = controller
            self.parent = self.controller
            self.primary = primary
            self.address = address
            self.name = name
            self.polyConfig = None
            self.drivers = self._convertDrivers(self.drivers)
            self._drivers = self._convertDrivers(self.drivers)
            self.config = None
            self.timeAdded = None
            self.isPrimary = False
            self.started = False
        except (KeyError) as err:
            LOGGER.error('Error Creating node: {}'.format(err), exc_info=True)

    def _convertDrivers(self, drivers):
        if isinstance(drivers, list):
            newFormat = {}
            for driver in drivers:
                newFormat[driver['driver']] = {}
                newFormat[driver['driver']]['value'] = str(driver['value'])
                newFormat[driver['driver']]['uom'] = str(driver['uom'])
            return newFormat
        else:    
            return deepcopy(drivers)

    def setDriver(self, driver, value, report=True, force=False, uom=None):
        try:
            if driver in self.drivers:
                self.drivers[driver]['value'] = str(value)
                if uom is not None:
                    self.drivers[driver]['uom'] = str(uom)
                if report:
                    self.reportDriver(driver, self.drivers[driver], report, force)
        except Exception as e:
            LOGGER.error('setDriver: {}'.format(e), exc_info=True)

    def reportDriver(self, name, driver, report, force):
        try:
            new = self.drivers[name]
            existing = self._drivers[name]
            if (new['value'] != existing['value'] or new['uom'] != existing['uom'] or force):
                LOGGER.info('Updating Driver {} - {}: {} uom: {}'.format(self.address, name, new['value'], new['uom']))
                message = {
                    'status': {
                        'address': self.address,
                        'driver': name,
                        'value': driver['value'],
                        'uom': driver['uom']
                    }
                }
                self.controller.poly.send(message)
        except Exception as e:
            LOGGER.error('reportDriver: {}'.format(e), exc_info=True)

    def reportCmd(self, command, value=None, uom=None):
        message = {
            'command': {
                'address': self.address,
                'command': command
            }
        }
        if value is not None and uom is not None:
            message['command']['value'] = str(value)
            message['command']['uom'] = uom
        self.controller.poly.send(message)

    def reportDrivers(self):
        LOGGER.info('Updating All Drivers to ISY for {}({})'.format(self.name, self.address))
        # self.updateDrivers(self.drivers)
        for name, driver in self.drivers.items():
            message = {
                'status': {
                    'address': self.address,
                    'driver': name,
                    'value': driver['value'],
                    'uom': driver['uom']
                }
            }
            self.controller.poly.send(message)

    def updateDrivers(self, drivers):
        self._drivers = deepcopy(drivers)

    def query(self):
        self.reportDrivers()

    def status(self):
        self.reportDrivers()

    def runCmd(self, command):
        if command['cmd'] in self.commands:
            fun = self.commands[command['cmd']]
            fun(self, command)

    def start(self):
        pass

    def getDriver(self, dv):
        return self.controller.polyConfig.nodes[self.address][dv].value or None

    def toJSON(self):
        LOGGER.debug(json.dumps(self.__dict__))

    def __rep__(self):
        return self.toJSON()

    id = ''
    commands = {}
    drivers = {}
    sends = {}


class Controller(Node):
    """
    Controller Class for controller management. Superclass of Node
    """
    __exists = False

    def __init__(self, poly):
        if self.__exists:
            warnings.warn('Only one Controller is allowed.')
            return
        try:
            self.controller = self
            self.isController = True
            self.parent = self.controller
            self.poly = poly
            self.poly.onConfig(self._gotConfig)
            self.poly.onStop(self.stop)
            self.name = 'Controller'
            self.address = 'controller'
            self.primary = self.address
            self.drivers = self._convertDrivers(self.drivers)
            self._drivers = self._convertDrivers(self.drivers)
            self._nodes = {}
            self.config = None
            self.nodes = { self.address: self }
            self._threads = {}
            self._threads['input'] = Thread(target = self._parseInput, name = 'Controller')
            self._threads['ns']  = Thread(target = self.start, name = 'NodeServer')
            self.polyConfig = None
            self.timeAdded = None
            self.isPrimary = True
            self.started = False
            self.nodesAdding = []
            self._startThreads()
        except (KeyError) as err:
            LOGGER.error('Error Creating node: {}'.format(err), exc_info=True)

    def _convertDrivers(self, drivers):
        if isinstance(drivers, list):
            newFormat = {}
            for driver in drivers:
                newFormat[driver['driver']] = {}
                newFormat[driver['driver']]['value'] = driver['value']
                newFormat[driver['driver']]['uom'] = driver['uom']
            return newFormat
        else:    
            return deepcopy(drivers)

    def _gotConfig(self, config):
        self.polyConfig = config
        for address, node in config['nodes'].items():
            self._nodes[address] = node
            if address in self.nodes:
                currentNode = self.nodes[address]
                setattr(currentNode, '_drivers', deepcopy(node['drivers']))
                setattr(currentNode, 'config', deepcopy(node))
                setattr(currentNode, 'timeAdded', deepcopy(node['timeAdded']))
                setattr(currentNode, 'isPrimary', deepcopy(node['isPrimary']))
                #self.nodes[address].config = node
                #self.nodes[address].timeAdded = node['timeAdded']
                #self.nodes[address].isPrimary = node['isPrimary']
        if self.address not in self._nodes:
            self.addNode(self)
            LOGGER.info('Waiting on Controller node to be added.......')
        elif not self.started:
            self.nodes[self.address] = self
            self.started = True
            # self.setDriver('ST', 1, True, True)
            # time.sleep(1)
            self._threads['ns'].start()

    def _startThreads(self):
        self._threads['input'].daemon = True
        self._threads['ns'].daemon = True
        self._threads['input'].start()

    def _parseInput(self):
        while self.poly.running:
            try: 
                input = self.poly.inQueue.get_nowait()
                for key in input:
                    if key == 'command':
                        if input[key]['address'] in self.nodes:
                            try:
                                self.nodes[input[key]['address']].runCmd(input[key])
                            except (Exception) as err:
                                LOGGER.error('_parseInput: failed {}.runCmd({}) {}'.format(input[key]['address'], input[key]['cmd'], err), exc_info=True)
                        else:
                            LOGGER.error('_parseInput: received command {} for a node that is not in memory: {}'.format(input[key]['cmd'], input[key]['address']))
                    elif key == 'result':
                        self._handleResult(input[key])
                    elif key == 'delete':
                        self._delete()
                    elif key == 'shortPoll':
                        self.shortPoll()
                    elif key == 'longPoll':
                        self.longPoll()
                    elif key == 'oauth':
                        self.oauth(input[key])
                    elif key == 'query':
                        if input[key]['address'] in self.nodes:
                            self.nodes[input[key]['address']].query()
                        elif input[key]['address'] == 'all':
                            self.query()
                    elif key == 'status':
                        if input[key]['address'] in self.nodes:
                            self.nodes[input[key]['address']].status()
                        elif input[key]['address'] == 'all':
                            self.status()
                self.poly.inQueue.task_done()
            except(queue.Empty):
                pass

    def _handleResult(self, result):
        try:
            if 'addnode' in result:
                if result['addnode']['success']:
                    if not result['addnode']['address'] == self.address:
                        time.sleep(1)
                        self.nodes[result['addnode']['address']].start()
                    # self.nodes[result['addnode']['address']].reportDrivers()
                    if result['addnode']['address'] in self.nodesAdding:
                        self.nodesAdding.remove(result['addnode']['address'])
                else:
                    del self.nodes[result['addnode']['address']]
        except (KeyError, ValueError) as err:
            print(err)
            LOGGER.error('handleResult: {}'.format(err), exc_info=True)

    def _delete(self):
        """
        Intermediate message that stops MQTT before sending to overrideable method for delete.
        """
        self.poly.stop()
        self.delete()

    def delete(self):
        """
        Incoming delete message from Polyglot. This NodeServer is being deleted.
        You have 5 seconds before the process is killed. Cleanup and disconnect.
        """
        pass

    """
    AddNode adds the class to self.nodes then sends the request to Polyglot
    If update is True, overwrite the node in Polyglot
    """
    def addNode(self, node, update=False, controller=False):
        self.nodes[node.address] = node
        # if node.address not in self._nodes or update:
        self.nodesAdding.append(node.address)
        self.poly.addNode(node)
        # else:
        # self.nodes[node.address].start()
        return node

    """
    Forces a full overwrite of the node
    """
    def updateNode(self, node):
        self.nodes[node.address] = node
        self.nodesAdding.append(node.address)
        self.poly.addNode(node)

    def removeNode(self, address):
        """
        Just send it along if requested, should be able to delete the node even if it isn't
        in our config anywhere. Usually used for normalization.
        """
        if address in self.nodes:
            del self.nodes[address]
        self.poly.removeNode(address)

    def delNode(self, address):
        # Legacy API
        self.removeNode(address)

    def longPoll(self):
        pass

    def oauth(self, oauth):
        pass
        
    def shortPoll(self):
        pass

    def query(self):
        for node in self.nodes:
            self.nodes[node].reportDrivers()

    def status(self):
        for node in self.nodes:
            self.nodes[node].reportDrivers()

    def runForever(self):
        self._threads['input'].join()

    def start(self):
        pass

    def saveCustomData(self, data):
        if not isinstance(data, dict):
            LOGGER.error('saveCustomData: data isn\'t a dictionary. Ignoring.')
        else:
            self.poly.saveCustomData(data)

    def addCustomParam(self, data):
        if not isinstance(data, dict):
            LOGGER.error('addCustomParam: data isn\'t a dictionary. Ignoring.')
        else:
            newData = deepcopy(self.poly.config['customParams'])
            newData.update(data)
            self.poly.saveCustomParams(newData)

    def removeCustomParam(self, data):
        try:  # check whether python knows about 'basestring'
            basestring
        except NameError:  # no, it doesn't (it's Python3); use 'str' instead
            basestring = str
        if not isinstance(data, basestring):
            LOGGER.error('removeCustomParam: data isn\'t a string. Ignoring.')
        else:
            try:
                newData = deepcopy(self.poly.config['customParams'])
                newData.pop(data)
                self.poly.saveCustomParams(newData)
            except KeyError:
                LOGGER.error('{} not found in customParams. Ignoring...'.format(data), exc_info=True)

    def getCustomParam(self, data):
        params = deepcopy(self.poly.config['customParams'])
        return params.get(data)

    def getCustomParams(self, data):
        return self.poly.config['customParams']

    def addNotice(self, data):
        if not isinstance(data, dict):
            LOGGER.error('addNotice: data isn\'t a dictionary. Ignoring.')
        else:
            newData = deepcopy(self.poly.config['notices'])
            newData.update(data)
            self.poly.saveNotices(newData)

    def removeNotice(self, key):
        try:  # check whether python knows about 'basestring'
            basestring
        except NameError:  # no, it doesn't (it's Python3); use 'str' instead
            basestring = str
        if not isinstance(data, basestring):
            LOGGER.error('removeNotice: data isn\'t a string. Ignoring.')
        else:
            try:
                newData = deepcopy(self.poly.config['notices'])
                newData.pop(data)
                self.poly.saveNotices(newData)
            except KeyError:
                LOGGER.error('{} not found in notices. Ignoring...'.format(data), exc_info=True)

    def removeNoticesAll(self):
        self.poly.saveNotices({})

    def getNotice(self, data):
        params = deepcopy(self.poly.config['customParams'])
        return params.get(data)

    def getNotices(self):
        return self.poly.config['notices']

    def stop(self):
        """ Called on nodeserver stop """
        pass

    id = 'controller'
    commands = {}
    drivers = {'ST': { 'value': 0, 'uom': 2 }}


if __name__ == "__main__":
    sys.exit(0)
