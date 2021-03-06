"""
This wsgi server is a single threaded, single process server that interleaves
the iterations of the wsgi apps - I could add a threadpool for blocking apps in
the future.

If you don't return iterators from apps and return lists you'll get, at most,
the performance of a server that processes requests sequentialy.

On the other hand this server has coroutine extensions that suppose to support
use of middleware in your application.

Example app with coroutine extensions:

.. sourcecode:: python

    def wait_app(environ, start_response):
      start_response('200 OK', [('Content-type','text/html')])
      yield "I'm waiting for some signal"
      yield environ['cogen'].core.events.WaitForSignal("abc", timeout=1)
      if isinstance(environ['cogen'].result, Exception):
        yield "Your time is up !"
      else:
        yield "Someone signaled me: %s" % environ['cogen'].result

* `environ['cogen'].core` is actualy a wrapper that sets
  `environ['cogen'].operation` with the called object and returns a empty
  string. This should penetrate most of the middleware - according to the wsgi
  spec, middleware should pass a empty string if it doesn't have anything to
  return on that specific iteration point, or, in other words, the length of the
  app iter returned by middleware should be at least that of the app.

* the wsigi server will set `environ['cogen'].result` with the result of the
  operation and `environ['cogen'].exception` with the details of the
  exception - if any: `(exc_type, exc_value, traceback_object)`.

HTTP handling code taken from the CherryPy WSGI server.
"""

# TODO: better application error reporting for the coroutine extensions

from __future__ import with_statement

__all__ = ['WSGIFileWrapper', 'WSGIServer', 'WSGIConnection', 'server_factory']

from contextlib import closing

import base64
import os
import re
import rfc822
import socket
import errno

try:
    from pywintypes import error as pywinerror
except ImportError:
    from socket import error as pywinerror

try:
  import cStringIO as StringIO
except ImportError:
  import StringIO
import sys
import time
import traceback
from urllib import unquote
from urlparse import urlparse
from traceback import format_exc

from cogen import core, __version__
from cogen.core import proactors, sockets, events
from cogen.core.util import priority
from cogen.core.sockets import SocketError, ConnectionClosed
from cogen.core.events import OperationTimeout
from cogen.core.coroutines import coroutine, local
from cogen.core.schedulers import Scheduler

import async

quoted_slash = re.compile("(?i)%2F")
useless_socket_errors = {}
for _ in ("EPIPE", "ETIMEDOUT", "ECONNREFUSED", "ECONNRESET",
      "EHOSTDOWN", "EHOSTUNREACH", "ENOTCONN",
      "WSAECONNABORTED", "WSAECONNREFUSED", "WSAECONNRESET",
      "WSAENETRESET", "WSAETIMEDOUT"):
  if _ in dir(errno):
    useless_socket_errors[getattr(errno, _)] = None
useless_socket_errors = useless_socket_errors.keys()
comma_separated_headers = ['ACCEPT', 'ACCEPT-CHARSET', 'ACCEPT-ENCODING',
  'ACCEPT-LANGUAGE', 'ACCEPT-RANGES', 'ALLOW', 'CACHE-CONTROL',
  'CONNECTION', 'CONTENT-ENCODING', 'CONTENT-LANGUAGE', 'EXPECT',
  'IF-MATCH', 'IF-NONE-MATCH', 'PRAGMA', 'PROXY-AUTHENTICATE', 'TE',
  'TRAILER', 'TRANSFER-ENCODING', 'UPGRADE', 'VARY', 'VIA', 'WARNING',
  'WWW-AUTHENTICATE']

class WSGIFileWrapper:
  def __init__(self, filelike, blocksize=8192):
    self.filelike = filelike
    self.blocksize = blocksize
    if hasattr(filelike,'close'):
      self.close = filelike.close

  def __getitem__(self, key):
    data = self.filelike.read(self.blocksize)
    if data:
      return data
    raise IndexError

class WSGIPathInfoDispatcher(object):
  """A WSGI dispatcher for dispatch based on the PATH_INFO.

  apps: a dict or list of (path_prefix, app) pairs.
  """

  def __init__(self, apps):
    try:
      apps = apps.items()
    except AttributeError:
      pass

    # Sort the apps by len(path), descending
    apps.sort()
    apps.reverse()

    # The path_prefix strings must start, but not end, with a slash.
    # Use "" instead of "/".
    self.apps = [(p.rstrip("/"), a) for p, a in apps]

  def __call__(self, environ, start_response):
    path = environ["PATH_INFO"] or "/"
    for p, app in self.apps:
      # The apps list should be sorted by length, descending.
      if path.startswith(p + "/") or path == p:
        environ = environ.copy()
        environ["SCRIPT_NAME"] = environ["SCRIPT_NAME"] + p
        environ["PATH_INFO"] = path[len(p):]
        return app(environ, start_response)

    start_response('404 Not Found', [('Content-Type', 'text/plain'),
                     ('Content-Length', '0')])
    return ['']

class WSGIConnection(object):
  connection_environ = {
    "wsgi.version": (1, 0),
    "wsgi.url_scheme": "http",
    "wsgi.multithread": False,
    "wsgi.multiprocess": False,
    "wsgi.run_once": False,
    "wsgi.errors": sys.stderr,
    "wsgi.input": None,
    "wsgi.file_wrapper": WSGIFileWrapper,
  }

  def __init__(self, sock, wsgi_app, environ, sendfile_timeout):
    self.conn = sock
    self.wsgi_app = wsgi_app
    self.server_environ = environ
    self.sendfile_timeout = sendfile_timeout
    self.conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    self.connfh = self.conn.makefile()

  def start_response(self, status, headers, exc_info = None):
    """WSGI callable to begin the HTTP response."""
    if self.started_response:
      if not exc_info:
        raise AssertionError("WSGI start_response called a second "
                   "time with no exc_info.")
      else:
        try:
          raise exc_info[0], exc_info[1], exc_info[2]
        finally:
          exc_info = None
    self.started_response = True
    self.status = status
    self.outheaders.extend(headers)

    return self.write_buffer.write

  def render_headers(self):
    hkeys = [key.lower() for key, value in self.outheaders]
    status = int(self.status[:3])
    if 'content-length' in hkeys:
        self.content_length = int(self.outheaders[hkeys.index('content-length')][1])
    if status == 413:
      # Request Entity Too Large. Close conn to avoid garbage.
      self.close_connection = True
    elif "content-length" not in hkeys:
      # "All 1xx (informational), 204 (no content),
      # and 304 (not modified) responses MUST NOT
      # include a message-body." So no point chunking.
      if status < 200 or status in (204, 205, 304):
        pass
      else:
        if self.response_protocol == 'HTTP/1.1':
          # Use the chunked transfer-coding
          self.chunked_write = True
          self.outheaders.append(("Transfer-Encoding", "chunked"))
        else:
          # Closing the conn is the only way to determine len.
          self.close_connection = True

    if "connection" not in hkeys:
      if self.response_protocol == 'HTTP/1.1':
        if self.close_connection:
          self.outheaders.append(("Connection", "close"))
      else:
        if not self.close_connection:
          self.outheaders.append(("Connection", "Keep-Alive"))

    if "date" not in hkeys:
      self.outheaders.append(("Date", rfc822.formatdate()))

    if "server" not in hkeys:
      self.outheaders.append(("Server", self.environ['SERVER_SOFTWARE']))

    buf = [self.environ['ACTUAL_SERVER_PROTOCOL'], " ", self.status, "\r\n"]
    try:
      buf += [k + ": " + v + "\r\n" for k, v in self.outheaders]
    except TypeError:
      if not isinstance(k, str):
        raise TypeError("WSGI response header key %r is not a string." % k)
      if not isinstance(v, str):
        raise TypeError("WSGI response header value %r is not a string." % v)
      else:
        raise
    buf.append("\r\n")
    return "".join(buf)

  def simple_response(self, status, msg=""):
    """Return a operation for writing simple response back to the client."""
    status = str(status)
    buf = ["%s %s\r\n" % (self.environ['ACTUAL_SERVER_PROTOCOL'], status),
         "Content-Length: %s\r\n" % len(msg),
         "Content-Type: text/plain\r\n"]

    if status[:3] == "413" and self.response_protocol == 'HTTP/1.1':
      # Request Entity Too Large
      self.close_connection = True
      buf.append("Connection: close\r\n")

    buf.append("\r\n")
    if msg:
      buf.append(msg)
    return sockets.SendAll(self.conn, "".join(buf))

  #~ from cogen.core.coroutines import debug_coroutine
  #~ @debug_coroutine
  @coroutine
  def run(self):
    """A bit bulky atm..."""
    self.close_connection = False

    try:
      while True:
        self.started_response = False
        self.status = ""
        self.outheaders = []
        self.sent_headers = False
        self.chunked_write = False
        self.write_buffer = StringIO.StringIO()
        self.content_length = None
        # Copy the class environ into self.
        ENVIRON = self.environ = self.connection_environ.copy()
        self.environ.update(self.server_environ)

        request_line = yield self.connfh.readline()
        if request_line == "\r\n":
          # RFC 2616 sec 4.1: "... it should ignore the CRLF."
          tolerance = 5
          while tolerance and request_line == "\r\n":
            request_line = yield self.connfh.readline()
            tolerance -= 1
          if not tolerance:
            return
        method, path, req_protocol = request_line.strip().split(" ", 2)
        ENVIRON["REQUEST_METHOD"] = method
        ENVIRON["CONTENT_LENGTH"] = ''

        scheme, location, path, params, qs, frag = urlparse(path)

        if frag:
          yield self.simple_response("400 Bad Request",
                      "Illegal #fragment in Request-URI.")
          return

        if scheme:
          ENVIRON["wsgi.url_scheme"] = scheme
        if params:
          path = path + ";" + params

        ENVIRON["SCRIPT_NAME"] = ""

        # Unquote the path+params (e.g. "/this%20path" -> "this path").
        # http://www.w3.org/Protocols/rfc2616/rfc2616-sec5.html#sec5.1.2
        #
        # But note that "...a URI must be separated into its components
        # before the escaped characters within those components can be
        # safely decoded." http://www.ietf.org/rfc/rfc2396.txt, sec 2.4.2
        atoms = [unquote(x) for x in quoted_slash.split(path)]
        path = "%2F".join(atoms)
        ENVIRON["PATH_INFO"] = path

        # Note that, like wsgiref and most other WSGI servers,
        # we unquote the path but not the query string.
        ENVIRON["QUERY_STRING"] = qs

        # Compare request and server HTTP protocol versions, in case our
        # server does not support the requested protocol. Limit our output
        # to min(req, server). We want the following output:
        #     request    server     actual written   supported response
        #     protocol   protocol  response protocol    feature set
        # a     1.0        1.0           1.0                1.0
        # b     1.0        1.1           1.1                1.0
        # c     1.1        1.0           1.0                1.0
        # d     1.1        1.1           1.1                1.1
        # Notice that, in (b), the response will be "HTTP/1.1" even though
        # the client only understands 1.0. RFC 2616 10.5.6 says we should
        # only return 505 if the _major_ version is different.
        rp = int(req_protocol[5]), int(req_protocol[7])
        server_protocol = ENVIRON["ACTUAL_SERVER_PROTOCOL"]
        sp = int(server_protocol[5]), int(server_protocol[7])
        if sp[0] != rp[0]:
          yield self.simple_response("505 HTTP Version Not Supported")
          return
        # Bah. "SERVER_PROTOCOL" is actually the REQUEST protocol.
        ENVIRON["SERVER_PROTOCOL"] = req_protocol
        self.response_protocol = "HTTP/%s.%s" % min(rp, sp)

        # If the Request-URI was an absoluteURI, use its location atom.
        if location:
          ENVIRON["SERVER_NAME"] = location

        # then all the http headers
        try:
          while True:
            line = yield self.connfh.readline()

            if line == '\r\n':
              # Normal end of headers
              break

            if line[0] in ' \t':
              # It's a continuation line.
              v = line.strip()
            else:
              k, v = line.split(":", 1)
              k, v = k.strip().upper(), v.strip()
              envname = "HTTP_" + k.replace("-", "_")

            if k in comma_separated_headers:
              existing = ENVIRON.get(envname)
              if existing:
                v = ", ".join((existing, v))
            ENVIRON[envname] = v

          ct = ENVIRON.pop("HTTP_CONTENT_TYPE", None)
          if ct:
            ENVIRON["CONTENT_TYPE"] = ct
          cl = ENVIRON.pop("HTTP_CONTENT_LENGTH", None)
          if cl:
            ENVIRON["CONTENT_LENGTH"] = cl
        except ValueError, ex:
          yield self.simple_response("400 Bad Request", repr(ex.args))
          return

        creds = ENVIRON.get("HTTP_AUTHORIZATION", "").split(" ", 1)
        ENVIRON["AUTH_TYPE"] = creds[0]
        if creds[0].lower() == 'basic':
          user, pw = base64.decodestring(creds[1]).split(":", 1)
          ENVIRON["REMOTE_USER"] = user

        # Persistent connection support
        if req_protocol == "HTTP/1.1":
          if ENVIRON.get("HTTP_CONNECTION", "") == "close":
            self.close_connection = True
        else:
          # HTTP/1.0
          if ENVIRON.get("HTTP_CONNECTION", "").lower() != "keep-alive":
            self.close_connection = True


        # Transfer-Encoding support
        te = None
        if self.response_protocol == "HTTP/1.1":
          te = ENVIRON.get("HTTP_TRANSFER_ENCODING")
          if te:
            te = [x.strip().lower() for x in te.split(",") if x.strip()]


        if te:
          # reject transfer encodings for now
          yield self.simple_response("501 Unimplemented")
          self.close_connection = True
          return
        ENV_COGEN_PROXY = ENVIRON['cogen.wsgi'] = async.COGENProxy(
          content_length = int(ENVIRON.get('CONTENT_LENGTH', None) or 0) or None,
          read_count = 0,
          operation = None,
          result = None,
          exception = None
        )
        ENVIRON['cogen.http_connection'] = self
        ENVIRON['cogen.core'] = async.COGENOperationWrapper(
          ENV_COGEN_PROXY,
          core
        )
        ENVIRON['cogen.call'] = async.COGENCallWrapper(ENV_COGEN_PROXY)
        ENVIRON['cogen.input'] = async.COGENOperationWrapper(
          ENV_COGEN_PROXY,
          self.connfh
        )
        ENVIRON['cogen.yield'] = async.COGENSimpleWrapper(ENV_COGEN_PROXY)
        response = self.wsgi_app(ENVIRON, self.start_response)
        #~ print 'WSGI RESPONSE:', response
        try:
          if isinstance(response, WSGIFileWrapper):
            # set tcp_cork to pack the header with the file data
            if hasattr(socket, "TCP_CORK"):
              self.conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_CORK, 1)

            assert self.started_response, "App returned the wsgi.file_wrapper but didn't call start_response."
            assert not self.sent_headers

            self.sent_headers = True
            yield sockets.SendAll(self.conn,
                    self.render_headers()+self.write_buffer.getvalue()
            )

            offset = response.filelike.tell()
            if self.chunked_write:
              fsize = os.fstat(response.filelike.fileno()).st_size
              yield sockets.SendAll(self.conn, hex(int(fsize-offset))+"\r\n")
            yield self.conn.sendfile(
              response.filelike,
              blocksize=response.blocksize,
              offset=offset,
              length=self.content_length,
              timeout=self.sendfile_timeout
            )

            if self.chunked_write:
              yield sockets.SendAll(self.conn, "\r\n")
            #  also, tcp_cork will make the file data sent on packet boundaries,
            # wich is a good thing
            if hasattr(socket, "TCP_CORK"):
              self.conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_CORK, 0)

          else:
            for chunk in response:
              if chunk:
                assert self.started_response, "App sended a value but hasn't called start_response."
                if not self.sent_headers:
                  self.sent_headers = True
                  headers = [self.render_headers(), self.write_buffer.getvalue()]
                else:
                  headers = []
                if self.chunked_write:
                  buf = [hex(len(chunk))[2:], "\r\n", chunk, "\r\n"]
                  if headers:
                    headers.extend(buf)
                    yield sockets.SendAll(self.conn, "".join(headers))
                  else:
                    yield sockets.SendAll(self.conn, "".join(buf))
                else:
                  if headers:
                    headers.append(chunk)
                    yield sockets.SendAll(self.conn, "".join(headers))
                  else:
                    yield sockets.SendAll(self.conn, chunk)
              else:
                if self.started_response:
                  if not self.sent_headers:
                    self.sent_headers = True
                    yield sockets.SendAll(self.conn,
                          self.render_headers()+self.write_buffer.getvalue())
                if ENV_COGEN_PROXY.operation:
                  op = ENV_COGEN_PROXY.operation
                  ENV_COGEN_PROXY.operation = None
                  try:
                    #~ print 'WSGI OP:', op
                    ENV_COGEN_PROXY.exception = None
                    ENV_COGEN_PROXY.result = yield op
                    #~ print 'WSGI OP RESULT:',ENVIRON['cogen.wsgi'].result
                  except:
                    #~ print 'WSGI OP EXCEPTION:', sys.exc_info()
                    ENV_COGEN_PROXY.exception = sys.exc_info()
                    ENV_COGEN_PROXY.result = ENV_COGEN_PROXY.exception[1]
                  del op
        finally:
          if hasattr(response, 'close'):
            response.close()

        if self.started_response:
          if not self.sent_headers:
            self.sent_headers = True
            yield sockets.SendAll(self.conn,
              self.render_headers()+self.write_buffer.getvalue()
            )
        else:
          import warnings
          warnings.warn("App was consumed and hasn't called start_response")

        if self.chunked_write:
          yield sockets.SendAll(self.conn, "0\r\n\r\n")
        if self.close_connection:
          return
        # TODO: consume any unread data

    except (socket.error, OSError, pywinerror), e:
      errno = e.args[0]
      if errno not in useless_socket_errors:
        yield self.simple_response("500 Internal Server Error",
                    format_exc())
      return
    except (OperationTimeout, ConnectionClosed, SocketError):
      return
    except (KeyboardInterrupt, SystemExit, GeneratorExit, MemoryError):
      raise
    except:
      if not self.started_response:
        yield self.simple_response(
          "500 Internal Server Error",
          format_exc()
        )
      else:
        print "*" * 60
        traceback.print_exc()
        print "*" * 60
      sys.exc_clear()
    finally:
      self.conn.close()
      ENVIRON = self.environ = None
class WSGIServer(object):
  """
  An HTTP server for WSGI.

  =================== ========================================================
  Option              Description
  =================== ========================================================
  bind_addr           The interface on which to listen for connections.
                      For TCP sockets, a (host, port) tuple. Host values may
                      be any IPv4 or IPv6 address, or any valid hostname.
                      The string 'localhost' is a synonym for '127.0.0.1' (or
                      '::1', if your hosts file prefers IPv6).
                      The string '0.0.0.0' is a special IPv4 entry meaning
                      "any active interface" (INADDR_ANY), and '::' is the
                      similar IN6ADDR_ANY for IPv6. The empty string or None
                      are not allowed.

                      For UNIX sockets, supply the filename as a string.

  wsgi_app            the WSGI 'application callable'; multiple WSGI
                      applications may be passed as (path_prefix, app) pairs.
  server_name         the string to set for WSGI's SERVER_NAME environ entry.
                      Defaults to socket.gethostname().
  request_queue_size  the 'backlog' argument to socket.listen();
                      specifies the maximum number of queued connections
                      (default 5).
  protocol            the version string to write in the Status-Line of all
                      HTTP responses. For example, "HTTP/1.1" (the default).
                      This also limits the supported features used in the
                      response.
  =================== ========================================================
  """

  protocol = "HTTP/1.1"
  _bind_addr = "localhost"
  ready = False
  ConnectionClass = WSGIConnection
  environ = {}

  def __init__(self, bind_addr, wsgi_app, scheduler,
            server_name=None,
            request_queue_size=64,
            sockoper_timeout=15,
            sendfile_timeout=-1,
            sockaccept_greedy=False
        ):
    self.request_queue_size = int(request_queue_size)
    self.sendfile_timeout = sendfile_timeout
    self.sockoper_timeout = sockoper_timeout
    self.scheduler = scheduler
    self.sockaccept_greedy = sockaccept_greedy
    self.environ['cogen.sched'] = self.scheduler

    self.version = "cogen.web/%s %s" % (__version__, scheduler.proactor.__class__.__name__)
    if callable(wsgi_app):
      # We've been handed a single wsgi_app, in CP-2.1 style.
      # Assume it's mounted at "".
      self.wsgi_app = wsgi_app
    else:
      # We've been handed a list of (path_prefix, wsgi_app) tuples,
      # so that the server can call different wsgi_apps, and also
      # correctly set SCRIPT_NAME.
      self.wsgi_app = WSGIPathInfoDispatcher(wsgi_app)

    self.bind_addr = bind_addr
    if not server_name:
      server_name = socket.gethostname()
    self.server_name = server_name

  def __str__(self):
    return "%s.%s(%r)" % (self.__module__, self.__class__.__name__,
                self.bind_addr)

  def _get_bind_addr(self):
    return self._bind_addr
  def _set_bind_addr(self, value):
    if isinstance(value, tuple) and value[0] in ('', None):
      # Despite the socket module docs, using '' does not
      # allow AI_PASSIVE to work. Passing None instead
      # returns '0.0.0.0' like we want. In other words:
      #     host    AI_PASSIVE     result
      #      ''         Y         192.168.x.y
      #      ''         N         192.168.x.y
      #     None        Y         0.0.0.0
      #     None        N         127.0.0.1
      # But since you can get the same effect with an explicit
      # '0.0.0.0', we deny both the empty string and None as values.
      raise ValueError("Host values of '' or None are not allowed. "
               "Use '0.0.0.0' (IPv4) or '::' (IPv6) instead "
               "to listen on all active interfaces.")
    self._bind_addr = value
  bind_addr = property(_get_bind_addr, _set_bind_addr,
    doc="""The interface on which to listen for connections.

    For TCP sockets, a (host, port) tuple. Host values may be any IPv4
    or IPv6 address, or any valid hostname. The string 'localhost' is a
    synonym for '127.0.0.1' (or '::1', if your hosts file prefers IPv6).
    The string '0.0.0.0' is a special IPv4 entry meaning "any active
    interface" (INADDR_ANY), and '::' is the similar IN6ADDR_ANY for
    IPv6. The empty string or None are not allowed.

    For UNIX sockets, supply the filename as a string.""")

  @coroutine
  def serve(self):
    """Run the server forever."""
    # We don't have to trap KeyboardInterrupt or SystemExit here,

    # Select the appropriate socket
    if isinstance(self.bind_addr, basestring):
      # AF_UNIX socket

      # So we can reuse the socket...
      try: os.unlink(self.bind_addr)
      except: pass

      # So everyone can access the socket...
      try: os.chmod(self.bind_addr, 0777)
      except: pass

      info = [(socket.AF_UNIX, socket.SOCK_STREAM, 0, "", self.bind_addr)]
    else:
      # AF_INET or AF_INET6 socket
      # Get the correct address family for our host (allows IPv6 addresses)
      host, port = self.bind_addr
      try:
        info = socket.getaddrinfo(host, port, socket.AF_UNSPEC,
                      socket.SOCK_STREAM, 0, socket.AI_PASSIVE)
      except socket.gaierror:
        # Probably a DNS issue. Assume IPv4.
        info = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", self.bind_addr)]

    self.socket = None
    msg = "No socket could be created"
    for res in info:
      af, socktype, proto, canonname, sa = res
      #~ print res
      try:
        self.bind(af, socktype, proto)
      except socket.error, msg:
        if self.socket:
          self.socket.close()
        self.socket = None
        continue
      break
    if not self.socket:
      raise socket.error, msg

    self.socket.listen(self.request_queue_size)
    with closing(self.socket):
      while True:
        try:
          s, addr = yield sockets.Accept(self.socket, timeout=-1)
          s.settimeout(self.sockoper_timeout)
        except Exception, exc:
          # make acceptor more robust in the face of weird
          # accept bugs, XXX: but we might get a infinite loop

          #~ import warnings
          #~ warnings.warn("Accept thrown an exception: %s" % exc)

          import traceback
          traceback.print_exc()
          continue

        environ = self.environ.copy()
        environ["SERVER_SOFTWARE"] = self.version
        # set a non-standard environ entry so the WSGI app can know what
        # the *real* server protocol is (and what features to support).
        # See http://www.faqs.org/rfcs/rfc2145.html.
        environ["ACTUAL_SERVER_PROTOCOL"] = self.protocol
        environ["SERVER_NAME"] = self.server_name

        if isinstance(self.bind_addr, basestring):
          # AF_UNIX. This isn't really allowed by WSGI, which doesn't
          # address unix domain sockets. But it's better than nothing.
          environ["SERVER_PORT"] = ""
        else:
          environ["SERVER_PORT"] = str(self.bind_addr[1])
          # optional values
          # Until we do DNS lookups, omit REMOTE_HOST
          environ["REMOTE_ADDR"] = addr[0]
          environ["REMOTE_PORT"] = str(addr[1])

        conn = self.ConnectionClass(s, self.wsgi_app, environ,
          self.sendfile_timeout)
        yield events.AddCoro(conn.run, prio=priority.LAST if
                                     self.sockaccept_greedy else priority.FIRST)

  def bind(self, family, type, proto=0):
    """Create (or recreate) the actual socket object."""
    self.socket = sockets.Socket(family, type, proto)
    self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    self.socket.setblocking(0)
    #~ self.socket.setsockopt(socket.SOL_SOCKET, socket.TCP_NODELAY, 1)
    self.socket.bind(self.bind_addr)

def asbool(obj):
    if isinstance(obj, (str, unicode)):
        obj = obj.strip().lower()
        if obj in ['true', 'yes', 'on', 'y', 't', '1']:
            return True
        elif obj in ['false', 'no', 'off', 'n', 'f', '0']:
            return False
        else:
            raise ValueError(
                "String is not true/false: %r" % obj)
    return bool(obj)

class Runner:
  def __init__(self, host, port, app, options, sched_class=Scheduler, server_class=WSGIServer):
    self.sched = sched_class(
      proactor = getattr(proactors, "has_"+options.get('proactor', 'any'))(),
      default_priority = int(options.get('sched_default_priority', priority.FIRST)),
      default_timeout = float(options.get('sched_default_timeout', 0)),
      proactor_resolution = float(options.get('proactor_resolution', 0.5)),
      proactor_multiplex_first = asbool(options.get('proactor_multiplex_first', 'true')),
      proactor_greedy = asbool(options.get('proactor_greedy')),
      ops_greedy = asbool(options.get('ops_greedy'))
    )
    self.server = server_class(
      (host, port),
      app,
      self.sched,
      server_name=options.get('server_name', host),
      request_queue_size=int(options.get('request_queue_size', 64)),
      sockoper_timeout=float(options.get('sockoper_timeout', 15)),
      sendfile_timeout=float(options.get('sendfile_timeout', 300)),
      sockaccept_greedy=asbool(options.get('sockaccept_greedy', 'false')),
    )
    self.sched.add(self.server.serve)

  def run(self):
    self.sched.run()

def server_factory(global_conf, host, port, **options):
  """Server factory for paste.

  Options are:
    * proactor: class name to use from cogen.core.proactors
      (default: DefaultProactor - best available proactor for current platform)
    * proactor_resolution: float
    * sched_default_priority: int (see cogen.core.util.priority)
    * sched_default_timeout: float (default: 0 - no timeout)
    * server_name: str
    * request_queue_size: int
    * sockoper_timeout: float (default: 15 - operations timeout in 15 seconds),
      -1 (no timeout), 0 (use scheduler's default), >0 (seconds)
    * sendfile_timeout: float (default: 300) - same as sockoper_timeout,
      only applied to sendfile operations (wich might need a much higher timeout
      value)
    * sockaccept_greedy: bool
  """
  port = int(port)

  try:
    import paste.util.threadinglocal as pastelocal
    pastelocal.local = local
  except ImportError:
    pass
  def serve(app):
    runner = Runner(host, port, app, options)
    runner.run()
  return serve
