import psycopg2, psycopg2.extras
import threading
from . import config
from . import logger

conn = None

_connect_lock = threading.Lock()

def connect():
    global conn
    conn = psycopg2.connect(**config.PGSQL_CONNECT)
    conn.autocommit = True

def _reconnect(dead):
    """Reconnects after the connection `dead` failed, unless some other thread beat us to it."""
    with _connect_lock:
        if conn is dead:
            logger.warning("Lost postgresql connection; reconnecting")
            connect()
            logger.warning("Reconnected to postgresql")

class Cursor:
    """Cursor wrapper that reconnects and replays the query if the postgresql connection has gone
    away (typically because the server restarted under us).  Replaying is only safe because we run
    in autocommit mode: each execute() is a self-contained transaction, and a query that died with
    the connection cannot have been committed."""

    def __init__(self, factory=None):
        self._factory = factory
        self._conn = None
        self._cur = None

    def _cursor(self):
        if self._cur is None:
            self._conn = conn
            self._cur = conn.cursor(cursor_factory=self._factory) if self._factory else conn.cursor()
        return self._cur

    def execute(self, *args, **kwargs):
        try:
            return self._cursor().execute(*args, **kwargs)
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
            logger.warning("postgresql query failed ({}); retrying with a new connection".format(e))
            _reconnect(self._conn)
            self._cur = None
            return self._cursor().execute(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._cursor(), name)

    def __iter__(self):
        return iter(self._cursor())

def cursor():
    return Cursor()

def dict_cursor():
    return Cursor(psycopg2.extras.DictCursor)

