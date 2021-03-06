Asynchronous server extensions
==============================


Introduction
------------

The idea is to support asynchronous operations: that is give the wsgi app ability
to pause itself, resume when something happens and make available the result of
that *something*. In cogen that *something* is a operation.

Another desired feature is to allow use of middleware. We achieve this by
yielding empty strings in the app and saving the operation to be run in a
object from `environ` - wsgi spec specifies that middleware should yield at
least as many chunks as the wrapped app has yielded [1]_. Though not any
middleware follows that to the letter - you have to look at `cogen.web.wsgi`
as to a streaming server - if the middleware flattens the response then breakage
will occur as operations aren't sent to the wsgi server and the app is not paused.

The wsgi server provides 4 nice objects in the environ that eases writing apps like this:

  * ``environ['cogen.core']`` - a wrapper that sets ``environ['cogen.wsgi'].operation``
    with the called object and returns a empty string. This should penetrate all
    the compilant middleware - take note of the flatten issue above.

  * ``environ['cogen.wsgi']`` - this is a object for communications between
    the app and the server

    * ``environ['cogen.wsgi'].result`` - holds the result of the operation,
      if a error occured it will be a instance of :class:`Exception`
    * ``environ['cogen.wsgi'].operation`` - hold the operation to run - you
      don't need to fiddle with need - just use ``environ['cogen.core']``
    * ``environ['cogen.wsgi'].exception`` - holds complete information about the
      error occurred in a tuple: ``(type, value, traceback)``

  * ``environ['cogen.input']`` - wrapped instance of :class:`~cogen.core.sockets._fileobject`
  * ``environ['cogen.call']`` - a wrapper that sets ``environ['cogen.wsgi'].operation``
    with the called object and returns a empty string.

So for example, if you have something like this in your wsgi app::

    yield environ['cogen.core'].events.Sleep(1)

what actually happens is that ``environ['cogen.core'].events.Sleep(1)`` will return an
empty string and set ``environ['cogen.wsgi'].operation`` to the actual Sleep
operation object. If your middleware stack respects the wsgi spec it will pass
that empty string down to the wsgi server where the Sleep operation will
be processed.


.. [1] http://www.python.org/dev/peps/pep-0333/#middleware-handling-of-block-boundaries

Example app with coroutine extensions
`````````````````````````````````````

::

    def wait_app(environ, start_response):
        start_response('200 OK', [('Content-type','text/html')])
        yield "I'm waiting for some signal<br>"
        yield environ['cogen.core'].events.WaitForSignal("abc", timeout=5)
        if isinstance(environ['cogen.wsgi'].result, Exception):
            yield "Your time is up !"
        else:
            yield "Someone signaled me with this message: %s" % cgi.escape(`environ['cogen.wsgi'].result`)


Reading the input asynchronously
````````````````````````````````

``environ['cogen.input']`` is an wrapped :class:`~cogen.core.sockets._fileobject`.

Example::

    buff = StringIO()
    while 1:
        yield environ['cogen.input'].read(8192)
        result = environ['cogen.wsgi'].result
        if isinstance(result, Exception):
            import traceback
            traceback.print_exception(*environ['cogen.wsgi'].exception)
            break
        else:
            if not result:
                break
            buff.write(result)
    buff.seek(0)

Running async apps in a regular wsgi stack
------------------------------------------

Pylons
``````

You'll have to make these tweaks:

in your `make_app` factory (usually located in some `wsgiapp.py` or
`config/middleware.py`) change the `RegistryManager` middleware from::

    app = RegistryManager(app)

to::

    app = RegistryManager(app, streaming=True)


Also, your `make_app` factory has an option `full_stack` that you need to set to
False (either set the default to False or set it in in your configuration .ini file).
We need to do this because the `ErrorHandler` middleware consumes the app iterable
in order to catch errors - and our async app needs to be streamable.

I usually change this::

    def make_app(global_conf, full_stack=True, **app_conf):

to::

    def make_app(global_conf, full_stack=False, **app_conf):

Starting coroutines on your app's startup
'''''''''''''''''''''''''''''''''''''''''

One solution could be making a custom server runner in your pylons app and
starting the coroutines there.

A custom server runner looks like::

    from cogen.web.wsgi import Runner
    from cogen.web.wsgi import local


    pubsub = PublishSubscribeQueue()
    engine = Engine(pubsub)

    def server_runner(app, global_conf, host, port, **options):
      port = int(port)

      try:
        import paste.util.threadinglocal as pastelocal
        pastelocal.local = local
      except ImportError:
        pass

      runner = Runner(host, port, app, options)

      # you start the coroutines here

      # eg:
      runner.sched.add(my_startup_coro)

      runner.run()

Then you have to edit your `setup.py` to add a special server runner entry point.
In `entry_points` add::

    [paste.server_runner]
    hijacked_runner = pylonsappname.async:server_runner

And finally change the development.ini of whatever you are running paste with to have
something like::

    [server:main]
    use = egg:pylonsappname#hijacked_runner