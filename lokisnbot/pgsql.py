import psycopg2, psycopg2.extras
from . import config

conn = None

def connect():
    global conn
    conn = psycopg2.connect(**config.PGSQL_CONNECT)
    conn.autocommit = True

def cursor():
    return conn.cursor()

def dict_cursor():
    return conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

