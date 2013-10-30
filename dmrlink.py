# Copyright (c) 2013 Cortney T. Buffington, N0MJS and the K0USY Group. n0mjs@me.com
#
# This work is licensed under the Creative Commons Attribution-ShareAlike
# 3.0 Unported License.To view a copy of this license, visit
# http://creativecommons.org/licenses/by-sa/3.0/ or send a letter to
# Creative Commons, 444 Castro Street, Suite 900, Mountain View,
# California, 94041, USA.

from __future__ import print_function
from twisted.internet.protocol import DatagramProtocol
from twisted.internet import reactor
from twisted.internet import task
import ConfigParser
import os
import sys
import argparse
import binascii
import hmac
import hashlib
import socket
import csv
import re

#************************************************
#     IMPORTING OTHER FILES - '#include'
#************************************************

# Import system logger configuration
#
try:
    from ipsc.ipsc_logger import logger
except ImportError:
    sys.exit('System logger configuration not found or invalid')

# Import IPSC message types and version information
#
try:
    from ipsc.ipsc_message_types import *
except ImportError:
    sys.exit('IPSC message types file not found or invalid')

# Import IPSC flag mask values
#
try:
    from ipsc.ipsc_mask import *
except ImportError:
    sys.exit('IPSC mask values file not found or invalid')

ids = {}
try:
    with open('./radioids.csv', 'r') as radioids_csv:
        radio_ids = csv.reader(radioids_csv, dialect='excel', delimiter=',')
        for row in radio_ids:
            ids[int(row[1])] = (row[0])
except ImportError:
    sys.exit('No Radio ID CSV file found')
    
    
#************************************************
#     PARSE THE CONFIG FILE AND BUILD STRUCTURE
#************************************************

'''
***LINKING STATUS: Byte 6***

	Byte 1 - BIT FLAGS:
	      xx.. .... = Peer Operational (01 only known valid value)
	      ..xx .... = Peer MODE: 00 - No Radio, 01 - Analog, 10 - Digital
	      .... xx.. = IPSC Slot 1: 10 on, 01 off 
	      .... ..xx = IPSC Slot 2: 10 on, 01 off

***SERVICE FLAGS: Bytes 7-10 (or 7-12)***

	Byte 1 - 0x00  	= Unknown
	Byte 2 - 0x00	= Unknown
	Byte 3 - BIT FLAGS:
	      x... .... = CSBK Message
	      .x.. .... = Repeater Call Monitoring
	      ..x. .... = 3rd Party "Console" Application
	      ...x xxxx = Unknown - default to 0
	Byte 4 = BIT FLAGS:
	      x... .... = XNL Connected (1=true)
	      .x.. .... = XNL Master Device
	      ..x. .... = XNL Slave Device
	      ...x .... = Set if packets are authenticated
	      .... x... = Set if data calls are supported
	      .... .x.. = Set if voice calls are supported
	      .... ..x. = Unknown - default to 0
	      .... ...x = Set if master
'''

networks = {}
NETWORK = {}

config = ConfigParser.ConfigParser()
config.read('./dmrlink.cfg')

for section in config.sections():
    if section == 'GLOBAL':
        pass
    else:
        NETWORK.update({section: {'LOCAL': {}, 'MASTER': {}, 'PEERS': []}})
        NETWORK[section]['LOCAL'].update({
            'MODE': '',
            'PEER_OPER': True,
            'PEER_MODE': 'DIGITAL',
            'FLAGS': '',
            'MAX_MISSED': 10,
            'NUM_PEERS': 0,
            'STATUS': {
                'ACTIVE': False
                },
            'ENABLED': config.getboolean(section, 'ENABLED'),
            'TS1_LINK': config.getboolean(section, 'TS1_LINK'),
            'TS2_LINK': config.getboolean(section, 'TS2_LINK'),
            'AUTH_ENABLED': config.getboolean(section, 'AUTH_ENABLED'),
            'RADIO_ID': (config.get(section, 'RADIO_ID').rjust(8,'0')).decode('hex'),
            'PORT': config.getint(section, 'PORT'),
            'ALIVE_TIMER': config.getint(section, 'ALIVE_TIMER'),
            'AUTH_KEY': (config.get(section, 'AUTH_KEY').rjust(40,'0')).decode('hex'),
            })
        NETWORK[section]['MASTER'].update({
            'RADIO_ID': '\x00\x00\x00\x00',
            'MODE': '\x00',
            'PEER_OPER': False,
            'PEER_MODE': '',
            'TS1_LINK': False,
            'TS2_LINK': False,
            'FLAGS': '\x00\x00\x00\x00',
            'STATUS': {
                'CONNECTED': False,
                'PEER_LIST': False,
                'KEEP_ALIVES_SENT': 0,
                'KEEP_ALIVES_MISSED': 0,
                'KEEP_ALIVES_OUTSTANDING': 0 
                },
            'IP': config.get(section, 'MASTER_IP'),
            'PORT': config.getint(section, 'MASTER_PORT')
            })
        
        if NETWORK[section]['LOCAL']['AUTH_ENABLED']:
            NETWORK[section]['LOCAL']['FLAGS'] = '\x00\x00\x00\x1C'
        else:
            NETWORK[section]['LOCAL']['FLAGS'] = '\x00\x00\x00\x0C'
    
        if not NETWORK[section]['LOCAL']['TS1_LINK'] and not NETWORK[section]['LOCAL']['TS2_LINK']:    
            NETWORK[section]['LOCAL']['MODE'] = '\x65'
        elif NETWORK[section]['LOCAL']['TS1_LINK'] and not NETWORK[section]['LOCAL']['TS2_LINK']:    
            NETWORK[section]['LOCAL']['MODE'] = '\x66'
        elif not NETWORK[section]['LOCAL']['TS1_LINK'] and NETWORK[section]['LOCAL']['TS2_LINK']:    
            NETWORK[section]['LOCAL']['MODE'] = '\x69'
        else:
            NETWORK[section]['LOCAL']['MODE'] = '\x6A'


#************************************************
#     UTILITY FUNCTIONS FOR INTERNAL USE
#************************************************

# Convert a hex string to an int (radio ID, etc.)
#
def int_id(_hex_string):
    return int(binascii.b2a_hex(_hex_string), 16)

# Re-Write Source Radio-ID (DMR NAT)
#
def dmr_nat(_data, _nat_id):
#    _log = logger.warning
    src_radio_id = _data[6:9]
    _data = re.sub(src_radio_id, _nat_id, _data)
#    _log('DMR NAT: Source %s re-written as %s', int(binascii.b2a_hex(src_radio_id), 16), int(binascii.b2a_hex(_nat_id), 16))
    return _data

# Lookup text data for numeric IDs
#
def get_info(_id):
    if _id in ids:
            return ids[_id]
    return _id

# Remove the hash from a packet and return the payload
#
def strip_hash(_data):
#    _log = logger.debug
#    _log('Stripped Packet: %s', binascii.b2a_hex(_data[:-10]))
    return _data[:-10]


# Determine if the provided peer ID is valid for the provided network 
#
def valid_peer(_peer_list, _peerid):
#    _log = logger.debug
    if _peerid in _peer_list:
#        _log('Peer List Has An Entry For: %s', binascii.b2a_hex(_peerid))
        return True
#    _log('Peer List Does NOT Have An Entry For: %s', binascii.b2a_hex(_peerid))        
    return False


# Determine if the provided master ID is valid for the provided network
#
def valid_master(_network, _peerid):
#    _log = logger.warning
    if NETWORK[_network]['MASTER']['RADIO_ID'] == _peerid:
#        _log('Master ID is Valid: %s', binascii.b2a_hex(_peerid))
        return True     
    else:
#        _log('Master ID is NOT Valid: %s', binascii.b2a_hex(_peerid))
        return False
        
            
# Accept a complete packet, ready to be sent, and send it to all active peers + master in an IPSC
#
def send_to_ipsc(_target, _packet):
#    _log = logger.debug
    # Send to the Master
#    _log('Sending %s to:', binascii.b2a_hex(_packet)    
    networks[_target].transport.write(_packet, (NETWORK[_target]['MASTER']['IP'], NETWORK[_target]['MASTER']['PORT']))
#    _log('     Master: %s', binascii.b2a_hex(NETWORK[_target]['MASTER']['RADIO_ID']))
    # Send to each connected Peer
    for peer in NETWORK[_target]['PEERS']:
        if peer['STATUS']['CONNECTED'] == True:
            networks[_target].transport.write(_packet, (peer['IP'], peer['PORT']))
#            _log('     Peer: %s', binascii.b2a_hex(peer['RADIO_ID']))
        
        
# De-register a peer from an IPSC by removing it's infomation
#
def de_register_peer(_network, _peerid):
#    _log = logger.debug
    # Iterate for the peer in our data
#    _log('Peer De-Registration Requested for: %s', binascii.b2a_hex(_peerid))
    for peer in NETWORK[_network]['PEERS']:
        # If we find the peer, remove it (we should find it)
        if _peerid == peer['RADIO_ID']:
            NETWORK[_network]['PEERS'].remove(peer)
#            _log('     Peer Found And De-Registered')
            return
        else:
#            _log('     Peer NOT Found')
            pass
       
        
# Take a recieved peer list and the network it belongs to, process and populate the
# data structure in my_ipsc_config with the results, and return a simple list of peers.
#
def process_peer_list(_data, _network, _peer_list):
#    _log = logger.debug
    # Determine the length of the peer list for the parsing iterator
    _peer_list_length = int(binascii.b2a_hex(_data[5:7]), 16)
    # Record the number of peers in the data structure... we'll use it later (11 bytes per peer entry)
    NETWORK[_network]['LOCAL']['NUM_PEERS'] = _peer_list_length/11
    #    _log('<<- (%s) The Peer List has been Received from Master\n%s There are %s peers in this IPSC Network', _network, (' '*(len(_network)+7)), _num_peers)
    
    # Iterate each peer entry in the peer list. Skip the header, then pull the next peer, the next, etc.
    for i in range(7, (_peer_list_length)+7, 11):
        # Extract various elements from each entry...
        _hex_radio_id = (_data[i:i+4])
        _hex_address  = (_data[i+4:i+8])
        _ip_address   = socket.inet_ntoa(_hex_address)
        _hex_port     = (_data[i+8:i+10])
        _port         = int(binascii.b2a_hex(_hex_port), 16)
        _hex_mode     = (_data[i+10:i+11])
        _mode         = int(binascii.b2a_hex(_hex_mode), 16)
        # mask individual Mode parameters
        _link_op      = _mode & PEER_OP_MSK
        _link_mode    = _mode & PEER_MODE_MSK
        _ts1          = _mode & IPSC_TS1_MSK
        _ts2          = _mode & IPSC_TS2_MSK    
        
        # Determine whether or not the peer is operational
        if   _link_op == 0b01000000:
            _peer_op = True
        else:
            _peer_op = False
              
        # Determine the operational mode of the peer
        if   _link_mode == 0b00000000:
            _peer_mode = 'NO_RADIO'
        elif _link_mode == 0b00010000:
            _peer_mode = 'ANALOG'
        elif _link_mode == 0b00100000:
            _peer_mode = 'DIGITAL'
        else:
            _peer_node = 'NO_RADIO'
            
        # Determine whether or not timeslot 1 is linked
        if _ts1 == 0b00001000:
             _ts1 = True
        else:
             _ts1 = False
             
        # Determine whether or not timeslot 2 is linked
        if _ts2 == 0b00000010:
            _ts2 = True
        else:
            _ts2 = False  

        # If this entry was NOT already in our list, add it.
        #     Note: We keep a "simple" peer list in addition to the large data
        #           structure because soemtimes, we just need to identify a
        #           peer quickly.
        if _hex_radio_id not in _peer_list:
            _peer_list.append(_hex_radio_id)
            NETWORK[_network]['PEERS'].append({
                'RADIO_ID':  _hex_radio_id, 
                'IP':        _ip_address, 
                'PORT':      _port, 
                'MODE':      _hex_mode,
                'PEER_OPER': _peer_op,
                'PEER_MODE': _peer_mode,
                'TS1_LINK':  _ts1,
                'TS2_LINK':  _ts2,
                'STATUS':    {'CONNECTED': False, 'KEEP_ALIVES_SENT': 0, 'KEEP_ALIVES_MISSED': 0, 'KEEP_ALIVES_OUTSTANDING': 0}
            })
    return _peer_list


# Gratuituous print-out of the peer list.. Pretty much debug stuff.
#
def print_peer_list(_network):
#    _log = logger.info
    _status = NETWORK[_network]['MASTER']['STATUS']['PEER_LIST']
    #print('Peer List Status for {}: {}' .format(_network, _status))
    
    if _status and not NETWORK[_network]['PEERS']:
        print('We are the only peer for: %s' % _network)
        print('')
        return
        
    print('Peer List for: %s' % _network)
    for dictionary in NETWORK[_network]['PEERS']:
        if dictionary['RADIO_ID'] == NETWORK[_network]['LOCAL']['RADIO_ID']:
            me = '(self)'
        else:
            me = ''
        print('\tRADIO ID: {} {}' .format(int(binascii.b2a_hex(dictionary['RADIO_ID']), 16), me))
        print('\t\tIP Address: {}:{}' .format(dictionary['IP'], dictionary['PORT']))
        print('\t\tOperational: {},  Mode: {},  TS1 Link: {},  TS2 Link: {}' .format(dictionary['PEER_OPER'], dictionary['PEER_MODE'], dictionary['TS1_LINK'], dictionary['TS2_LINK']))
        print('\t\tStatus: {},  KeepAlives Sent: {},  KeepAlives Outstanding: {},  KeepAlives Missed: {}' .format(dictionary['STATUS']['CONNECTED'], dictionary['STATUS']['KEEP_ALIVES_SENT'], dictionary['STATUS']['KEEP_ALIVES_OUTSTANDING'], dictionary['STATUS']['KEEP_ALIVES_MISSED']))
    print('')
        
# Gratuituous print-out of Master info.. Pretty much debug stuff.
#
def print_master(_network):
#    _log = logger.info
    _master = NETWORK[_network]['MASTER']
    print('Master for %s' % _network)
    print('\tRADIO ID: {}' .format(int(binascii.b2a_hex(_master['RADIO_ID']), 16)))
    print('\t\tIP Address: {}:{}' .format(_master['IP'], _master['PORT']))
    print('\t\tOperational: {},  Mode: {},  TS1 Link: {},  TS2 Link: {}' .format(_master['PEER_OPER'], _master['PEER_MODE'], _master['TS1_LINK'], _master['TS2_LINK']))
    print('\t\tStatus: {},  KeepAlives Sent: {},  KeepAlives Outstanding: {},  KeepAlives Missed: {}' .format(_master['STATUS']['CONNECTED'], _master['STATUS']['KEEP_ALIVES_SENT'], _master['STATUS']['KEEP_ALIVES_OUTSTANDING'], _master['STATUS']['KEEP_ALIVES_MISSED']))


#************************************************
#********                             ***********
#********    IPSC Network 'Engine'    ***********
#********                             ***********
#************************************************

#************************************************
#     Base Class (used nearly all of the time)
#************************************************


class IPSC(DatagramProtocol):
    
    # Modify the initializer to set up our environment and build the packets
    # we need to maitain connections
    #
    def __init__(self, *args, **kwargs):
        if len(args) == 1:
            # Housekeeping: create references to the configuration and status data for this IPSC instance.
            # Some configuration objects that are used frequently and have lengthy names are shortened
            # such as (self._master_sock) expands to (self._config['MASTER']['IP'], self._config['MASTER']['PORT']).
            # Note that many of them reference each other... this is the Pythonic way.
            #
            self._network = args[0]
            self._config = NETWORK[self._network]
            #
            self._local = self._config['LOCAL']
            self._local_stat = self._local['STATUS']
            self._local_id = self._local['RADIO_ID']
            #
            self._master = self._config['MASTER']
            self._master_stat = self._master['STATUS']
            self._master_sock = self._master['IP'], self._master['PORT']
            #
            self._peers = self._config['PEERS']
            #
            # This is a regular list to store peers for the IPSC. At times, parsing a simple list is much less
            # Spendy than iterating a list of dictionaries... Maybe I'll find a better way in the future. Also
            # We have to know when we have a new peer list, so a variable to indicate we do (or don't)
            #
            self._peer_list = []
            args = ()
            
            
            # Packet 'constructors' - builds the necessary control packets for this IPSC instance.
            # This isn't really necessary for anything other than readability (reduction of code golf)
            #
            self.TS_FLAGS             = (self._local['MODE'] + self._local['FLAGS'])
            self.MASTER_REG_REQ_PKT   = (MASTER_REG_REQ + self._local_id + self.TS_FLAGS + IPSC_VER)
            self.MASTER_ALIVE_PKT     = (MASTER_ALIVE_REQ + self._local_id + self.TS_FLAGS + IPSC_VER)
            self.PEER_LIST_REQ_PKT    = (PEER_LIST_REQ + self._local_id)
            self.PEER_REG_REQ_PKT     = (PEER_REG_REQ + self._local_id + IPSC_VER)
            self.PEER_REG_REPLY_PKT   = (PEER_REG_REPLY + self._local_id + IPSC_VER)
            self.PEER_ALIVE_REQ_PKT   = (PEER_ALIVE_REQ + self._local_id + self.TS_FLAGS)
            self.PEER_ALIVE_REPLY_PKT = (PEER_ALIVE_REPLY + self._local_id + self.TS_FLAGS)
            
        else:
            # If we didn't get called correctly, log it!
            #
            logger.error('(%s) Unexpected arguments found.', self._network)
            sys.exit()


    # This is called by REACTOR when it starts, We use it to set up the timed
    # loop for each instance of the IPSC engine
    #       
    def startProtocol(self):
        # Timed loops for:
        #   IPSC connection establishment and maintenance
        #   Reporting/Housekeeping
        #
        self._maintenance = task.LoopingCall(self.maintenance_loop)
        self._maintenance_loop = self._maintenance.start(self._local['ALIVE_TIMER'])
        #
        self._reporting = task.LoopingCall(self.reporting_loop)
        self._reporting_loop = self._reporting.start(10)

    #************************************************
    #     CALLBACK FUNCTIONS FOR USER PACKET TYPES
    #************************************************

    def call_ctl_1(self, _network, _data):
        print('({}) Call Control Type 1 Packet Received From: {}' .format(_network, _src_sub))
    
    def call_ctl_2(self, _network, _data):
        print('({}) Call Control Type 2 Packet Received' .format(_network))
    
    def call_ctl_3(self, _network, _data):
        print('({}) Call Control Type 3 Packet Received' .format(_network))
    
    def xcmp_xnl(self, _network, _data):
        print('({}) XCMP/XNL Packet Received From: {}' .format(_network, _src_sub))
    
    def group_voice(self, _network, _src_sub, _dst_sub, _ts, _end, _peerid, _data):
        _dst_sub    = get_info(int_id(_dst_sub))
        _peerid     = get_info(int_id(_peerid))
        _src_sub    = get_info(int_id(_src_sub))
        print('({}) Group Voice Packet Received From: {}, IPSC Peer {}, Destination {}' .format(_network, _src_sub, _peerid, _dst_sub))
    
    def private_voice(self, _network, _src_sub, _dst_sub, _ts, _end, _peerid, _data):
        _dst_sub    = get_info(int_id(_dst_sub))
        _peerid     = get_info(int_id(_peerid))
        _src_sub    = get_info(int_id(_src_sub))
        print('({}) Private Voice Packet Received From: {}, IPSC Peer {}, Destination {}' .format(_network, _src_sub, _peerid, _dst_sub))
    
    def group_data(self, _network, _src_sub, _dst_sub, _ts, _end, _peerid, _data):    
        _dst_sub    = get_info(int_id(_dst_sub))
        _peerid     = get_info(int_id(_peerid))
        _src_sub    = get_info(int_id(_src_sub))
        print('({}) Group Data Packet Received From: {}, IPSC Peer {}, Destination {}' .format(_network, _src_sub, _peerid, _dst_sub))
    
    def private_data(self, _network, _src_sub, _dst_sub, _ts, _end, _peerid, _data):    
        _dst_sub    = get_info(int_id(_dst_sub))
        _peerid     = get_info(int_id(_peerid))
        _src_sub    = get_info(int_id(_src_sub))
        print('({}) Private Data Packet Received From: {}, IPSC Peer {}, Destination {}' .format(_network, _src_sub, _peerid, _dst_sub))

    def unknown_message(self, _network, _packettype, _peerid, _data):
        _time = time.strftime('%m/%d/%y %H:%M:%S')
        _packettype = binascii.b2a_hex(_packettype)
        _peerid = get_info(int_id(_peerid))
        print('{} ({}) Unknown message type encountered\n\tPacket Type: {}\n\tFrom: {}' .format(_time, _network, _packettype, _peerid))
        print('\t', binascii.b2a_hex(_data))


    # Take a packet to be SENT, calcualte auth hash and return the whole thing
    #
    def hashed_packet(self, _key, _data):
        _hash = binascii.a2b_hex((hmac.new(_key,_data,hashlib.sha1)).hexdigest()[:20])
        return (_data + _hash)    
    
    
    # Take a RECEIVED packet, calculate the auth hash and verify authenticity
    #
    def validate_auth(self, _key, _data):
        _log = logger.info
        _payload = strip_hash(_data)
        _hash = _data[-10:]
        _chk_hash = binascii.a2b_hex((hmac.new(_key,_payload,hashlib.sha1)).hexdigest()[:20])   

        if _chk_hash == _hash:
            return True
        else:
            _log('AUTHENTICATION FAILURE: \n\t Payload: %s\n\t Hash: %s', binascii.b2a_hex(_payload), binascii.b2a_hex(_hash))
            return False


#************************************************
#     TIMED LOOP - MY CONNECTION MAINTENANCE
#************************************************
    
    def reporting_loop(self):
        # Right now, without this, we really dont' know anything is happening.  
        # print_master(self._network)
        # print_peer_list(self._network)
        pass
    
    def maintenance_loop(self):
        
        # If the master isn't connected, we have to do that before we can do anything else!
        #
        if self._master_stat['CONNECTED'] == False:
            reg_packet = self.hashed_packet(self._local['AUTH_KEY'], self.MASTER_REG_REQ_PKT)
            self.transport.write(reg_packet, (self._master_sock))
        
        # Once the master is connected, we have to send keep-alives.. and make sure we get them back
        elif (self._master_stat['CONNECTED'] == True):
            # Send keep-alive to the master
            master_alive_packet = self.hashed_packet(self._local['AUTH_KEY'], self.MASTER_ALIVE_PKT)
            self.transport.write(master_alive_packet, (self._master_sock))
            
            # If we had a keep-alive outstanding by the time we send another, mark it missed.
            if (self._master_stat['KEEP_ALIVES_OUTSTANDING']) > 0:
                self._master_stat['KEEP_ALIVES_MISSED'] += 1
            
            # If we have missed too many keep-alives, de-regiseter the master and start over.
            if self._master_stat['KEEP_ALIVES_OUTSTANDING'] >= self._local['MAX_MISSED']:
                self._master_stat['CONNECTED'] = False
                logger.error('Maximum Master Keep-Alives Missed -- De-registering the Master')
            
            # Update our stats before we move on...
            self._master_stat['KEEP_ALIVES_SENT'] += 1
            self._master_stat['KEEP_ALIVES_OUTSTANDING'] += 1
            
        else:
            # This is bad. If we get this message, we need to reset the state and try again
            logger.error('->> (%s) Master in UNKOWN STATE:%s:%s', self._network, self._master_sock)
            self._master_stat['CONNECTED'] == False
        
        
        # If the master is connected and we don't have a peer-list yet....
        #
        if  ((self._master_stat['CONNECTED'] == True) and (self._master_stat['PEER_LIST'] == False)):
            # Ask the master for a peer-list
            peer_list_req_packet = self.hashed_packet(self._local['AUTH_KEY'], self.PEER_LIST_REQ_PKT)
            self.transport.write(peer_list_req_packet, (self._master_sock))


        # If we do have a peer-list, we need to register with the peers and send keep-alives...
        #
        if (self._master_stat['PEER_LIST'] == True):
            # Iterate the list of peers... so we do this for each one.
            for peer in (self._peers):
                # We will show up in the peer list, but shouldn't try to talk to ourselves.
                if (peer['RADIO_ID'] == self._local_id):
                    continue
                # If we haven't registered to a peer, send a registration
                if peer['STATUS']['CONNECTED'] == False:
                    peer_reg_packet = self.hashed_packet(self._local['AUTH_KEY'], self.PEER_REG_REQ_PKT)
                    self.transport.write(peer_reg_packet, (peer['IP'], peer['PORT']))
                    print
                # If we have registered with the peer, then send a keep-alive
                elif peer['STATUS']['CONNECTED'] == True:
                    peer_alive_req_packet = self.hashed_packet(self._local['AUTH_KEY'], self.PEER_ALIVE_REQ_PKT)
                    self.transport.write(peer_alive_req_packet, (peer['IP'], peer['PORT']))
                    
                    # If we have a keep-alive outstanding by the time we send another, mark it missed.
                    if peer['STATUS']['KEEP_ALIVES_OUTSTANDING'] > 0:
                        peer['STATUS']['KEEP_ALIVES_MISSED'] += 1
                    
                    # If we have missed too many keep-alives, de-register the peer and start over.
                    if peer['STATUS']['KEEP_ALIVES_OUTSTANDING'] >= self._local['MAX_MISSED']:
                        peer['STATUS']['CONNECTED'] = False
                        self._peer_list.remove(peer['RADIO_ID']) # Remove the peer from the simple list FIRST
                        self._peers.remove(peer)                 # Becuase once it's out of the dictionary, you can't use it for anything else.
                        logger.error('Maximum Peer Keep-Alives Missed -- De-registering the Peer: %s', peer)
                    
                    # Update our stats before moving on...
                    peer['STATUS']['KEEP_ALIVES_SENT'] += 1
                    peer['STATUS']['KEEP_ALIVES_OUTSTANDING'] += 1
    
    
    # For public display of information, etc. - anything not part of internal logging/diagnostics
    #
    def _notify_event(self, network, event, info):
        """
            Used internally whenever an event happens that may be useful to notify the outside world about.
            Arguments:
                network: string, network name to look up in config
                event:   string, basic description
                info:    dict, in the interest of accomplishing as much as possible without code changes.
                         The dict will typically contain a peer_id so the origin of the event is known.
        """
        pass
    
    
#************************************************
#     RECEIVED DATAGRAM - ACT IMMEDIATELY!!!
#************************************************

    # Actions for recieved packets by type: For every packet recieved, there are some things that we need to do:
    #   Decode some of the info
    #   Check for auth and authenticate the packet
    #   Strip the hash from the end... we don't need it anymore
    #
    # Once they're done, we move on to the proccessing or callbacks for each packet type.
    #
    def datagramReceived(self, data, (host, port)):
        _packettype = data[0:1]
        _peerid     = data[1:5]
        
        # Authenticate the packet
        if self.validate_auth(self._local['AUTH_KEY'], data) == False:
            logger.warning('(%s) AuthError: IPSC packet failed authentication. Type %s: Peer ID: %s', self._network, binascii.b2a_hex(_packettype), int(binascii.b2a_hex(_peerid), 16))
            return
            
        # Strip the hash, we won't need it anymore
        data = strip_hash(data)

        # Packets types that must be originated from a peer (including master peer)
        if (_packettype in ANY_PEER_REQUIRED):
            if not(valid_master(self._network, _peerid) == False or valid_peer(self._peer_list, _peerid) == False):
                logger.warning('(%s) PeerError: Peer not in peer-list: %s', self._network, int(binascii.b2a_hex(_peerid), 16))
                return
                
            # User, as in "subscriber" generated packets - a.k.a someone trasmitted
            if (_packettype in USER_PACKETS):
                # Extract commonly used items from the packet header
                _src_sub    = data[6:9]
                _dst_sub    = data[9:12]
                _call       = int_id(data[17:18])
                _ts         = bool(_call & TS_CALL_MSK)
                _end        = bool(_call & END_MSK)

                # User Voice and Data Call Types:
                if (_packettype == GROUP_VOICE):
                    self._notify_event(self._network, 'group_voice', {'peer_id': int(binascii.b2a_hex(_peerid), 16)})
                    self.group_voice(self._network, _src_sub, _dst_sub, _ts, _end, _peerid, data)
                    return
            
                elif (_packettype == PVT_VOICE):
                    self._notify_event(self._network, 'private_voice', {'peer_id': int(binascii.b2a_hex(_peerid), 16)})
                    self.private_voice(self._network, _src_sub, _dst_sub, _ts, _end, _peerid, data)
                    return
                    
                elif (_packettype == GROUP_DATA):
                    self._notify_event(self._network, 'group_data', {'peer_id': int(binascii.b2a_hex(_peerid), 16)})
                    self.group_data(self._network, _src_sub, _dst_sub, _ts, _end, _peerid, data)
                    return
                    
                elif (_packettype == PVT_DATA):
                    self._notify_event(self._network, 'private_voice', {'peer_id': int(binascii.b2a_hex(_peerid), 16)})
                    self.private_data(self._network, _src_sub, _dst_sub, _ts, _end, _peerid, data)
                    return
                return
                
            # Other peer-required types that we don't do much or anything with yet   
            elif (_packettype == XCMP_XNL):
                self.xcmp_xnl(self._network, data)
                return
            
            elif (_packettype == CALL_CTL_1):
                self.call_ctl_1(self._network, data)
                return
                
            elif (_packettype == CALL_CTL_2):
                self.call_ctl_2(self._network, data)
                return
                
            elif (_packettype == CALL_CTL_3):
                self.call_ctl_3(self._network, data)
                return
                
            # Connection maintenance packets that fall into this category
            elif (_packettype == DE_REG_REQ):
                de_register_peer(self._network, _peerid)
                logger.warning('<<- (%s) Peer De-Registration Request From:%s:%s', self._network, host, port)
                return
            
            elif (_packettype == DE_REG_REPLY):
                logger.warning('<<- (%s) Peer De-Registration Reply From:%s:%s', self._network, host, port)
                return
                
            elif (_packettype == RPT_WAKE_UP):
                logger.warning('<<- (%s) Repeater Wake-Up Packet From:%s:%s', self._network, host, port)
                return
            return


        # Packets types that must be originated from a peer
        if (_packettype in PEER_REQUIRED):
            if valid_peer(self._peer_list, _peerid) == False:
                logger.warning('(%s) PeerError: Peer %s not in peer-list: %s', self._network, int(binascii.b2a_hex(_peerid), 16), self._peer_list)
                return
            
            # Packets we send...
            if (_packettype == PEER_ALIVE_REQ):
                # Generate a hashed paket from our template and send it.
                peer_alive_reply_packet = self.hashed_packet(self._local['AUTH_KEY'], self.PEER_ALIVE_REPLY_PKT)
                self.transport.write(peer_alive_reply_packet, (host, port))
                return
                                
            elif (_packettype == PEER_REG_REQ):
                peer_reg_reply_packet = self.hashed_packet(self._local['AUTH_KEY'], self.PEER_REG_REPLY_PKT)
                self.transport.write(peer_reg_reply_packet, (host, port))
                return
                
            # Packets we receive...
            elif (_packettype == PEER_ALIVE_REPLY):
                for peer in self._config['PEERS']:
                    if peer['RADIO_ID'] == _peerid:
                        peer['STATUS']['KEEP_ALIVES_OUTSTANDING'] = 0
                return

            elif (_packettype == PEER_REG_REPLY):
                for peer in self._config['PEERS']:
                    if peer['RADIO_ID'] == _peerid:
                        peer['STATUS']['CONNECTED'] = True
                return
            return
        
        
        # Packets types that must be originated from a Master
        # Packets we receive...
        if (_packettype in MASTER_REQUIRED):
            if valid_master(self._network, _peerid) == False:
                logger.warning('(%s) PeerError: Master %s is invalid: %s', self._network, int(binascii.b2a_hex(_peerid), 16), self._peer_list)
                return
                
            if (_packettype == MASTER_ALIVE_REPLY):
                # This action is so simple, it doesn't require a callback function, master is responding, we're good.
                self._master_stat['KEEP_ALIVES_OUTSTANDING'] = 0
                return
            
            elif (_packettype == PEER_LIST_REPLY):
                NETWORK[self._network]['MASTER']['STATUS']['PEER_LIST'] = True
                if len(data) > 18:
                    self._peer_list = process_peer_list(data, self._network, self._peer_list)
                return
            return
            
        
        # When we hear from the maseter, record it's ID, flag that we're connected, and reset the dead counter.
        elif (_packettype == MASTER_REG_REPLY):
            self._master['RADIO_ID'] = _peerid
            self._master_stat['CONNECTED'] = True
            self._master_stat['KEEP_ALIVES_OUTSTANDING'] = 0
            return
        
        # We know about these types, but absolutely don't take an action
        elif (_packettype == MASTER_REG_REQ):
            # We can't operate as a master as of now, so we should never receive one of these.
            # logger.debug('<<- (%s) Master Registration Packet Recieved', self._network)
            return 
            
        # If there's a packet type we don't know aobut, it should be logged so we can figure it out and take an appropriate action!    
        else:
            self.unknown_message(self._network, _packettype, _peerid, data)
            return


#************************************************
#     Derived Class
#       used in the rare event of an
#       unauthenticated IPSC network.
#************************************************

class UnauthIPSC(IPSC):
    
    # There isn't a hash to build, so just return the data
    #
    def hashed_packet(self, _key, _data):
        return (_data)    
    
    # Everything is validated, so just return True
    #
    def validate_auth(self, _key, _data):
        return True
    

#************************************************
#      MAIN PROGRAM LOOP STARTS HERE
#************************************************

if __name__ == '__main__':
    networks = {}
    for ipsc_network in NETWORK:
        if (NETWORK[ipsc_network]['LOCAL']['ENABLED']):
            if NETWORK[ipsc_network]['LOCAL']['AUTH_ENABLED'] == True:
                networks[ipsc_network] = IPSC(ipsc_network)
            else:
                networks[ipsc_network] = UnauthIPSC(ipsc_network)
            reactor.listenUDP(NETWORK[ipsc_network]['LOCAL']['PORT'], networks[ipsc_network])
    reactor.run()