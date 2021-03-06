# ovirt-imageio
# Copyright (C) 2018 Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.

import errno
import logging
import socket

from wsgiref import simple_server
from six.moves import socketserver

log = logging.getLogger("wsgi")


# Taken from asyncore.py. Treat these as expected error when reading or writing
# to client connection.
_DISCONNECTED = frozenset((
    errno.ECONNRESET,
    errno.ENOTCONN,
    errno.ESHUTDOWN,
    errno.ECONNABORTED,
    errno.EPIPE
))


class WSGIServer(socketserver.ThreadingMixIn,
                 simple_server.WSGIServer):
    """
    Threaded WSGI HTTP server.
    """
    daemon_threads = True


class WSGIRequestHandler(simple_server.WSGIRequestHandler):
    """
    WSGI request handler using HTTP/1.1.
    """

    protocol_version = "HTTP/1.1"

    # Avoids possible delays when sending very small response.
    disable_nagle_algorithm = True

    def address_string(self):
        """
        Override to avoid slow and unneeded name lookup.
        """
        return self.client_address[0]

    def handle_one_request(self):
        """
        WSGIRequestHandler does not implement this, and instead implements
        handle().

        This code was copied from BaseHTTPServer.BaseHTTPRequestHandler
        and modified to dispatch the request to ServerHandler, based on
        WSGIRequestHandler.handle(). Logging and error handling were
        also improved. The rest of the code should be kept as is, to
        make it easy to backport fixes from Python.

        See the original version here:
        https://github.com/python/cpython/blob/2.7/Lib/BaseHTTPServer.py
        https://github.com/python/cpython/blob/master/Lib/http/server.py
        """
        try:
            self.raw_requestline = self.rfile.readline(65537)
            if len(self.raw_requestline) > 65536:
                log.warning("Request line too long: %d > 65536, closing "
                            "connection",
                            len(self.raw_requestline))
                self.requestline = ''
                self.request_version = ''
                self.command = ''
                self.send_error(414)
                return

            if not self.raw_requestline:
                log.debug("Empty request line, client disconnected")
                self.close_connection = 1
                return

            if not self.parse_request():
                return

            handler = ServerHandler(
                self.rfile,
                self.wfile,
                self.get_stderr(),
                self.get_environ())

            handler.request_handler = self  # back reference for logging
            handler.http_version = self.protocol_version.split("/")[1]

            handler.run(self.server.get_app())

        except socket.timeout as e:
            log.warning("Timeout reading or writing to socket: %s", e)
            self.close_connection = 1
        except socket.error as e:
            if e[0] not in _DISCONNECTED:
                raise
            log.debug("Client disconnected: %s", e)
            self.close_connection = 1

    def handle(self):
        """
        Override to handle multiple requests per connection.

        Copied from BaseHTTServer.BaseHTTPRequestHandler.
        """
        self.close_connection = 1

        self.handle_one_request()
        while not self.close_connection:
            self.handle_one_request()

    def log_message(self, format, *args):
        """
        Override to avoid unwanted logging to stderr.
        """


class ServerHandler(simple_server.ServerHandler):

    def run(self, application):
        """
        Run WSGI application.

        Override to ensure that close is called exactly once.

        Based on wsgiref/handlers.BaseHandler.run(), modifying error handling.
        """
        try:
            self.setup_environ()
            self.result = application(self.environ, self.start_response)
            self.finish_response()
        except Exception:
            self.handle_error()
        finally:
            self.close()

    def finish_response(self):
        """
        Send any iterable data.

        Override to remove double closing; we close now in run().

        Based on wsgiref/handlers.BaseHandler.finish_response(), removing
        try-finally block closing the handler.
        """
        if not self.result_is_file() or not self.sendfile():
            for data in self.result:
                self.write(data)
            self.finish_content()

    def handle_error(self):
        """
        Log current error, and send error output to client if possible.

        Override to ensure that self.result is closed before replace it with
        the standard error output. If we don't replace result, it will closed
        in close().

        Also use our logger to the get the traceback in our log.

        Based on wsgiref/handlers.BaseHandler.handle_error(), changing logging
        and closing result before replacing it.
        """
        log.exception("Unhandled error processing request")
        if not self.headers_sent:
            if hasattr(self.result, "close"):
                log.debug("Closing result")
                self.result.close()

            self.result = self.error_output(self.environ, self.start_response)
            self.finish_response()

    def write(self, data):
        """
        Override to allow writing buffer object.

        Based on wsgiref/handlers.BaseHandler.write(), removing the check for
        StringType.
        """
        if not self.status:
            raise AssertionError("write() before start_response()")

        elif not self.headers_sent:
            # Before the first output, send the stored headers
            self.bytes_sent = len(data)    # make sure we know content-length
            self.send_headers()
        else:
            self.bytes_sent += len(data)

        self._write(data)
        self._flush()

    def close(self):
        """
        Extend to close the connection after failures.

        If the request failed but it has a content-length header, there
        is a chance that some of the body was not read yet. Since we
        cannot recover from this, the only thing we can do is closing
        the connection.
        """
        log.debug("Closing handler")

        if self.status:
            status = int(self.status[:3])
            if status >= 400 and self.environ["CONTENT_LENGTH"]:
                log.debug("Request failed, possibly before reading the "
                          "entire request body, closing connection")
                self.request_handler.close_connection = 1

        simple_server.ServerHandler.close(self)
