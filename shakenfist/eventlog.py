import copy
import datetime
import flask
import json
from oslo_concurrency import lockutils
import os
import pathlib
from shakenfist_utilities import logs
import sqlite3
import time

from shakenfist.config import config
from shakenfist import etcd


LOG, _ = logs.setup(__name__)


def add_event(object_type, object_uuid, message, duration=None,
              extra=None, suppress_event_logging=False):
    # Queue an event in etcd to get shuffled over to the long term data store
    timestamp = time.time()

    if not object_type or not object_uuid:
        return

    # If we alter extra, we don't want that to leak back to the caller.
    if not extra:
        extra = {}
    else:
        extra = copy.deepcopy(extra)

    # If this event was created in the context of a request from our API, then
    # we should record the request id that caused this event.
    try:
        request_id = flask.request.environ.get('FLASK_REQUEST_ID')
    except RuntimeError:
        request_id = None
    if request_id and 'request-id' not in extra:
        extra['request-id'] = request_id

    if not suppress_event_logging:
        log = LOG
        if extra:
            log = log.with_fields(extra)
        log.with_fields(
            {
                object_type: object_uuid,
                'fqdn': config.NODE_NAME,
                'duration': duration,
                'message': message
            }).info('Added event')

    # We use the old eventlog mechanism as a queueing system to get the logs
    # to the eventlog node.
    etcd.put('event/%s' % object_type, object_uuid, timestamp,
             {
                 'timestamp': timestamp,
                 'object_type': object_type,
                 'object_uuid': object_uuid,
                 'fqdn': config.NODE_NAME,
                 'duration': duration,
                 'message': message,
                 'extra': extra
             })


def upgrade_data_store():
    # Upgrades for the actual underlying data store
    version_path = os.path.join(config.STORAGE_PATH, 'events', '_version')
    if os.path.exists(version_path):
        with open(version_path) as f:
            version = json.loads(f.read())['version']
    else:
        version = 1
    start_version = version
    start_time = time.time()

    if version == 1:
        # Version two is shared, and uses an EventLog chain instead of a single
        # sqlite database.
        version = 2
        count = 0
        for objtype in ['artifact', 'blob', 'instance', 'namespace', 'network',
                        'networkinterface', 'node']:
            objroot = os.path.join(config.STORAGE_PATH, 'events', objtype)
            if os.path.exists(objroot):
                for ent in os.listdir(objroot):
                    entpath = os.path.join(objroot, ent)
                    if os.path.isdir(entpath):
                        continue
                    if ent.endswith('.lock'):
                        continue

                    if len(ent) == 2:
                        # A special case -- very short namespace and node
                        # names upset the system by clashing with the shard
                        # directory name.
                        os.rename(entpath, entpath + '.mv')
                        if os.path.exists(entpath + '.lock'):
                            os.rename(entpath + '.lock', entpath + '.mv.lock')
                        entpath += '.mv'

                    # Moving data between chunks is hard, so we don't. Instead
                    # we just use the year month that this code was written as
                    # the chunk name.
                    newdir = _shard_db_path(objtype, ent)
                    os.makedirs(newdir, exist_ok=True)
                    os.rename(entpath, os.path.join(newdir, ent + '.202303'))
                    if os.path.exists(entpath + '.lock'):
                        os.rename(entpath + '.lock',
                                  os.path.join(newdir, ent + '.lock'))

                    count += 0

        if count > 0:
            LOG.info('Resharded %d event log databases' % count)

    if start_version != version:
        os.makedirs(os.path.dirname(version_path), exist_ok=True)
        with open(version_path, 'w') as f:
            f.write(json.dumps({'version': version}, indent=4, sort_keys=True))
        LOG.info('Event datastore upgrade took %.02f seconds'
                 % (time.time() - start_time))


def _shard_db_path(objtype, objuuid):
    path = os.path.join(config.STORAGE_PATH, 'events', objtype, objuuid[0:2])
    os.makedirs(path, exist_ok=True)
    return path


def _timestamp_to_year_month(timestamp):
    dt = datetime.datetime.fromtimestamp(timestamp)
    return dt.year, dt.month


class EventLog(object):
    # An EventLog is a meta object which manages a chain of EventLogChunks,
    # which are per-month sqlite databases. This is done to keep individual
    # database sizes manageable, and provide a form of simple log rotation.
    # Locking for a given object is handled at this level, as well as handling
    # corruption of a single chunk.
    def __init__(self, objtype, objuuid):
        self.objtype = objtype
        self.objuuid = objuuid
        self.log = LOG.with_fields({self.objtype: self.objuuid})
        self.dbdir = _shard_db_path(self.objtype, self.objuuid)
        self.lock = lockutils.external_lock(
            '%s.lock' % self.objuuid, lock_path=self.dbdir)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        ...

    def write_event(self, timestamp, fqdn, duration, message, extra=None):
        with self.lock:
            year, month = _timestamp_to_year_month(timestamp)
            elc = EventLogChunk(self.objtype, self.objuuid, year, month)
            elc.write_event(timestamp, fqdn, duration, message, extra=extra)
            elc.close()

    def _get_all_chunks(self):
        p = pathlib.Path()
        yearmonths = []
        for ent in p.glob('%s*' % self.objuuid):
            if ent.endswith('.lock'):
                continue
            if ent.endswith('.corrupt'):
                continue

            _, yearmonth = ent.split('.')
            yearmonths.append(yearmonth)

        for yearmonth in sorted(yearmonths, reverse=True):
            year = int(yearmonth[0:4])
            month = int(yearmonth[4:])
            yield year, month

    def read_events(self, limit=100):
        with self.lock:
            events = []

            for year, month in self._get_all_chunks():
                elc = EventLogChunk(self.objtype, self.objuuid, year, month)
                events.append(elc.read_events(limit=(limit - len(events))))
                elc.close()

                if len(events) >= limit:
                    return events

            return events

    def delete(self):
        with self.lock:
            for year, month in self._get_all_chunks():
                elc = EventLogChunk(self.objtype, self.objuuid, year, month)
                elc.delete()
            self.log.info('Removed event log')

        lockpath = os.path.join(self.dbdir, self.objuuid + '.lock')
        if os.path.exists(lockpath):
            os.remove(lockpath)

    def prune_old_events(self, before_timestamp, message=None, limit=10000):
        with self.lock:
            removed = 0

            for year, month in self._get_all_chunks():
                elc = EventLogChunk(self.objtype, self.objuuid, year, month)
                removed += elc.prune_old_events(
                    before_timestamp, message=message, limit=(limit - removed))
                elc.close()

                if removed >= limit:
                    return removed

            return removed


# This is the version for an individual sqlite file
VERSION = 4
CREATE_EVENT_TABLE = """CREATE TABLE IF NOT EXISTS events(
    timestamp real PRIMARY KEY, fqdn text,
    operation text, phase text, duration float, message text,
    extra text)"""
CREATE_VERSION_TABLE = """CREATE TABLE IF NOT EXISTS version(version int primary key)"""


class EventLogChunk(object):
    # An event log chunk is a single sqlite database which covers a specific
    # calendar month. Note that locking is done at the EventLog level, not the
    # EventLogChunk level to reduce the number of lock files we are storing
    # in the filesystem.
    def __init__(self, objtype, objuuid, year, month):
        self.objtype = objtype
        self.objuuid = objuuid
        self.chunk = '%04d%02d' % (year, month)

        self.log = LOG.with_fields({
            self.objtype: self.objuuid,
            'chunk': self.chunk
            })

        self.dbdir = _shard_db_path(self.objtype, self.objuuid)
        self.dbpath = os.path.join(self.dbdir, self.objuuid + '.' + self.chunk)
        self._bootstrap()

    def _bootstrap(self):
        os.makedirs(self.dbdir, exist_ok=True)
        if not os.path.exists(self.dbpath):
            self.log.info('Creating event log')

        self.con = sqlite3.connect(self.dbpath)
        self.con.row_factory = sqlite3.Row
        cur = self.con.cursor()

        # Check if we already have a version table with data
        cur.execute("SELECT count(name) FROM sqlite_master WHERE "
                    "type='table' AND name='version'")
        if cur.fetchone()['count(name)'] == 0:
            # We do not have a version table, skip to the latest version
            self.con.execute(CREATE_EVENT_TABLE)
            self.con.execute(CREATE_VERSION_TABLE)
            self.con.execute('INSERT INTO version VALUES (?)', (VERSION, ))
            self.con.commit()

        else:
            # Open an existing database, which _might_ require upgrade
            start_upgrade = time.time()
            cur.execute('SELECT * FROM version')
            ver = cur.fetchone()['version']
            start_ver = ver

            if ver == 1:
                ver = 2
                self.log.info('Upgrading database from v1 to v2')
                self.con.execute(
                    'ALTER TABLE events ADD COLUMN extra text')
                self.con.execute(
                    'INSERT INTO events(timestamp, message) '
                    'VALUES (%f, "Upgraded database to version 2")'
                    % time.time())
                self.log.info('Upgraded database from v1 to v2')

            if ver == 2:
                ver = 3
                self.log.info('Upgrading database from v2 to v3')
                if self.objtype == 'node':
                    self.con.execute(
                        'DELETE FROM events WHERE timestamp < %d AND '
                        'message = "Updated node resources"'
                        % (time.time() - config.MAX_NODE_RESOURCE_EVENT_AGE))

                self.con.execute(
                    'INSERT INTO events(timestamp, message) '
                    'VALUES (%f, "Upgraded database to version 3")'
                    % time.time())
                self.log.info('Upgraded database from v2 to v3')

            if ver == 3:
                ver = 4
                self.log.info('Upgrading database from v3 to v4')
                if self.objtype == 'node':
                    self.con.execute(
                        'DELETE FROM events WHERE timestamp < %d AND '
                        'message = "Updated node resources and package versions";'
                        % (time.time() - config.MAX_NODE_RESOURCE_EVENT_AGE))

                self.con.execute(
                    'INSERT INTO events(timestamp, message) '
                    'VALUES (%f, "Upgraded database to version 4");'
                    % time.time())
                self.log.info('Upgraded database from v3 to v4')

            if start_ver != ver:
                self.con.execute('UPDATE version SET version = ?', (ver,))
                self.con.commit()
                self.con.execute('VACUUM')
                self.con.execute(
                        'INSERT INTO events(timestamp, message) '
                        'VALUES (%f, "Compacted database")'
                        % time.time())
                self.log.info('Database upgrade took %.02f seconds'
                              % (time.time() - start_upgrade))

    def close(self):
        self.con.close()

    def write_event(self, timestamp, fqdn, duration, message, extra=None):
        attempts = 0
        while attempts < 3:
            try:
                self.con.execute(
                    'INSERT INTO events(timestamp, fqdn, duration, message, extra) '
                    'VALUES (?, ?, ?, ?, ?)',
                    (timestamp, fqdn, duration, message,
                     json.dumps(extra, cls=etcd.JSONEncoderCustomTypes)))
                self.con.commit()
                return
            except sqlite3.IntegrityError:
                timestamp += 0.00001
                attempts += 1

        self.log.with_fields({
            'timestamp': timestamp,
            'fqdn': fqdn,
            'duration': duration,
            'message': message,
            'extra': extra
        }).error('Failed to record event after 3 retries')

    def read_events(self, limit=100):
        cur = self.con.cursor()
        cur.execute('SELECT * FROM events ORDER BY TIMESTAMP DESC LIMIT %d'
                    % limit)
        for row in cur.fetchall():
            yield dict(row)

    def count_events(self):
        cur = self.con.cursor()
        cur.execute('SELECT COUNT(*) FROM events')
        return cur.fetchone()[0]

    def delete(self):
        self.close()
        if os.path.exists(self.dbpath):
            os.unlink(self.dbpath)
        self.log.info('Removed event log chunk')

    def prune_old_events(self, before_timestamp, message=None, limit=10000):
        sql = 'DELETE FROM EVENTS WHERE timestamp < %s' % before_timestamp
        if message:
            sql += ' AND MESSAGE="%s"' % message
        sql += ' LIMIT %d' % limit

        cur = self.con.cursor()
        cur.execute(sql)

        cur.execute('SELECT CHANGES()')
        changes = cur.fetchone()[0]
        if changes > 0:
            self.log.with_fields({'message': message}).info(
                'Removed %d old events' % changes)
        self.con.commit()
        if changes == limit:
            self.log.info('Vacuuming event database')
            cur.execute('VACUUM')
        return changes
