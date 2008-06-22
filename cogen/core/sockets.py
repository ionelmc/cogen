"""
Socket-only coroutine operations and `Socket` wrapper.
Really - the only thing you need to know for most stuff is 
the `Socket <cogen.core.sockets.Socket.html>`_ class.
"""
__all__ = [
    'getdefaulttimeout', 'setdefaulttimeout', 'Socket', 'SendFile', 'Read',
    'ReadAll', 'ReadLine', 'Write', 'WriteAll','Accept','Connect', 
    'SocketOperation'
]

import socket
import errno
import exceptions
import datetime
import struct

try:
    import ctypes
    import win32file
    import win32event
    import pywintypes
except:
    pass

import events
from util import debug, priority, fmt_list
import reactors

getnow = datetime.datetime.now

try:
    import sendfile
except ImportError:
    sendfile = None
    
_TIMEOUT = None

def getdefaulttimeout():
    return _TIMEOUT

def setdefaulttimeout(timeout):
    """Set the default timeout used by the socket wrapper 
    (`Socket <cogen.core.sockets.Socket.html>`_ class)"""
    _TIMEOUT = timeout


class Socket(object):
    """
    A wrapper for socket objects, sets nonblocking mode and
    adds some internal bufers and wrappers. Regular calls to the usual 
    socket methods return operations for use in a coroutine.
    
    So you use this in a coroutine like:
    
    .. sourcecode:: python
    
        sock = Socket(family, type, proto) # just like the builtin socket module
        yield sock.read(1024)
    
    
    Constructor details:
    
    .. sourcecode:: python
    
        Socket([family[, type[, proto]]]) -> socket object
    
    Open a socket of the given type.  The family argument specifies the
    address family; it defaults to AF_INET.  The type argument specifies
    whether this is a stream (SOCK_STREAM, this is the default)
    or datagram (SOCK_DGRAM) socket.  The protocol argument defaults to 0,
    specifying the default protocol.  Keyword arguments are accepted.

    A socket object represents one endpoint of a network connection.
    """
    __slots__ = ['_fd', '_timeout', '_reactor_added']
    def __init__(self, *a, **k):
        self._fd = socket.socket(*a, **k)
        self._fd.setblocking(0)
        self._timeout = _TIMEOUT
        self._reactor_added = False
        
    def recv(self, bufsize, **kws):
        """Receive data from the socket. The return value is a string 
        representing the data received. The amount of data may be less than the
        ammount specified by _bufsize_. """
        return Recv(self, bufsize, timeout=self._timeout, **kws)
        
       
    def makefile(self, mode, bufsize):
        return _fileobject(self, mode, bufsize)
        
    def send(self, data, **kws):
        """Send data to the socket. The socket must be connected to a remote 
        socket. Ammount sent may be less than the data provided."""
        return Send(self, data, timeout=self._timeout, **kws)
        
    def sendall(self, data, **kws):
        """Send data to the socket. The socket must be connected to a remote 
        socket. All the data is guaranteed to be sent."""
        return SendAll(self, data, timeout=self._timeout, **kws)
        
    def accept(self, **kws):
        """Accept a connection. The socket must be bound to an address and 
        listening for connections. The return value is a pair (conn, address) 
        where conn is a new socket object usable to send and receive data on the 
        connection, and address is the address bound to the socket on the other 
        end of the connection. 
        
        Example:
        {{{
        conn, address = yield mysock.accept()
        }}}
        """
        return Accept(self, **kws)
        
    def close(self, *args):
        """Close the socket. All future operations on the socket object will 
        fail. The remote end will receive no more data (after queued data is 
        flushed). Sockets are automatically closed when they are garbage-collected. 
        """
        self._fd.close()
        
    def bind(self, *args):
        """Bind the socket to _address_. The socket must not already be bound. 
        (The format of _address_ depends on the address family) 
        """
        return self._fd.bind(*args)
        
    def connect(self, address, **kws):
        """Connect to a remote socket at _address_. """
        return Connect(self, address, **kws)
        
    def fileno(self):
        """Return the socket's file descriptor """
        return self._fd.fileno()
        
    def listen(self, backlog):
        """Listen for connections made to the socket. The _backlog_ argument 
        specifies the maximum number of queued connections and should be at 
        least 1; the maximum value is system-dependent (usually 5). 
        """
        return self._fd.listen(backlog)
        
    def getpeername(self):
        """Return the remote address to which the socket is connected."""
        return self._fd.getpeername()
        
    def getsockname(self, *args):
        """Return the socket's own address. """
        return self._fd.getsockname()
        
    def settimeout(self, to):
        """Set a timeout on blocking socket operations. The value argument can 
        be a nonnegative float expressing seconds, timedelta or None. 
        """
        self._timeout = to
        
    def gettimeout(self, *args):
        """Return the associated timeout value. """
        return self._timeout
        
    def shutdown(self, *args):
        """Shut down one or both halves of the connection. Same as the usual 
        socket method."""
        return self._fd.shutdown(*args)
        
    def setblocking(self, val):
        if val: 
            raise RuntimeError("You can't.")
    def setsockopt(self, *args):
        """Set the value of the given socket option. Same as the usual socket 
        method."""
        self._fd.setsockopt(*args)
        
    def __repr__(self):
        return '<socket at 0x%X>' % id(self)
    def __str__(self):
        return 'sock@0x%X' % id(self)
        
class SocketOperation(events.TimedOperation):
    """
    This is a generic class for a operation that involves some socket call.
        
    A socket operation should subclass WriteOperation or ReadOperation, define a
    `run` method and call the __init__ method of the superclass.
    """
    __slots__ = [
        'sock', 'last_update', 'type', 'run_first',        
    ]
    def __init__(self, sock, run_first=True, **kws):
        """
        All the socket operations have these generic properties that the 
        poller and scheduler interprets:
        
          * timeout - the ammout of time in seconds or timedelta, or the datetime 
            value till the poller should wait for this operation.
          * weak_timeout - if this is True the timeout handling code will take 
            into account the time of last activity (that would be the time of last
            `try_run` call)
          * prio - a flag for the scheduler
        """
        assert isinstance(sock, Socket)
        
        super(SocketOperation, self).__init__(**kws)
        self.sock = sock
        self.run_first = run_first
        
    def cleanup(self, sched, coro):
        return sched.proctor.remove(self, coro)
    
    
class SendFile(WriteOperation):
    """
        Uses underling OS sendfile call or a regular memory copy operation if 
        there is no sendfile.
        You can use this as a WriteAll if you specify the length.
        Usage:
            
        .. sourcecode:: python
            yield sockets.SendFile(file_object, socket_object, 0) 
                # will send till send operations return 0
                
            yield sockets.SendFile(file_object, socket_object, 0, blocksize=0)
                # there will be only one send operation (if successfull)
                # that meas the whole file will be read in memory if there is 
                #no sendfile
                
            yield sockets.SendFile(file_object, socket_object, 0, file_size)
                # this will hang if we can't read file_size bytes
                #from the file

    """
    __slots__ = [
        'sent', 'file_handle', 'offset', 
        'position', 'length', 'blocksize'
    ]
    def __init__(self, file_handle, sock, offset=None, length=None, blocksize=4096, **kws):
        super(SendFile, self).__init__(sock, **kws)
        self.file_handle = file_handle
        self.offset = self.position = offset or file_handle.tell()
        self.length = length
        self.sent = 0
        self.blocksize = blocksize
    def send(self, offset, length):
        if sendfile:
            offset, sent = sendfile.sendfile(
                self.sock.fileno(), 
                self.file_handle.fileno(), 
                offset, 
                length
            )
        else:
            self.file_handle.seek(offset)
            sent = self.sock._fd.send(self.file_handle.read(length))
        return sent
        
    def iocp_send(self, offset, length, overlap):
        self.file_handle.seek(offset)
        return win32file.WSASend(self.sock._fd, self.file_handle.read(length), overlap, 0)
        
    def iocp(self, overlap):
        if self.length:
            if self.blocksize:
                return self.iocp_send(
                    self.offset + self.sent, 
                    min(self.length-self.sent, self.blocksize),
                    overlap
                )
            else:
                return self.iocp_send(self.offset+self.sent, self.length-self.sent, overlap)
        else:
            return self.iocp_send(self.offset+self.sent, self.blocksize, overlap)
            
    def iocp_done(self, rc, nbytes):
        self.sent += nbytes

    def run(self, reactor):
        if self.length:
            assert self.sent <= self.length
        if self.sent == self.length:
            return self
            
        if self.length:
            if self.blocksize:
                self.sent += self.send(
                    self.offset + self.sent, 
                    min(self.length-self.sent, self.blocksize)
                )
            else:
                self.sent += self.send(self.offset+self.sent, self.length-self.sent)
            if self.sent == self.length:
                return self
        else:
            if self.blocksize:
                sent = self.send(self.offset+self.sent, self.blocksize)
            else:
                sent = self.send(self.offset+self.sent, self.blocksize)
                # we would use self.length but we don't have any,
                #  and we don't know the file's length
            self.sent += sent
            if not sent:
                return self
        #TODO: test this some more with bad usage cases
        
        
    def __repr__(self):
        return "<%s at 0x%X %s fh:%s offset:%r len:%s bsz:%s to:%s>" % (
            self.__class__.__name__, 
            id(self), 
            self.sock, 
            self.file_handle, 
            self.offset, 
            self.length, 
            self.blocksize, 
            self.timeout
        )
    


class Recv(SocketOperation):
    """
    Example usage:
    
    .. sourcecode:: python
        
        yield sockets.Read(socket_object, buffer_length)
    
    `buffer_length` is max read size, BUT, if if there are buffers from ReadLine 
    return them first.    
    """
    __slots__ = ['buff', 'len']
        
    def __init__(self, sock, len = 4096, **kws):
        super(Read, self).__init__(sock, **kws)
        self.len = len
        self.buff = None
    
    def process(self, sched, coro):
        return sched.proctor.request_recv(self, coro)
        
    def finalize(self):
        super(Read, self).finalize()
        return self.buff
                

class Send(SocketOperation):
    """
    Write the buffer to the socket and return the number of bytes written.
    """    
    __slots__ = ['sent']
    
    def __init__(self, sock, buff, **kws):
        super(Write, self).__init__(sock, **kws)
        self.buff = buff
        self.sent = 0
        
    def process(self, sched, coro):
        return sched.proctor.request_send(self, coro)
    
    def finalize(self):
        super(Write, self).finalize()
        return self.sent
        
class SendAll(SocketOperation):
    """
    Run this operation till all the bytes have been written.
    """
    __slots__ = ['sent', 'buff']
    
    def __init__(self, sock, buff, **kws):
        super(WriteAll, self).__init__(sock, **kws)
        self.buff = buff
        
    def process(self, sched, coro):
        return sched.proctor.request_sendall(self, coro)
    
 
class Accept(SocketOperation):
    """
    Returns a (conn, addr) tuple when the operation completes.
    """
    __slots__ = ['conn', 'addr']
    
    def __init__(self, sock, **kws):
        super(Accept, self).__init__(sock, **kws)
        self.conn = None
        
    def process(self, sched, coro):
        return sched.proctor.request_accept(self, coro)

    def finalize(self):
        super(Accept, self).finalize()
        return (self.conn, self.addr)
        
    def __repr__(self):
        return "<%s at 0x%X %s conn:%r to:%s>" % (
            self.__class__.__name__, 
            id(self), 
            self.sock, 
            self.conn, 
            self.timeout
        )
             
class Connect(SocketOperation):
    """
    
    """
    __slots__ = ['addr', 'conn', 'connect_attempted']
    
    def __init__(self, sock, addr, **kws):
        """
        Connect to the given `addr` using `sock`.
        """
        super(Connect, self).__init__(sock, **kws)
        self.addr = addr

    def process(self, sched, coro):
        return sched.proctor.request_connect(self, coro)
        
    def finalize(self):
        super(Accept, self).finalize()
        return self.conn
    