__version__ = '1.1.8'
__description__ = 'PGC Interface'
__url__ = 'https://github.com/UniversalDevicesInc/pgc-python-interface'
__author__ = 'James Milne'
__authoremail__ = 'milne.james@gmail.com'
__license__ = 'MIT'

from .pgc_interface import Interface, Node, Controller, LOGGER

LOGGER.info('{} {} Starting...'.format(__description__, __version__))
