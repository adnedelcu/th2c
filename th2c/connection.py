import functools
import logging
import socket
import ssl
import traceback

import h2.config
import h2.connection
import h2.errors
import h2.events
import h2.exceptions
import h2.settings
from tornado import stack_context
from tornado import log as tornado_log
from tornado.httpclient import HTTPError
from tornado.ioloop import IOLoop

logger = tornado_log.gen_log

log = logging.getLogger(__name__)


class HTTP2ClientConnection(object):

    def __init__(self, host, port, tcp_client, on_connection_ready=None, on_connection_closed=None, connect_timeout=None):
        """

        :param host:
        :param port:
        :param tcp_client:
        :type tcp_client: tornado.tcpclient.TCPClient
        :param on_connection_ready:
        :param on_connection_closed:
        :param connect_timeout:
        """
        self.host = host
        self.port = port

        self.tcp_client = tcp_client
        self.on_connection_ready = on_connection_ready
        self.on_connection_closed = on_connection_closed

        self._is_connected = False
        self.timed_out = False

        self.stream = None

        self.connect_timeout = connect_timeout
        self._connect_timeout_t = None

        self.h2conn = None

        self._ongoing_streams = dict()

        self.event_handlers = dict()

        self.ssl_context = None
        self.parse_ssl_opts()

    def parse_ssl_opts(self):
        if self.port != 443:
            return

        ssl_context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
        ssl_context.options |= (
            ssl.OP_NO_TLSv1 | ssl.OP_NO_TLSv1_1 | ssl.OP_NO_COMPRESSION
        )
        ssl_context.set_ciphers("ECDHE+AESGCM")
        ssl_context.set_alpn_protocols(["h2"])

        self.ssl_context = ssl_context

    def connect(self):
        # don't try to connect twice,
        # it seems we are already waiting for a connection
        if self._connect_timeout_t:
            log.warning("Tried to connect while waiting for a connection!")
            return

        self.timed_out = False
        start_time = IOLoop.current().time()

        # set the connection timeout
        self._connect_timeout_t = IOLoop.current().add_timeout(
            start_time + self.connect_timeout, self.on_timeout
        )

        #  connect the tcp client, passing self.on_connect as callback
        with stack_context.ExceptionStackContext(functools.partial(self.on_error, "during connection")):
            self.tcp_client.connect(
                self.host, self.port, af=socket.AF_UNSPEC,
                ssl_options=self.ssl_context,  # self.ssl_options,
                callback=self.on_connect
            )

    @property
    def is_connected(self):
        return self._is_connected

    def on_connect(self, io_stream):
        log.info(["IOStream opened", io_stream])
        if self.timed_out:
            log.info("IOStream should close, we timed out")
            io_stream.close()
            return

        self._is_connected = True

        # remove the connection timeout
        IOLoop.current().remove_timeout(self._connect_timeout_t)
        self._connect_timeout_t = None

        self.stream = io_stream
        self.stream.set_nodelay(True)

        # set the close callback
        self.stream.set_close_callback(
            functools.partial(self.on_close, io_stream.error)
        )

        # initialize the connection
        self.h2conn = h2.connection.H2Connection(
            h2.config.H2Configuration(client_side=True)
        )

        # initiate the h2 connection
        self.h2conn.initiate_connection()

        # disable server push
        self.h2conn.update_settings({h2.settings.SettingCodes.ENABLE_PUSH: 0})

        # set the stream reading callback
        with stack_context.ExceptionStackContext(functools.partial(self.on_error, "during read")):
            self.stream.read_bytes(
                num_bytes=65535,
                streaming_callback=self.data_received,
                callback=self.data_received
            )

        self.flush()

        IOLoop.instance().add_callback(self.on_connection_ready)

    def on_close(self, reason):
        log.info(["IOStream closed with reason", reason])
        # cleanup
        self._is_connected = False
        self.h2conn = None

        if self.stream:
            try:
                self.stream.close()
            except:
                log.error("Error trying to close stream", exc_info=True)
            finally:
                self.stream = None

        # callback connection closed
        self.on_connection_closed(reason)

    def on_timeout(self):
        log.info(
            "HTTP2ClientConnection timed out after {}".format(
                self.connect_timeout
            )
        )
        self.timed_out = True
        self._connect_timeout_t = False
        self.on_close(HTTPError(599))

    def on_error(self, phase, typ, val, tb):
        log.error(
            ["HTTP2ClientConnection error ", phase, typ, val, traceback.format_tb(tb)]
        )
        self.on_close(val)

    def data_received(self, data):
        log.info(["Received data on IOStream", len(data)])
        try:
            events = self.h2conn.receive_data(data)
            log.info(["Events to process", events])
            if events:
                self.process_events(events)
        except:
            log.info(
                "Could not process events received on the HTTP/2 connection",
                exc_info=True
            )

    def process_events(self, events):
        """
        Processes events received on the HTTP/2 connection and
        dispatches them to their corresponding HTTPStreams.
        """
        recv_streams = dict()

        # if RemoteSettingsChanged is received, we should flush the connection
        # to ACK the new settings
        settings_updated = False

        connection_terminated = False

        for event in events:
            log.info(["PROCESSING EVENT", event])
            stream_id = getattr(event, 'stream_id', None)

            if isinstance(event, h2.events.RemoteSettingsChanged):
                settings_updated = True
            elif isinstance(event, h2.events.ConnectionTerminated):
                connection_terminated = True
            elif isinstance(event, h2.events.DataReceived):
                recv_streams[stream_id] = recv_streams.get(stream_id, 0) + event.flow_controlled_length
            if stream_id and stream_id in self._ongoing_streams:
                stream = self._ongoing_streams[stream_id]
                with stack_context.ExceptionStackContext(stream.handle_exception):
                    stream.handle_event(event)
            elif not stream_id:
                log.warning(
                    ["Received event for connection!", event]
                )
            else:
                log.warning(
                    ["Received event for unregistered stream", event]
                )

            if event in self.event_handlers:
                for ev_handler in self.event_handlers[event]:
                    ev_handler(event)

        recv_connection = 0
        for stream_id, num_bytes in recv_streams.iteritems():
            if not num_bytes:
                continue
            recv_connection += num_bytes

            try:
                log.info("Trying to increment flow control window for stream {} with {}".format(stream_id, num_bytes))
                self.h2conn.increment_flow_control_window(num_bytes, stream_id)
            except h2.exceptions.StreamClosedError:
                # TODO: maybe cleanup stream?
                log.warning("Tried to increment flow control window for closed stream")

        if recv_connection:
            log.info("Incrementing window flow control")
            self.h2conn.increment_flow_control_window(recv_connection)
        if recv_connection or settings_updated:
            self.flush()

    def begin_stream(self, stream):
        stream_id = self.h2conn.get_next_available_stream_id()
        self._ongoing_streams[stream_id] = stream
        return stream_id

    def end_stream(self, stream):
        del self._ongoing_streams[stream.stream_id]

    def flush(self):
        data_to_send = self.h2conn.data_to_send()
        if data_to_send:
            log.info(["Flushing data to IOStream", len(data_to_send)])
            self.stream.write(data_to_send)

    def add_event_handler(self, event, handler):
        self.event_handlers.get(event, set()).add(handler)

    def remove_event_handler(self, event, handler):
        self.event_handlers.get(event, set()).remove(handler)