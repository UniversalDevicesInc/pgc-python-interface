#!/usr/bin/env python
"""
Python Interface for PGC NodeServers - Swarm
by Einstein.42 (James Milne) milne.james@gmail.com
"""

from copy import deepcopy
import json
import logging
import logging.handlers
import __main__ as main
import os
import zlib
import binascii
# from os.path import join, expanduser
# import paho.mqtt.client as mqtt
from AWSIoTPythonSDK.MQTTLib import AWSIoTMQTTClient as mqttClient
from urllib.parse import urlparse
from urllib.request import urlopen
try:
    import queue
except ImportError:
    import Queue as queue
import re
import sys
import time
import base64
import select
from .pythonjsonlogger import jsonlogger
from threading import Thread, Event, current_thread
import warnings

# from polyinterface import __features__

def warning_on_one_line(message, category, filename, lineno, file=None, line=None):
    return '{}:{}: {}: {}'.format(filename, lineno, category.__name__, message)

class LoggerWriter(object):
    def __init__(self, level):
        self.level = level

    def write(self, message):
        if not re.match(r'^\s*$', message):
            self.level(message.strip())

    def flush(self):
        pass

def setup_log():
    # Log Location
    # path = os.path.dirname(sys.argv[0])
    log_filename = '/app/logs/debug.log'
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
    # making a new file at 5MB, keep 2
    handler = logging.handlers.TimedRotatingFileHandler(log_filename, when="midnight", backupCount=10)
    handler2 = logging.StreamHandler()
    # Format each log message like this
    formatter = logging.Formatter('%(asctime)s [%(threadName)-10s] [%(levelname)-5s] %(message)s')
    # Attach the formatter to the handler
    handler.setFormatter(JsonFormatter())
    handler2.setFormatter(formatter)
    # Attach the handler to the logger
    logger.addHandler(handler)
    logger.addHandler(handler2)
    warnlog.addHandler(handler)
    warnlog.addHandler(handler2)
    return logger

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

class MQTTHandler(logging.Handler):
    def __init__(self, interface):
        logging.Handler.__init__(self)
        self.interface = interface
        self.topic = interface.logTopic

    def emit(self, record):
        msg = self.format(record)
        if self.interface.connected:
            self.interface._mqttc.publish(self.topic, msg, 0)


LOGGER = setup_log()
sys.stdout = LoggerWriter(LOGGER.debug)
sys.stderr = LoggerWriter(LOGGER.error)

class Interface(object):

    # pylint: disable=too-many-instance-attributes
    # pylint: disable=unused-argument

    __exists = False

    def __init__(self, name = False):
        if self.__exists:
            warnings.warn('Only one Interface is allowed.')
            return
        try:
            self.connected = False
            self.init = json.loads(os.environ['NODESERVER'])
            self.stage = os.environ['STAGE']
            self.mqttEndpoint = os.environ['MQTTENDPOINT']
            self.profileNum = self.init['profileNum']
            self.userId = self.init['userId']
            self.worker = self.init['worker']
            self.clientId = '{}_{}_{}'.format(self.worker, self.profileNum, self.userId)
            self.logBucket = self.init['logBucket']
            if self.worker is not 'none':
                LOGGER.info('Running with WorkerID: {}'.format(self.worker))
            else:
                LOGGER.error('WorkerID not present. Exiting...')
                sys.exit(1)
            self.id = self.init['id']
            self.firstRun = False
            if 'firstRun' in self.init:
                self.firstRun = self.init['firstRun']
                LOGGER.info('First run: {}'.format(self.firstRun))
            self.serverFile = None
            if os.path.isfile('server.json'):
                with open('server.json') as f:
                    self.serverFile = json.load(f)
                LOGGER.info('Server file read into memory...')
        except (KeyError, ValueError) as e:
            LOGGER.error('Required variables not set. Exiting...', exc_info=True)
            sys.exit(1)
        self.recvTopic = '{}/ns/{}'.format(self.stage, self.worker)
        self.sendTopic = '{}/ns'.format(self.stage)
        self.logTopic = '{}/frontend/{}/logs/{}'.format(self.stage, self.userId, self.worker)
        self.config = None
        self.loop = None
        self.mqttLogHandler = None
        self._shortPoll = None
        self._longPoll = None
        self.inQueue = queue.Queue()
        self._threads = {}
        self._threads['socket'] = Thread(target = self._startMqtt, name='Interface')
        self._mqttc = mqttClient(self.clientId)
        self._mqttc.onOnline = self._online
        self._mqttc.onMessage = self._message
        # self._mqttc.on_subscribe = self._subscribe
        self._mqttc.onOffline = self._disconnect
        # self._mqttc.on_publish = self._publish
        # self._mqttc.on_log = self._log
        # self._mqttc.enable_logger(logger=LOGGER)
        self.isyVersion = None
        self.running = True
        self.__configObservers = []
        self.__stopObservers = []
        self.__logObservers = []
        Interface.__exists = True

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

    def _startMqtt(self):
        """
        The client start method. Starts the thread for the MQTT Client
        and publishes the connected message.
        """
        LOGGER.info('Connecting to MQTT endpoint {}...'.format(self.mqttEndpoint))
        try:
            self._mqttc.configureEndpoint(self.mqttEndpoint, 8883)
            self._mqttc.configureCredentials('/app/certs/AmazonRootCA1.pem',
                '/app/certs/private.key',
                '/app/certs/iot.crt')
            # Configure the auto-reconnect backoff to start with 1 second and use 60 seconds as a maximum back off time.
            # Connection over 20 seconds is considered stable and will reset the back off time back to its base.
            # self._mqttc.configureConnectDisconnectTimeout(60)
            self._mqttc.configureAutoReconnectBackoffTime(1, 60, 20)
            self._mqttc.configureLastWill(self.sendTopic, json.dumps({
                'connected': False,
                'userId': self.userId,
                'topic': self.recvTopic,
                'profileNum': self.profileNum,
                'id': self.id
                }), 0)
            if self._mqttc.connect(30):
                self._connect()
        except Exception as err:
            LOGGER.error("MQTT Connection error: {}".format(err), exc_info=True)

    def _online(self):
        if current_thread().name != "MQTT":
            current_thread().name = "MQTT"
        self.connected = True
        LOGGER.info('MQTT Connected successfully to: {} as {}'.format(self.mqttEndpoint, self.clientId))
        self.send({'connected': True})

    #def _connect(self, mqttc, userdata, flags, rc):
    def _connect(self,mid=None, data=None):
        """
        The callback for when the client receives a CONNACK response from the server.
        Subscribing in on_connect() means that if we lose the connection and
        reconnect then subscriptions will be renewed.

        :param mqttc: The client instance for this callback
        :param userdata: The private userdata for the mqtt client. Not used in Polyglot
        :param flags: The flags set on the connection.
        :param rc: Result code of connection, 0 = Success, anything else is a failure
        """
        if self._mqttc.subscribe(self.recvTopic, 0, None):
            LOGGER.info('MQTT Subscribed to topic: {}'.format(self.recvTopic))
        self.inConfig(self.init)
        self.send({'connected': True})
        try:
            if self.firstRun:
                LOGGER.debug('here')
                self.updatePolls(self.serverFile)
                self.installprofile()
                self.updatePollsInPG(self.serverFile)
            else:
                self.updatePolls(self.init)
        except Exception as err:
            LOGGER.error('Poll thread error: {}'.format(err), exc_info=True)

    # def _message(self, mqttc, userdata, msg):
    def _message(self, msg):
        """
        The callback for when a PUBLISH message is received from the server.

        :param mqttc: The client instance for this callback
        :param userdata: The private userdata for the mqtt client. Not used in Polyglot
        :param flags: The flags set on the connection.
        :param msg: Dictionary of MQTT received message. Uses: msg.topic, msg.qos, msg.payload
        """
        try:
            parsed_msg = json.loads(msg.payload.decode('utf-8'))
            inputCmds = ['query', 'command', 'result', 'status', 'shortPoll', 'longPoll', 'oauth', 'delete']
            ignoreList = ['clientId', 'id', 'userId']
            LOGGER.debug('MQTT Received Message: {}: {}'.format(msg.topic, parsed_msg))
            for key in parsed_msg:
                if key == 'config':
                    LOGGER.debug('Recieved Message: config')
                    self.inConfig(parsed_msg[key])
                elif key == 'stop':
                    LOGGER.debug('Received stop from PGC... Shutting Down.')
                    self.stop()
                elif key == 'polls':
                    #self.updatePolls(parsed_msg[key])
                    pass
                elif key == 'startLogStream' or key == 'stopLogStream':
                    self._streamLog(key, parsed_msg)
                elif key == 'tailLog':
                    self._tailLog()
                elif key == 'removeTail':
                    self._removeTail()
                elif key in inputCmds:
                    LOGGER.debug('Received Message: {}'.format(parsed_msg))
                    self.inQueue.put_nowait(parsed_msg)
                elif key in ignoreList:
                    pass
                else:
                    LOGGER.error('Invalid command received in message from Polyglot: {}'.format(key))
        except (Exception, ValueError, json.decoder.JSONDecodeError) as err:
            LOGGER.error('MQTT Received Payload Error: {} :: {}'.format(err, repr(msg)), exc_info=True)

    # def _disconnect(self, mqttc, userdata, rc):
    def _disconnect(self):
        """
        The callback for when a DISCONNECT occurs.

        :param mqttc: The client instance for this callback
        :param userdata: The private userdata for the mqtt client. Not used in Polyglot
        :param rc: Result code of connection, 0 = Graceful, anything else is unclean
        """
        self.connected = False
        LOGGER.info("MQTT disconnected. Trying reconnect.")

    def _log(self, mqttc, userdata, level, string):
        """ Use for debugging MQTT Packets, disable for normal use, NOISY. """
        # LOGGER.info('MQTT Log - {}: {}'.format(str(level), str(string)))
        pass

    def _subscribe(self, mqttc, userdata, mid, granted_qos):
        """ Callback for Subscribe message. Unused currently. """
        # LOGGER.info("MQTT Subscribed Succesfully for Message ID: {} - QoS: {}".format(str(mid), str(granted_qos)))
        pass

    def _publish(self, mqttc, userdata, mid):
        """ Callback for publish message. Unused currently. """
        # LOGGER.info("MQTT Published message ID: {}".format(str(mid)))
        pass

    def updatePollsInPG(self, polls = {}):
        try:
            if {'shortPoll', 'longPoll'} <= polls.keys():
                payload = { 'polls' : {}}
                payload['polls']['shortPoll'] = polls['shortPoll']
                payload['polls']['longPoll'] = polls['longPoll']
                self.send(payload)
        except Exception as err:
            LOGGER.error('Poll thread error: {}'.format(err), exc_info=True)

    def updatePolls(self, polls = {}):
        try:
            if 'shortPoll' in polls:
                if self._shortPoll is not None:
                    self._shortPoll()
                self._shortPoll = self.setInterval(int(polls['shortPoll']), self._poll, 'shortPoll')
            if 'longPoll' in polls:
                if self._longPoll is not None:
                    self._longPoll()
                self._longPoll = self.setInterval(int(polls['longPoll']), self._poll)
        except Exception as err:
            LOGGER.error('Poll thread error: {}'.format(err), exc_info=True)

    def _poll(self, poll = 'longPoll'):
        pollType = {}
        pollType[poll] = {}
        self.inQueue.put_nowait(pollType)

    def _streamLog(self, type, msg):
        try:
            if type == 'startLogStream':
                if msg['clientId'] not in self.__logObservers:
                    self.__logObservers.append(msg['clientId'])
                if self.mqttLogHandler is None:
                    self.mqttLogHandler = MQTTHandler(self)
                    self.mqttLogHandler.setFormatter(JsonFormatter())
                logFile = '/app/logs/debug.log'
                with open(logFile, 'rb') as f:
                    logFileData = f.read()
                compressed = bytearray(zlib.compress(logFileData, zlib.Z_BEST_COMPRESSION))
                compressRatio = 100.0 * ((float(len(logFileData)) - float(len(compressed))) / float(len(logFileData)))
                LOGGER.addHandler(self.mqttLogHandler)
                LOGGER.debug('Sending Logfile to frontend: {} :: Compressed {}%'.format(msg['clientId'], compressRatio))
                # LOGGER.debug('Compressed: {}'.format(binascii.hexlify(compressed)))
                self._mqttc.publish(self.logTopic + '/file', compressed, 0)
            else:
                if msg['clientId'] in self.__logObservers:
                    self.__logObservers.remove(msg['clientId'])
                    if not self.__logObservers:
                        LOGGER.removeHandler(self.mqttLogHandler)
            LOGGER.info('Started streaming log to frontend client: {}'.format(msg['clientId']))
            LOGGER.debug('{} :: Current Streams: {}'.format(type, self.__logObservers))
        except (Exception) as err:
            LOGGER.error('_streamLog: {}'.format(err), exc_info=True)

    # Set a threaded timer for Polls
    # https://stackoverflow.com/questions/22498038/improve-current-implementation-of-a-setinterval-python/22498708#22498708
    def setInterval(self, interval, func, *args):
        stopped = Event()
        def loop():
            while not stopped.wait(interval): # the first call is in `interval` secs
                func(*args)
        Thread(target=loop).start()
        return stopped.set

    def start(self):
        """
        The client start method. Starts the thread for the MQTT Client
        and publishes the connected message.
        """
        for _, thread in self._threads.items():
            thread.start()

    def stop(self, deleting = False):
        """
        The client stop method. If the client is currently connected
        stop the thread and disconnect. Publish the disconnected
        message if clean shutdown.
        """
        try:
            self.running = False
            if self._shortPoll is not None:
                self._shortPoll()
            if self._longPoll is not None:
                self._longPoll()
            if self.connected:
                if not deleting:
                    self.send({'connected': False})
                LOGGER.info('Disconnecting from MQTT...')
                self._mqttc.disconnect()
                self._mqttc.loop_stop()
            for watcher in self.__stopObservers:
                watcher()
            sys.exit(0)
        except Exception as e:
            LOGGER.exception('Exception in stop: {}'.format(e), exc_info=True)

    def send(self, message, service = None):
        if not isinstance(message, dict) and self.connected:
            warnings.warn('payload not a dictionary')
            return False
        try:
            topic = self.sendTopic if service is None else '{}/{}'.format(self.stage, service)
            message['userId'] = self.userId
            message['topic'] = self.recvTopic
            message['profileNum'] = self.profileNum
            message['id'] = self.id
            LOGGER.debug('Sent Message: [{}] : {}'.format(topic, json.dumps(message)))
            self._mqttc.publish(topic, json.dumps(message), 0)
        except TypeError as err:
            LOGGER.error('MQTT Send Error: {}'.format(err), exc_info=True)

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
        if node.hint is not None:
            message['addnode']['hint'] = node.hint
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
        LOGGER.info('Installing Profile to ISY.')
        try:
            profileFolder = 'profile/'
            if not os.path.isdir(profileFolder):
                LOGGER.error('Profile folder does not exist. Aborting.')
            else:
                importTypes = ['nodedef', 'editor', 'nls']
                for type in importTypes:
                    pathFolder = '{}{}'.format(profileFolder, type)
                    if os.path.isdir(pathFolder):
                        extension = '.txt' if type is 'nls' else '.xml'
                        files = [f for f in os.listdir(pathFolder) if f.lower().endswith(extension)]
                        for fileName in files:
                            with open('{}'.format(os.path.join(pathFolder, fileName)), 'rb') as fb:
                                fileData = fb.read()
                            payload = {
                                'uploadProfile': {
                                    'type': type,
                                    'filename': fileName,
                                    'payload': base64.encodestring(fileData).decode('ascii')
                                }
                            }
                            self.send(payload, 'isy')
        except (Exception) as err:
            LOGGER.error('installprofile: {}'.format(err), exc_info=True)

    def getNode(self, address):
        """
        Get Node by Address of existing nodes.
        """
        try:
            if address in self.config['nodes']:
                return self.config['nodes'][address]
            else:
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
            self.config = deepcopy(config)
            self.isyVersion = config['isyVersion']
            # LOGGER.debug('Received config. ISY Version: {}'.format(self.isyVersion))
            for watcher in self.__configObservers:
                watcher(self.config)

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
                    'address': self.address,
                    'driver': name,
                    'value': driver['value'],
                    'uom': driver['uom']
                }
                self.controller._driversQueue.put(message)
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
        message = {
            'batch': {
                'status': []
            }
        }
        for name, driver in self.drivers.items():
            message['batch']['status'].append({
                'address': self.address,
                'driver': name,
                'value': driver['value'],
                'uom': driver['uom']
            })
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
    hint = None


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
            self._driversQueue = queue.Queue()
            self._nodesQueue = queue.Queue()
            self._nodes = {}
            self.config = None
            self.nodes = { self.address: self }
            self._threads = {}
            self._threads['input'] = Thread(target = self._parseInput, name = 'Controller')
            self._threads['ns']  = Thread(target = self.start, name = 'NodeServer')
            self._threads['drivers'] = Thread(target = self._driverHandler, name='Drivers')
            self._threads['nodes'] = Thread(target = self._nodesHandler, name='Nodes')
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
            self._nodes[address] = deepcopy(node)
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
            self._threads['drivers'].start()
            self._threads['nodes'].start()

    def _startThreads(self):
        self._threads['input'].daemon = True
        self._threads['ns'].daemon = True
        self._threads['drivers'].daemon = True
        self._threads['nodes'].daemon = True
        self._threads['input'].start()

    def _parseInput(self):
        while self.poly.running:
            try:
                input = self.poly.inQueue.get()
                for key in input:
                    if key == 'command':
                        if input[key]['address'] in self.nodes:
                            try:
                                self.nodes[input[key]['address']].runCmd(input[key])
                                if 'query' in input[key] and 'requestId' in input[key]['query']:
                                    self.poly.send({
                                        'report': {
                                            'requestId': input[key]['query']['requestId'],
                                            'success': True
                                        }
                                    })
                            except (Exception) as err:
                                LOGGER.error('_parseInput: failed {}.runCmd({}) {}'.format(input[key]['address'], input[key]['cmd'], err), exc_info=True)
                                if 'query' in input[key] and 'requestId' in input[key]['query']:
                                    self.poly.send({
                                        'report': {
                                            'requestId': input[key]['query']['requestId'],
                                            'success': False
                                        }
                                    })
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

    def _handleResult(self, results):
        try:
            if 'addnode' in results:
                if isinstance(results['addnode'], list):
                    for result in results['addnode']:
                        # Added properly or if 400 it already existed
                        if result['success'] or result['statusCode'] == 400:
                            if not result['address'] == self.address:
                                time.sleep(1)
                                self.nodes[result['address']].start()
                            if result['address'] in self.nodesAdding:
                                self.nodesAdding.remove(result['address'])
                        else:
                            del self.nodes[result['address']]
                else:
                    if results['addnode']['success']:
                        if not results['addnode']['address'] == self.address:
                            time.sleep(1)
                            self.nodes[results['addnode']['address']].start()
                        if results['addnode']['address'] in self.nodesAdding:
                            self.nodesAdding.remove(results['addnode']['address'])
                    else:
                        del self.nodes[result['addnode']['address']]
        except (KeyError, ValueError) as err:
            print(err)
            LOGGER.error('handleResult: {}'.format(err), exc_info=True)

    def _delete(self):
        """
        Intermediate message that stops MQTT before sending to overrideable method for delete.
        """
        self.delete()
        self.poly.stop(True)

    def _driverHandler(self):
        maxUpdates = 20
        while self.poly.running:
            updates = []
            for num in range(0, maxUpdates):
                try:
                    if num == maxUpdates:
                        break
                    updates.append(self._driversQueue.get_nowait())
                except(queue.Empty):
                    break
            if updates:
                message = {
                    'batch': {
                        'status': updates
                    }
                }
                self.poly.send(message)
            time.sleep(5)

    def _nodesHandler(self):
        maxAdds = 20
        while self.poly.running:
            adds = []
            for num in range(0, maxAdds):
                try:
                    if num == maxAdds:
                        break
                    adds.append(self._nodesQueue.get_nowait())
                except(queue.Empty):
                    break
            if adds:
                message = {
                    'batch': {
                        'addnode': adds
                    }
                }
                self.poly.send(message)
            time.sleep(5)

    def delete(self):
        """
        Incoming delete message from Polyglot. This NodeServer is being deleted.
        You have 5 seconds before the process is killed. Cleanup and disconnect.
        """
        pass

    def _addNode(self, node):
        """
        Add a node to the NodeServer

        :param node: Dictionary of node settings. Keys: address, name, nodedefid, primary, and drivers are required.
        """
        controller = hasattr(node, 'isController')
        LOGGER.info('Adding {} node {}({})'.format('Controller' if controller else '', node.name, node.address))
        message = {
            'address': node.address,
            'name': node.name,
            'nodedefid': node.id,
            'primary': node.primary,
            'drivers': node.drivers,
            'isController': True if controller else False
        }
        if node.hint is not None:
            message['hint'] = node.hint
        if controller:
            self.poly.send({'addnode': message})
        else:
            self._nodesQueue.put(message)

    """
    AddNode adds the class to self.nodes then sends the request to Polyglot
    If update is True, overwrite the node in Polyglot
    """
    def addNode(self, node, update=False):
        if node.address in self._nodes:
            node._drivers = self._nodes[node.address]['drivers']
            for name, driver in node.drivers.items():
                if name in node._drivers:
                    driver['value'] = deepcopy(node._drivers[name]['value'])
        else:
            node._drivers = deepcopy(node.drivers)
        self.nodes[node.address] = node
        # if node.address not in self._nodes or update:
        self.nodesAdding.append(node.address)
        self._addNode(node)
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
        LOGGER.info('Recieved oauth {}'.format(oauth))

    def shortPoll(self):
        pass

    def query(self):
        for node in self.nodes:
            self.nodes[node].reportDrivers()

    def status(self):
        for node in self.nodes:
            self.nodes[node].reportDrivers()

    def runForever(self):
        self.poly._threads['socket'].join()

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
    drivers = {'ST': { 'value': 1, 'uom': 2 }}


if __name__ == "__main__":
    sys.exit(0)
