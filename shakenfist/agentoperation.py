from functools import partial
from shakenfist_utilities import logs

from shakenfist import baseobject
from shakenfist.baseobject import (
    DatabaseBackedObject as dbo,
    DatabaseBackedObjectIterator as dbo_iter)
from shakenfist import etcd


LOG, _ = logs.setup(__name__)


class AgentOperation(dbo):
    object_type = 'agentoperation'
    initial_version = 1
    current_version = 1

    STATE_QUEUED = 'queued'
    STATE_EXECUTING = 'executing'
    STATE_COMPLETE = 'complete'

    ACTIVE_STATES = {dbo.STATE_CREATED, STATE_QUEUED, STATE_EXECUTING, STATE_COMPLETE}

    state_targets = {
        None: (dbo.STATE_CREATED, dbo.STATE_ERROR),
        dbo.STATE_CREATED: (dbo.STATE_DELETED, dbo.STATE_ERROR, STATE_QUEUED),
        STATE_QUEUED: (dbo.STATE_DELETED, dbo.STATE_ERROR, STATE_EXECUTING),
        STATE_EXECUTING: (dbo.STATE_DELETED, dbo.STATE_ERROR, STATE_COMPLETE),
        dbo.STATE_DELETED: None,
    }

    def __init__(self, static_values):
        self.upgrade(static_values)

        super(AgentOperation, self).__init__(static_values['uuid'],
                                             static_values.get('version'))

        self.__namespace = static_values['namespace']
        self.__instance_uuid = static_values['instance_uuid']
        self.__commands = static_values['commands']

    @classmethod
    def new(cls, operation_uuid, namespace, instance_uuid, commands):
        o = AgentOperation.from_db(operation_uuid)
        if o:
            return o

        AgentOperation._db_create(operation_uuid, {
            'uuid': operation_uuid,
            'namespace': namespace,
            'instance_uuid': instance_uuid,
            'commands': commands,
            'version': cls.current_version
        })
        o = AgentOperation.from_db(operation_uuid)
        o.state = cls.STATE_CREATED
        return o

    def external_view(self):
        # If this is an external view, then mix back in attributes that users
        # expect
        retval = self._external_view()
        retval.update({
            'namespace': self.namespace,
            'instance_uuid': self.instance_uuid,
            'commands': self.commands
        })
        return retval

    # Static values
    @property
    def namespace(self):
        return self.__namespace

    @property
    def instance_uuid(self):
        return self.__instance_uuid

    @property
    def commands(self):
        return self.__commands

    def delete(self):
        self.state = self.STATE_DELETED


class AgentOperations(dbo_iter):
    def __iter__(self):
        for _, o in etcd.get_all('agentoperation', None):
            operation_uuid = o.get('uuid')
            o = AgentOperation.from_db(operation_uuid)
            if not o:
                continue

            out = self.apply_filters(o)
            if out:
                yield out


active_states_filter = partial(baseobject.state_filter, AgentOperation.ACTIVE_STATES)
