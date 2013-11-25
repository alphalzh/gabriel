#!/usr/bin/env python
#
# Cloudlet Infrastructure for Mobile Computing
#
#   Author: Kiryong Ha <krha@cmu.edu>
#
#   Copyright (C) 2011-2013 Carnegie Mellon University
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
#

import sys
sys.path.insert(0, "../../")
from control.protocol import Protocol_client
from control.protocol import Protocol_measurement
from control import log as logging
from control.upnp_client import UPnPClient
from control.config import Const
from control.config import ServiceMeta

from optparse import OptionParser
import os
import tempfile
import traceback
import threading
import socket
import select
import struct
import json
import Queue
import time
import pprint


LOG = logging.getLogger(__name__)
upnp_client = UPnPClient()
CONST = Const
SERVICE_META = ServiceMeta


def get_service_list(argv):
    settings, args = process_command_line(sys.argv[1:])
    service_list = None
    if settings.address is None:
        upnp_client.start()
        upnp_client.join()
        service_list = upnp_client.service_list
    else:
        import urllib2
        ip_addr, port = settings.address.split(":", 1)
        port = int(port)
        meta_stream = urllib2.urlopen("http://%s:%d/" % (ip_addr, port))
        meta_raw = meta_stream.read()
        service_list = json.loads(meta_raw)
    pstr = pprint.pformat(service_list)
    LOG.info("Gabriel Server :")
    LOG.info(pstr)
    return service_list


def process_command_line(argv):
    VERSION = 'gabriel discovery'
    DESCRIPTION = "Gabriel service discovery"

    parser = OptionParser(usage='%prog [option]', version=VERSION,
            description=DESCRIPTION)

    parser.add_option(
            '-s', '--address', action='store', dest='address',
            help="(IP address:port number) of directory server")
    settings, args = parser.parse_args(argv)
    if len(args) >= 1:
        parser.error("invalid arguement")

    if hasattr(settings, 'address') and settings.address is not None:
        if settings.address.find(":") == -1:
            parser.error("Need address and port. Ex) 10.0.0.1:8081")
    return settings, args


class AppProxyError(Exception):
    pass

class AppProxyStreamingClient(threading.Thread):
    """
    This client will receive data from the control server as much as possible.
    And put the data into the queue, so that other thread can use the image
    """

    def __init__(self, control_addr, data_queue):
        self.stop = threading.Event()
        self.port_number = None
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.sock.connect(control_addr)
            LOG.info("Success to connect to %s" % str(control_addr))
        except socket.error as e:
            raise AppProxyError("Failed to connect to %s" % str(control_addr))
        self.data_queue = data_queue
        self.stop_file= tempfile.TemporaryFile(prefix="gabriel-threadfd-")
        threading.Thread.__init__(self, target=self.streaming)

    def streaming(self):
        LOG.info("Start getting data from the server")
        stopfd = self.stop_file.fileno()
        socket_fd = self.sock.fileno()
        input_list = [socket_fd, stopfd]
        except_list = [socket_fd, stopfd]
        try:
            while True:
                inputready, outputready, exceptready = \
                        select.select(input_list, [], except_list)
                for s in inputready:
                    if s == socket_fd:
                        self._handle_input_streaming()
                    if s == stopfd:
                        break
        except Exception as e:
            LOG.warning(traceback.format_exc())
            LOG.warning("%s" % str(e))
            LOG.warning("handler raises exception")
            LOG.warning("Server is disconnected unexpectedly")
        self.terminate()
        LOG.debug("Streaming thread terminated")

    def terminate(self):
        if self.stop_file is not None:
            os.write(self.stop_file.fileno(), "stop\n")
            self.stop_file.flush()
            self.stop_file.close()
            self.stop_file = None
        if self.sock is not None:
            self.sock.close()

    def _recv_all(self, sock, recv_size):
        data = ''
        while len(data) < recv_size:
            tmp_data = sock.recv(recv_size - len(data))
            if tmp_data == None or len(tmp_data) == 0:
                raise AppProxyError("Socket is closed")
            data += tmp_data
        return data

    def _handle_input_streaming(self):
        # receive data from control VM
        header_size = struct.unpack("!I", self._recv_all(self.sock, 4))[0]
        data_size = struct.unpack("!I", self._recv_all(self.sock, 4))[0]
        header = self._recv_all(self.sock, header_size)
        data = self._recv_all(self.sock, data_size)
        header_data = json.loads(header)

        # add header data for measurement
        #header_data[Protocol_measurement.JSON_KEY_APP_SENT_TIME] = time.time()

        try:
            if self.data_queue.full() is True:
                self.data_queue.get_nowait()
        except Queue.Empty as e:
            pass
        try:
            self.data_queue.put_nowait((header_data, data))
        except Queue.Full as e:
            pass


class AppProxyBlockingClient(threading.Thread):
    def __init__(self, control_addr):
        self.stop = threading.Event()
        self.port_number = None
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock.connect(control_addr)
        self.result_queue = Queue.Queue()
        threading.Thread.__init__(self, target=self.block_read)

    def block_read(self):
        LOG.info("Start getting data from the server")
        socket_fd = self.sock.fileno()
        input_list = [socket_fd]
        try:
            while(not self.stop.wait(0.01)):
                inputready, outputready, exceptready = \
                        select.select(input_list, [], [], 0)
                for s in inputready:
                    if s == socket_fd:
                        self._handle_video_streaming()
        except Exception as e:
            LOG.info(traceback.format_exc())
            LOG.info("%s" % str(e))
            LOG.info("handler raises exception")
            LOG.info("Server is disconnected unexpectedly")
        self.sock.close()

    def terminate(self):
        self.stop.set()
        if self.sock is not None:
            self.sock.close()

    def _recv_all(self, sock, recv_size):
        data = ''
        while len(data) < recv_size:
            tmp_data = sock.recv(recv_size - len(data))
            if tmp_data == None or len(tmp_data) == 0:
                raise AppProxyError("Socket is closed")
            data += tmp_data
        return data

    def handle_frame(self, data):
        pass

    def _handle_video_streaming(self):
        # receive data from control VM
        #LOG.info("receiving new image\n")
        header_size = struct.unpack("!I", self._recv_all(self.sock, 4))[0]
        data_size = struct.unpack("!I", self._recv_all(self.sock, 4))[0]
        header = self._recv_all(self.sock, header_size)
        data = self._recv_all(self.sock, data_size)
        header_data = json.loads(header)
        frame_id = header_data.get(Protocol_client.FRAME_MESSAGE_KEY, None)

        return_message = self.handle_frame(data)
        if return_message is None:
            return_message = dict()

        if len(return_message) > 0:
            if frame_id is not None:
                return_message[Protocol_client.FRAME_MESSAGE_KEY] = frame_id
            self.result_queue.put(json.dumps(return_message))


class AppProxyThread(threading.Thread):
    def __init__(self, data_queue, output_queue_list):
        self.data_queue = data_queue
        self.output_queue_list = output_queue_list
        self.stop = threading.Event()
        threading.Thread.__init__(self, target=self.run)

    def run(self):
        while(not self.stop.wait(0.001)):
            try:
                (header, data) = self.data_queue.get_nowait()
            except Queue.Empty as e:
                continue
            if header == None or data == None:
                continue

            result = self.handle(header, data)
            if result is not None:
                header[Protocol_client.RESULT_MESSAGE_KEY] = result
                for output_queue in self.output_queue_list:
                    output_queue.put(json.dumps(header))
        LOG.debug("App thread terminated")

    def handle(self, header, data):
        return None

    def terminate(self):
        self.stop.set()


class ResultpublishClient(threading.Thread):
    """
    This client will publish the algorithm result to all TCP server
    listed in publish_addr_list
    """

    def __init__(self, publish_addr_list, result_queue_list):
        self.stop = threading.Event()
        self.result_queue_list = result_queue_list
        self.publish_addr_list = list()
        for addr in publish_addr_list:
            (ip, port) = addr.split(":")
            # addr, port, socket, result_data_queue
            self.publish_addr_list.append((ip, int(port), None, None))
        threading.Thread.__init__(self, target=self.publish)

    def update_connection(self):
        for index, (addr, port, app_sock, result_queue) in enumerate(self.publish_addr_list):
            if app_sock is not None:
                continue
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                original_timeout = sock.gettimeout()
                sock.settimeout(1)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.connect((addr, port))
                sock.settimeout(original_timeout)
                # update
                result_queue = Queue.Queue()
                self.result_queue_list.append(result_queue)
                self.publish_addr_list[index] = (addr, port, sock, result_queue)
            except socket.error as e:
                LOG.info("Failed to connect to (%s, %d)" % (addr, port))
                pass

    def _get_all_socket_fd(self):
        fd_list = list()
        for (addr, port, app_sock, result_queue) in self.publish_addr_list:
            if app_sock is None:
                continue
            fd_list.append(app_sock.fileno())
        return fd_list

    def publish(self):
        self.update_connection()
        output_list = self._get_all_socket_fd()
        error_list = self._get_all_socket_fd()

        LOG.info("Start publishing data")
        try:
            while(not self.stop.wait(0.001)):
                inputready, outputready, exceptready = \
                        select.select([], output_list, error_list, 0.1)
                for s in inputready:
                    pass
                for s in outputready:
                    self._handle_result_output(s)
                for s in exceptready:
                    self._handle_error(s)
                time.sleep(0.001)
        except Exception as e:
            LOG.warning(traceback.format_exc())
            LOG.warning("%s" % str(e))
            LOG.warning("handler raises exception")
            LOG.warning("Server is disconnected unexpectedly")
        LOG.debug("Publish thread terminated")

    def terminate(self):
        self.stop.set()
        for index, (addr, port, app_sock, result_queue) in enumerate(self.publish_addr_list):
            if app_sock is None:
                continue
            self.result_queue_list.remove(result_queue)
            app_sock.close()

    def _handle_error(self, socket_fd):
        for index, (addr, port, app_sock, result_queue) in enumerate(self.publish_addr_list):
            if app_sock is None:
                continue
            if app_sock.fileno() == socket_fd:
                self.result_queue_list.remove(result_queue)
                del self.publish_addr_list[index]
                break

    def _handle_result_output(self, socket_fd):
        sending_socket = None
        output_queue = None
        for (addr, port, app_sock, result_queue) in self.publish_addr_list:
            if app_sock is None:
                continue
            if app_sock.fileno() == socket_fd:
                sending_socket = app_sock
                output_queue = result_queue
        if sending_socket is None:
            return

        while output_queue.empty() is False:
            return_data = output_queue.get()
            packet = struct.pack("!I%ds" % len(return_data),
                    len(return_data), return_data)
            sending_socket.sendall(packet)
            LOG.info("returning result: %s" % return_data)

if __name__ == "__main__":
    LOG.info("Start receiving data")
    try:
        control_addr = ("128.2.210.197", 10101)
        client = AppProxyBlockingClient(control_addr)
        client.start()
        client.isDaemon = True
        while True:
            time.sleep(1)
    except KeyboardInterrupt as e:
        LOG.info("user exits")
        client.terminate()
    except Exception as e:
        client.terminate()
