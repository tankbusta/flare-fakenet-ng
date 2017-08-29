import socket
import SocketServer
import threading
import sys
import glob
import time
import importlib
import Queue
import select
import logging
import ssl
from OpenSSL import SSL
from ssl_utils import ssl_detector 
import hexdump
import listeners
from listeners import *

BUF_SZ = 1024
IP = '0.0.0.0'

class ProxyListener():


    def __init__(
            self, 
            config={}, 
            name ='ProxyListener', 
            logging_level=logging.DEBUG, 
            ):

        self.logger = logging.getLogger(name)
        self.logger.setLevel(logging_level)

        self.config = config
        self.name = name
        self.server = None

        self.logger.info('Starting...')

        self.logger.debug('Initialized with config:')
        for key, value in config.iteritems():
            self.logger.debug('  %10s: %s', key, value)

    def start(self):

        proto = self.config.get('protocol').upper()
        if proto != None:

            if proto == 'TCP':

                self.logger.debug('Starting TCP ...')

                self.server = ThreadedTCPServer((IP, 
                    int(self.config.get('port'))), ThreadedTCPRequestHandler)
            
            elif proto == 'UDP':

                self.logger.debug('Starting UDP ...')

                self.server = ThreadedUDPServer((IP, 
                    int(self.config.get('port'))), ThreadedUDPRequestHandler)

            else:
                self.logger.error('Unknown protocol %s' % proto)
                return

        else:
            self.logger.error('Protocol is not defined')
            return
   
        self.server.config = self.config
        self.server.logger = self.logger
        self.server.running_listeners = None
        self.server.diverter = None
        self.server_thread = threading.Thread(
                target=self.server.serve_forever)
        self.server_thread.daemon = True
        self.server_thread.start()
        server_ip, server_port = self.server.server_address
        self.logger.info("%s Server(%s:%d) thread: %s" % (proto, server_ip, 
            server_port, self.server_thread.name))

    def stop(self):
        self.logger.debug('Stopping...')
        if self.server:
            self.server.shutdown()
            self.server.server_close()

    def acceptListeners(self, listeners):
        self.server.listeners = listeners

    def acceptDiverter(self, diverter):
        self.server.diverter = diverter
        
class ThreadedTCPClientSocket(threading.Thread):


    def __init__(self, ip, port, listener_q, remote_q, config, log):

        super(ThreadedTCPClientSocket, self).__init__()
        self.ip = ip
        self.port = int(port)
        self.listener_q = listener_q
        self.remote_q = remote_q
        self.config = config
        self.logger = log
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    def run(self):

        try:
            self.sock.connect((self.ip, self.port))
            while True:
                readable, writable, exceptional = select.select([self.sock], 
                        [], [], .001)
                if not self.remote_q.empty():
                    data = self.remote_q.get()
                    self.sock.send(data)
                if readable:
                    data = self.sock.recv(BUF_SZ)
                    if data:
                        self.listener_q.put(data)
                    else:
                        self.sock.close()
                        exit(1)
        except Exception as e:
            self.logger.debug('Listener socket exception %s' % e.message)

        
class ThreadedUDPClientSocket(threading.Thread):


    def __init__(self, ip, port, listener_q, remote_q, config, log):

        super(ThreadedUDPClientSocket, self).__init__()
        #listener port, ip
        self.ip = ip
        self.port = int(port)
        self.listener_q = listener_q
        self.remote_q = remote_q
        self.config = config
        self.logger = log
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def run(self):

        try:
            self.sock.bind((self.ip, self.port))
            while True:
                readable, writable, exceptional = select.select([self.sock], 
                        [], [], .001)
                if not self.remote_q.empty():
                    data = self.remote_q.get()
                    self.sock.sendto(data, (ip, port))
                if readable:
                    data = self.sock.recv(BUF_SZ)
                    if data:
                        self.listener_q.put(data)
                    else:
                        self.sock.close()
                        exit(1)
        except Exception as e:
            self.logger.debug('Listener socket exception %s' % e.message)

class ThreadedTCPServer(SocketServer.ThreadingMixIn, SocketServer.TCPServer):
    pass

class ThreadedUDPServer(SocketServer.ThreadingMixIn, SocketServer.UDPServer):
    pass

def get_top_listener(config, data, listeners, diverter, orig_src_ip, 
        orig_src_port, proto):
    

    top_listener = None
    top_confidence = 0
    dport = diverter.getOriginalDestPort(orig_src_ip, orig_src_port, proto)

    for listener in listeners:
  
        try:
            confidence = listener.taste(data, dport)
            if confidence > top_confidence:
                top_confidence = confidence
                top_listener = listener
        except:
            # Exception occurs if taste() is not implemented for this listener
            pass
    
    return top_listener

class ThreadedTCPRequestHandler(SocketServer.BaseRequestHandler):

    
    def handle(self):

        remote_sock = self.request
        # queue for data received from the listener
        listener_q = Queue.Queue()
        # queue for data received from remote
        remote_q = Queue.Queue()
        data = None

        ssl_remote_sock = None
        ssl_config = { 
                'certfile': 'listeners/ssl_utils/server.pem', 
                'keyfile': 'listeners/ssl_utils/privkey.pem',
                'ssl_version' : ssl.PROTOCOL_SSLv23 }

        try:
            data = remote_sock.recv(BUF_SZ, socket.MSG_PEEK)
            self.server.logger.debug('Received data\n%s' % hexdump.hexdump(data, 
                result='return'))
        except Exception as e:
            self.server.logger.info('recv() error: %s' % e.message)

        if data:

            if ssl_detector.looks_like_ssl(data):
                self.server.logger.debug('SSL detected')
                ssl_remote_sock = ssl.wrap_socket(
                        remote_sock, 
                        server_side=True, 
                        do_handshake_on_connect=True,
                        certfile=ssl_config['certfile'], 
                        ssl_version=ssl_config['ssl_version'],
                        keyfile=ssl_config['keyfile'] )
                data = ssl_remote_sock.recv(BUF_SZ)
            
            orig_src_ip = self.client_address[0]
            orig_src_port = self.client_address[1]

            top_listener = get_top_listener(self.server.config, data, 
                    self.server.listeners, self.server.diverter, 
                    orig_src_ip, orig_src_port, 'TCP')

            if top_listener:
                self.server.logger.debug('Likely listener: %s' % 
                        top_listener.name)
                listener_sock = ThreadedTCPClientSocket('localhost', 
                        top_listener.port, listener_q, remote_q, 
                        self.server.config, self.server.logger)
                listener_sock.setDaemon(True)
                listener_sock.start()
                remote_sock.setblocking(0)

                # ssl has no 'peek' option, so we need to process the first 
                # packet that is already consumed from the socket
                if ssl_remote_sock:
                    ssl_remote_sock.setblocking(0)
                    remote_q.put(data)
                
                while True:
                    readable, writable, exceptional = select.select(
                            [remote_sock], [], [], .001)
                    if readable:
                        try:
                            if ssl_remote_sock:
                                data = ssl_remote_sock.recv(BUF_SZ)
                            else:
                                data = remote_sock.recv(BUF_SZ)
                            if data:
                                remote_q.put(data)
                            else:
                                self.server.logger.debug(
                                        'Closing remote socket connection')
                                return
                        except Exception as e:
                            self.server.logger.debug('Remote Connection terminated')
                            return
                    if not listener_q.empty():
                        data = listener_q.get()
                        if ssl_remote_sock:
                            ssl_remote_sock.send(data)
                        else:
                            remote_sock.send(data)

class ThreadedUDPRequestHandler(SocketServer.BaseRequestHandler):

    
    def handle(self):

        data = self.request[0]
        remote_sock = self.request[1]

        self.server.logger.debug('Received UDP packet from %s.' % 
                self.client_address[0])

        if data:

            self.server.logger.debug('Packet data\n%s' % hexdump.hexdump(data, 
                result='return'))

            orig_src_ip = self.client_address[0]
            orig_src_port = self.client_address[1]

            top_listener = get_top_listener(self.server.config, data, 
                    self.server.listeners, self.server.diverter, 
                    orig_src_ip, orig_src_port, 'UDP')

            if top_listener:
                self.server.logger.debug('Likely listener: %s' % 
                        top_listener.name)
                listener_sock = ThreadedUDPClientSocket('localhost', 
                        top_listener.port, listener_q, remote_q, 
                        self.server.config, self.server.logger)
                listener_sock.setDaemon(True)
                listener_sock.start()
                remote_sock.setblocking(0)

                # no peek option so process the data already recd
                remote_q.put(data)

                while True:
                    readable, writable, exceptional = select.select(
                            [remote_sock], [], [], .001)
                    if readable:
                        try:
                            data = remote_sock.recv(BUF_SZ)
                            if data:
                                remote_q.put(data)
                            else:
                                self.server.logger.debug(
                                        'Closing remote socket connection')
                                return
                        except Exception as e:
                            self.server.logger.debug('Remote Connection terminated')
                            return
                    if not listener_q.empty():
                        data = listener_q.get()
                        remote_sock.send(data)

        else:
            self.server.logger.debug('No packet data')

def main():

    logging.basicConfig(format='%(asctime)s [%(name)15s] %(message)s', 
            datefmt='%m/%d/%y %I:%M:%S %p', level=logging.DEBUG)
    global listeners
    listeners = load_plugins()

    TCP_server = ThreadedTCPServer((IP, int(sys.argv[1])), 
            ThreadedTCPRequestHandler)
    TCP_server_thread = threading.Thread(target=TCP_server.serve_forever)
    TCP_server_thread.daemon = True
    TCP_server_thread.start()
    tcp_server_ip, tcp_server_port = TCP_server.server_address
    logger.info("TCP Server(%s:%d) thread: %s" % (tcp_server_ip, 
        tcp_server_port, TCP_server_thread.name))

    try:
        while True:
            time.sleep(.001)
    except Exception as e:
        logger.info(e)
        TCP_server.shutdown()
    finally:
        logger.info('Closing ProxyListener')
        exit(1)
    logger.info('Exiting')
    TCP_server.shutdown()

if __name__ == '__main__':
    main()
