#################################################################################
# DEAR FUTURE ME... The order of decorators on these API methods deeply deeply  #
# matters. We need to verify auth before anything, and we need to fetch things  #
# from the database before we make decisions based on those things. So remember #
# the outer decorator is executed first!                                        #
#                                                                               #
# Additionally, you should use suppress_traceback=True in calls to error()      #
# which exist inside an expected exception block, otherwise we'll log a stray   #
# traceback.                                                                    #
#################################################################################

import base64
import bcrypt
import copy
from functools import partial
import flask
from flask_jwt_extended import create_access_token
from flask_jwt_extended import get_jwt_identity
from flask_jwt_extended import JWTManager
from flask_jwt_extended import jwt_required
from flask_jwt_extended.exceptions import (
    JWTDecodeError, NoAuthorizationError, InvalidHeaderError, WrongTokenError,
    RevokedTokenError, FreshTokenRequired, CSRFError
)
import flask_restful
from flask_restful import fields
from flask_restful import marshal_with
import ipaddress
import json
from jwt.exceptions import DecodeError, PyJWTError
import os
import random
import re
import requests
import sys
import traceback
import uuid

from shakenfist import artifact
from shakenfist.artifact import Artifact, Artifacts
from shakenfist import baseobject
from shakenfist.blob import Blob
from shakenfist.daemons import daemon
from shakenfist.baseobject import DatabaseBackedObject as dbo
from shakenfist.config import config
from shakenfist import db
from shakenfist import exceptions
from shakenfist import images
from shakenfist import instance
from shakenfist.ipmanager import IPManager
from shakenfist import logutil
from shakenfist import net
from shakenfist import networkinterface
from shakenfist.networkinterface import NetworkInterface
from shakenfist.node import Node, Nodes
from shakenfist import scheduler
from shakenfist.tasks import (
    DeleteInstanceTask,
    FetchImageTask,
    PreflightInstanceTask,
    StartInstanceTask,
    DestroyNetworkTask,
    FloatNetworkInterfaceTask,
    DefloatNetworkInterfaceTask
)
from shakenfist import util


LOG, HANDLER = logutil.setup(__name__)
daemon.set_log_level(LOG, 'api')


TESTING = False
SCHEDULER = None


def error(status_code, message, suppress_traceback=False):
    global TESTING

    body = {
        'error': message,
        'status': status_code
    }

    _, _, tb = sys.exc_info()
    formatted_trace = traceback.format_exc()

    if TESTING or config.get('INCLUDE_TRACEBACKS'):
        if tb:
            body['traceback'] = formatted_trace

    resp = flask.Response(json.dumps(body),
                          mimetype='application/json')
    resp.status_code = status_code

    if not suppress_traceback:
        LOG.info('Returning API error: %d, %s\n    %s'
                 % (status_code, message,
                    '\n    '.join(formatted_trace.split('\n'))))
    else:
        LOG.info('Returning API error: %d, %s (traceback suppressed by caller)'
                 % (status_code, message))

    return resp


def flask_get_post_body():
    j = {}
    try:
        j = flask.request.get_json(force=True)
    except Exception:
        if flask.request.data:
            try:
                j = json.loads(flask.request.data)
            except Exception:
                pass
    return j


def generic_wrapper(func):
    def wrapper(*args, **kwargs):
        try:
            j = flask_get_post_body()

            if j:
                for key in j:
                    if key == 'uuid':
                        destkey = 'passed_uuid'
                    else:
                        destkey = key
                    kwargs[destkey] = j[key]

            formatted_headers = []
            for header in flask.request.headers:
                formatted_headers.append(str(header))

            # Ensure key does not appear in logs
            kwargs_log = copy.copy(kwargs)
            if 'key' in kwargs_log:
                kwargs_log['key'] = '*****'

            msg = 'API request: %s %s' % (
                flask.request.method, flask.request.url)
            msg += '\n    Args: %s\n    KWargs: %s' % (args, kwargs_log)

            if re.match(r'http(|s)://0.0.0.0:\d+/$', flask.request.url):
                LOG.debug(msg)
            else:
                LOG.info(msg)

            return func(*args, **kwargs)

        except TypeError as e:
            return error(400, str(e), suppress_traceback=True)

        except DecodeError:
            # Send a more informative message than 'Not enough segments'
            return error(401, 'invalid JWT in Authorization header',
                         suppress_traceback=True)

        except (JWTDecodeError,
                NoAuthorizationError,
                InvalidHeaderError,
                WrongTokenError,
                RevokedTokenError,
                FreshTokenRequired,
                CSRFError,
                PyJWTError,
                ) as e:
            return error(401, str(e), suppress_traceback=True)

        except Exception as e:
            LOG.exception('Server error')
            return error(500, 'server error: %s' % repr(e),
                         suppress_traceback=True)

    return wrapper


class Resource(flask_restful.Resource):
    method_decorators = [generic_wrapper]


def caller_is_admin(func):
    # Ensure only users in the 'system' namespace can call this method
    def wrapper(*args, **kwargs):
        if get_jwt_identity() != 'system':
            return error(401, 'unauthorized')

        return func(*args, **kwargs)
    return wrapper


def arg_is_instance_uuid(func):
    # Method uses the instance from the db
    def wrapper(*args, **kwargs):
        if 'instance_uuid' in kwargs:
            kwargs['instance_from_db'] = instance.Instance.from_db(
                kwargs['instance_uuid'])
        if not kwargs.get('instance_from_db'):
            LOG.with_instance(kwargs['instance_uuid']).info(
                'Instance not found, genuinely missing')
            return error(404, 'instance not found')

        return func(*args, **kwargs)
    return wrapper


def redirect_instance_request(func):
    # Redirect method to the hypervisor hosting the instance
    def wrapper(*args, **kwargs):
        i = kwargs.get('instance_from_db')
        if not i:
            return

        placement = i.placement
        if not placement:
            return
        if not placement.get('node'):
            return

        if placement.get('node') != config.NODE_NAME:
            url = 'http://%s:%d%s' % (placement['node'], config.get('API_PORT'),
                                      flask.request.environ['PATH_INFO'])
            api_token = util.get_api_token(
                'http://%s:%d' % (placement['node'], config.get('API_PORT')),
                namespace=get_jwt_identity())
            r = requests.request(
                flask.request.environ['REQUEST_METHOD'], url,
                data=json.dumps(flask_get_post_body()),
                headers={'Authorization': api_token,
                         'User-Agent': util.get_user_agent()})

            LOG.info('Proxied %s %s returns: %d, %s' % (
                     flask.request.environ['REQUEST_METHOD'], url,
                     r.status_code, r.text))
            resp = flask.Response(r.text,
                                  mimetype='application/json')
            resp.status_code = r.status_code
            return resp

        return func(*args, **kwargs)
    return wrapper


def requires_instance_ownership(func):
    # Requires that @arg_is_instance_uuid has already run
    def wrapper(*args, **kwargs):
        if not kwargs.get('instance_from_db'):
            LOG.with_field('instance', kwargs['instance_uuid']).info(
                'Instance not found, kwarg missing')
            return error(404, 'instance not found')

        i = kwargs['instance_from_db']
        if get_jwt_identity() not in [i.namespace, 'system']:
            LOG.with_instance(i).info(
                'Instance not found, ownership test in decorator')
            return error(404, 'instance not found')

        return func(*args, **kwargs)
    return wrapper


def requires_instance_active(func):
    # Requires that @arg_is_instance_uuid has already run
    def wrapper(*args, **kwargs):
        if not kwargs.get('instance_from_db'):
            LOG.with_field('instance', kwargs['instance_uuid']).info(
                'Instance not found, kwarg missing')
            return error(404, 'instance not found')

        i = kwargs['instance_from_db']
        if i.state.value != instance.Instance.STATE_CREATED:
            LOG.with_instance(i).info(
                'Instance not ready (%s)' % i.state.value)
            return error(406, 'instance %s is not ready (%s)' % (i.uuid, i.state.value))

        return func(*args, **kwargs)
    return wrapper


def arg_is_network_uuid(func):
    # Method uses the network from the db
    def wrapper(*args, **kwargs):
        if 'network_uuid' in kwargs:
            kwargs['network_from_db'] = net.Network.from_db(
                kwargs['network_uuid'])
        if not kwargs.get('network_from_db'):
            LOG.with_field('network', kwargs['network_uuid']).info(
                'Network not found, missing or deleted')
            return error(404, 'network not found')

        return func(*args, **kwargs)
    return wrapper


def redirect_to_network_node(func):
    # Redirect method to the network node
    def wrapper(*args, **kwargs):
        if not util.is_network_node():
            admin_token = util.get_api_token(
                'http://%s:%d' % (config.NETWORK_NODE_IP,
                                  config.get('API_PORT')),
                namespace='system')
            r = requests.request(
                flask.request.environ['REQUEST_METHOD'],
                'http://%s:%d%s'
                % (config.NETWORK_NODE_IP,
                   config.get('API_PORT'),
                   flask.request.environ['PATH_INFO']),
                data=flask.request.data,
                headers={'Authorization': admin_token,
                         'User-Agent': util.get_user_agent()})

            LOG.info('Returning proxied request: %d, %s'
                     % (r.status_code, r.text))
            resp = flask.Response(r.text,
                                  mimetype='application/json')
            resp.status_code = r.status_code
            return resp

        return func(*args, **kwargs)
    return wrapper


def requires_network_ownership(func):
    # Requires that @arg_is_network_uuid has already run
    def wrapper(*args, **kwargs):
        log = LOG.with_field('network', kwargs['network_uuid'])

        if not kwargs.get('network_from_db'):
            log.info('Network not found, kwarg missing')
            return error(404, 'network not found')

        if get_jwt_identity() not in [kwargs['network_from_db'].namespace, 'system']:
            log.info('Network not found, ownership test in decorator')
            return error(404, 'network not found')

        return func(*args, **kwargs)
    return wrapper


def requires_network_active(func):
    # Requires that @arg_is_network_uuid has already run
    def wrapper(*args, **kwargs):
        log = LOG.with_field('network', kwargs['network_uuid'])

        if not kwargs.get('network_from_db'):
            log.info('Network not found, kwarg missing')
            return error(404, 'network not found')

        state = kwargs['network_from_db'].state
        if state.value != dbo.STATE_CREATED:
            log.info('Network not ready (%s)' % state.value)
            return error(406,
                         'network %s is not ready (%s)'
                         % (kwargs['network_from_db'].uuid, state.value))

        return func(*args, **kwargs)
    return wrapper


def _metadata_putpost(meta_type, owner, key, value):
    if meta_type not in ['namespace', 'instance', 'network']:
        return error(500, 'invalid meta_type %s' % meta_type)
    if not key:
        return error(400, 'no key specified')
    if not value:
        return error(400, 'no value specified')

    with db.get_lock('metadata', meta_type, owner,
                     op='Metadata update'):
        md = db.get_metadata(meta_type, owner)
        if md is None:
            md = {}
        md[key] = value
        db.persist_metadata(meta_type, owner, md)


app = flask.Flask(__name__)
api = flask_restful.Api(app, catch_all_404s=False)
app.config['JWT_SECRET_KEY'] = config.AUTH_SECRET_SEED.get_secret_value()
jwt = JWTManager(app)

# Use our handler to get SF log format (instead of gunicorn's handlers)
app.logger.handlers = [HANDLER]


@app.before_request
def log_request_info():
    LOG.debug(
        'API request headers:\n' +
        ''.join(['    %s: %s\n' % (h, v) for h, v in flask.request.headers]) +
        'API request body: %s' % flask.request.get_data())


class Root(Resource):
    def get(self):
        resp = flask.Response(
            'Shaken Fist REST API service',
            mimetype='text/plain')
        resp.status_code = 200
        return resp


class AdminLocks(Resource):
    @jwt_required
    @caller_is_admin
    def get(self):
        return db.get_existing_locks()


class Auth(Resource):
    def _get_keys(self, namespace):
        rec = db.get_namespace(namespace)
        if not rec:
            return (None, [])

        keys = []
        for key_name in rec.get('keys', {}):
            keys.append(base64.b64decode(rec['keys'][key_name]))
        return (rec.get('service_key'), keys)

    def post(self, namespace=None, key=None):
        if not namespace:
            return error(400, 'missing namespace in request')
        if not key:
            return error(400, 'missing key in request')
        if not isinstance(key, str):
            # Must be a string to encode()
            return error(400, 'key is not a string')

        service_key, keys = self._get_keys(namespace)
        if service_key and key == service_key:
            return {'access_token': create_access_token(identity=namespace)}
        for possible_key in keys:
            if bcrypt.checkpw(key.encode('utf-8'), possible_key):
                return {'access_token': create_access_token(identity=namespace)}

        return error(401, 'unauthorized')


class AuthNamespaces(Resource):
    @jwt_required
    @caller_is_admin
    def post(self, namespace=None, key_name=None, key=None):
        if not namespace:
            return error(400, 'no namespace specified')

        with db.get_lock('namespace', None, 'all', op='Namespace update'):
            rec = db.get_namespace(namespace)
            if not rec:
                rec = {
                    'name': namespace,
                    'keys': {}
                }

            # Allow shortcut of creating key at same time as the namespace
            if key_name:
                if not key:
                    return error(400, 'no key specified')
                if not isinstance(key, str):
                    # Must be a string to encode()
                    return error(400, 'key is not a string')
                if key_name == 'service_key':
                    return error(403, 'illegal key name')

                encoded = str(base64.b64encode(bcrypt.hashpw(
                    key.encode('utf-8'), bcrypt.gensalt())), 'utf-8')
                rec['keys'][key_name] = encoded

            # Initialise metadata
            db.persist_metadata('namespace', namespace, {})
            db.persist_namespace(namespace, rec)

        return namespace

    @jwt_required
    @caller_is_admin
    def get(self):
        out = []
        for rec in db.list_namespaces():
            out.append(rec['name'])
        return out


class AuthNamespace(Resource):
    @jwt_required
    @caller_is_admin
    def delete(self, namespace):
        if not namespace:
            return error(400, 'no namespace specified')
        if namespace == 'system':
            return error(403, 'you cannot delete the system namespace')

        # The namespace must be empty
        instances = []
        deleted_instances = []
        for i in instance.instances_in_namespace(namespace):
            if i.state.value in [dbo.STATE_DELETED, dbo.STATE_ERROR]:
                deleted_instances.append(i.uuid)
            else:
                LOG.withFields({'instance': i.uuid,
                                'state': i.state}).info('Blocks namespace delete')
                instances.append(i.uuid)
        if len(instances) > 0:
            return error(400, 'you cannot delete a namespace with instances')

        networks = []
        for n in net.networks_in_namespace(namespace):
            if not n.is_dead():
                LOG.withFields({'network': n.uuid,
                                'state': n.state}).info('Blocks namespace delete')
                networks.append(n.uuid)
        if len(networks) > 0:
            return error(400, 'you cannot delete a namespace with networks')

        db.delete_namespace(namespace)
        db.delete_metadata('namespace', namespace)


def _namespace_keys_putpost(namespace=None, key_name=None, key=None):
    if not namespace:
        return error(400, 'no namespace specified')
    if not key_name:
        return error(400, 'no key name specified')
    if not key:
        return error(400, 'no key specified')
    if key_name == 'service_key':
        return error(403, 'illegal key name')

    with db.get_lock('namespace', None, 'all', op='Namespace key update'):
        rec = db.get_namespace(namespace)
        if not rec:
            return error(404, 'namespace does not exist')

        encoded = str(base64.b64encode(bcrypt.hashpw(
            key.encode('utf-8'), bcrypt.gensalt())), 'utf-8')
        rec['keys'][key_name] = encoded

        db.persist_namespace(namespace, rec)

    return key_name


class AuthNamespaceKeys(Resource):
    @jwt_required
    @caller_is_admin
    def get(self, namespace=None):
        rec = db.get_namespace(namespace)
        if not rec:
            return error(404, 'namespace does not exist')

        out = []
        for keyname in rec['keys']:
            out.append(keyname)
        return out

    @jwt_required
    @caller_is_admin
    def post(self, namespace=None, key_name=None, key=None):
        return _namespace_keys_putpost(namespace, key_name, key)


class AuthNamespaceKey(Resource):
    @jwt_required
    @caller_is_admin
    def put(self, namespace=None, key_name=None, key=None):
        rec = db.get_namespace(namespace)
        if not rec:
            return error(404, 'namespace does not exist')
        if key_name not in rec['keys']:
            return error(404, 'key does not exist')

        return _namespace_keys_putpost(namespace, key_name, key)

    @jwt_required
    @caller_is_admin
    def delete(self, namespace, key_name):
        if not namespace:
            return error(400, 'no namespace specified')
        if not key_name:
            return error(400, 'no key name specified')

        with db.get_lock('namespace', None, namespace, op='Namespace key delete'):
            ns = db.get_namespace(namespace)
            if ns.get('keys') and key_name in ns['keys']:
                del ns['keys'][key_name]
            else:
                return error(404, 'key name not found in namespace')
            db.persist_namespace(namespace, ns)


class AuthMetadatas(Resource):
    @jwt_required
    @caller_is_admin
    def get(self, namespace):
        md = db.get_metadata('namespace', namespace)
        if not md:
            return {}
        return md

    @jwt_required
    @caller_is_admin
    def post(self, namespace, key=None, value=None):
        return _metadata_putpost('namespace', namespace, key, value)


class AuthMetadata(Resource):
    @jwt_required
    @caller_is_admin
    def put(self, namespace, key=None, value=None):
        return _metadata_putpost('namespace', namespace, key, value)

    @jwt_required
    @caller_is_admin
    def delete(self, namespace, key=None, value=None):
        if not key:
            return error(400, 'no key specified')

        with db.get_lock('metadata', 'namespace', namespace, op='Metadata delete'):
            md = db.get_metadata('namespace', namespace)
            if md is None or key not in md:
                return error(404, 'key not found')
            del md[key]
            db.persist_metadata('namespace', namespace, md)


class BlobEndpoint(Resource):
    @jwt_required
    def get(self, blob_uuid=None):
        # Fast path if we have the blob locally
        blob_path = os.path.join(config.get(
            'STORAGE_PATH'), 'blobs', blob_uuid)
        if os.path.exists(blob_path):
            def read_file(filename):
                with open(blob_path, 'rb') as f:
                    d = f.read(8192)
                    while d:
                        yield d
                        d = f.read(8192)

            return flask.Response(flask.stream_with_context(read_file(blob_path)),
                                  mimetype='text/plain', status=200)

        # Otherwise find a node which has the blob and proxy. Write to our blob
        # store as well if the blob is under replicated.
        b = Blob.from_db(blob_uuid)
        if not b:
            return error(404, 'blob not found')

        locations = b.locations
        if not locations:
            return error(404, 'blob missing')

        def read_remote(target, blob_uuid, blob_path=None):
            api_token = util.get_api_token(
                'http://%s:%d' % (target, config.get('API_PORT')),
                namespace=get_jwt_identity())
            url = 'http://%s:%d/blob/%s' % (target,
                                            config.get('API_PORT'), blob_uuid)

            if blob_path:
                local_blob = open(blob_path + '.partial', 'wb')
            r = requests.request('GET', url,
                                 headers={'Authorization': api_token,
                                          'User-Agent': util.get_user_agent()})
            for chunk in r.iter_content(chunk_size=8192):
                if blob_path:
                    local_blob.write(chunk)
                yield chunk

            if blob_path:
                local_blob.close()
                os.rename(blob_path + '.partial', blob_path)
                Blob.from_db(blob_uuid).observe()

        if len(locations) >= config.BLOB_REPLICATION_FACTOR:
            blob_path = None

        random.shuffle(locations)
        return flask.Response(flask.stream_with_context(
            read_remote(locations[0], blob_uuid, blob_path=blob_path)),
            mimetype='text/plain', status=200)


class Instance(Resource):
    @jwt_required
    @arg_is_instance_uuid
    @requires_instance_ownership
    def get(self, instance_uuid=None, instance_from_db=None):
        return instance_from_db.external_view()

    @jwt_required
    @arg_is_instance_uuid
    @requires_instance_ownership
    def delete(self, instance_uuid=None, instance_from_db=None):
        # Check if instance has already been deleted
        if instance_from_db.state.value == dbo.STATE_DELETED:
            return error(404, 'instance not found')

        # If this instance is not on a node, just do the DB cleanup locally
        placement = instance_from_db.placement
        if not placement.get('node'):
            node = config.NODE_NAME
        else:
            node = placement['node']

        instance_from_db.enqueue_delete_remote(node)


def _assign_floating_ip(ni):
    float_net = net.Network.from_db('floating')
    if not float_net:
        return error(404, 'floating network not found')

    # Address is allocated and added to the record here, so the job has it later.
    db.add_event('interface', ni.uuid, 'api', 'float', None, None)
    with db.get_lock('ipmanager', None, 'floating', ttl=120, op='Interface float'):
        ipm = IPManager.from_db('floating')
        addr = ipm.get_random_free_address(ni.unique_label())
        ipm.persist()

    ni.floating = addr


class Instances(Resource):
    @jwt_required
    def get(self, all=False):
        filters = [partial(baseobject.namespace_filter, get_jwt_identity())]
        if not all:
            filters.append(instance.active_states_filter)

        retval = []
        for i in instance.Instances(filters):
            # This forces the instance through the external view rehydration
            retval.append(i.external_view())
        return retval

    @jwt_required
    def post(self, name=None, cpus=None, memory=None, network=None, disk=None,
             ssh_key=None, user_data=None, placed_on=None, namespace=None,
             video=None):
        global SCHEDULER

        # Check that the instance name is safe for use as a DNS host name
        if name != re.sub(r'([^a-zA-Z0-9\-])', '', name) or len(name) > 63:
            return error(400, ('instance name %s is not useable as a DNS and Linux host name. '
                               'That is, less than 63 characters and in the character set: '
                               'a-z, A-Z, 0-9, or hyphen (-).' % name))

        # If we are placed, make sure that node exists
        if placed_on:
            n = Node.from_db(placed_on)
            if not n:
                return error(404, 'Specified node does not exist')
            if n.state.value != Node.STATE_CREATED:
                return error(404, 'Specified node not ready')

        # Sanity check
        if not disk:
            return error(400, 'instance must specify at least one disk')
        for d in disk:
            if not isinstance(d, dict):
                return error(400, 'disk specification should contain JSON objects')

            if d.get('base', '').startswith('label:'):
                label = d['base'][len('label:'):]
                a = Artifact.from_url(
                    Artifact.TYPE_LABEL, 'sf://label/%s/%s' % (get_jwt_identity(), label))
                if not a:
                    return error(404, 'label %s not found' % label)
                d['base'] = 'sf://blob/%s' % a.most_recent_index['blob_uuid']

        if network:
            for netdesc in network:
                if not isinstance(netdesc, dict):
                    return error(400,
                                 'network specification should contain JSON objects')

                if 'network_uuid' not in netdesc:
                    return error(400, 'network specification is missing network_uuid')

                net_uuid = netdesc['network_uuid']
                if netdesc.get('address') and not util.noneish(netdesc.get('address')):
                    # The requested address must be within the ip range specified
                    # for that virtual network, unless it is equivalent to "none".
                    ipm = IPManager.from_db(net_uuid)
                    if not ipm.is_in_range(netdesc['address']):
                        return error(400,
                                     'network specification requests an address outside the '
                                     'range of the network')

                n = net.Network.from_db(net_uuid)
                if not n:
                    return error(404, 'network %s does not exist' % net_uuid)
                if n.state.value != net.Network.STATE_CREATED:
                    return error(406, 'network %s is not ready (%s)' % (n.uuid, n.state.value))

        if not video:
            video = {'model': 'cirrus', 'memory': 16384}

        if not namespace:
            namespace = get_jwt_identity()

        # If accessing a foreign namespace, we need to be an admin
        if get_jwt_identity() not in [namespace, 'system']:
            return error(401,
                         'only admins can create resources in a different namespace')

        # Create instance object
        inst = instance.Instance.new(
            name=name,
            disk_spec=disk,
            memory=memory,
            cpus=cpus,
            ssh_key=ssh_key,
            user_data=user_data,
            namespace=namespace,
            video=video,
            requested_placement=placed_on
        )

        # Initialise metadata
        db.persist_metadata('instance', inst.uuid, {})

        # Allocate IP addresses
        order = 0
        float_tasks = []
        if network:
            for netdesc in network:
                n = net.Network.from_db(netdesc['network_uuid'])
                if not n:
                    m = 'missing network %s during IP allocation phase' % (
                        netdesc['network_uuid'])
                    inst.enqueue_delete_due_error(m)
                    return error(
                        404, 'network %s not found' % netdesc['network_uuid'])

                # NOTE(mikal): we now support interfaces with no address on them
                # (thanks OpenStack Kolla), which are special cased here. To not
                # have an address, you use a detailed netdesc and specify
                # address=none.
                if 'address' in netdesc and util.noneish(netdesc['address']):
                    netdesc['address'] = None
                else:
                    with db.get_lock('ipmanager', None,  netdesc['network_uuid'],
                                     ttl=120, op='Network allocate IP'):
                        db.add_event('network', netdesc['network_uuid'], 'allocate address',
                                     None, None, inst.uuid)
                        ipm = IPManager.from_db(netdesc['network_uuid'])
                        if 'address' not in netdesc or not netdesc['address']:
                            netdesc['address'] = ipm.get_random_free_address(
                                inst.unique_label())
                        else:
                            if not ipm.reserve(netdesc['address'], inst.unique_label()):
                                m = 'failed to reserve an IP on network %s' % (
                                    netdesc['network_uuid'])
                                inst.enqueue_delete_due_error(m)
                                return error(409, 'address %s in use' %
                                             netdesc['address'])

                        ipm.persist()

                if 'model' not in netdesc or not netdesc['model']:
                    netdesc['model'] = 'virtio'

                iface_uuid = str(uuid.uuid4())
                LOG.with_object(inst).with_object(n).withFields({
                    'networkinterface': iface_uuid
                }).info('Interface allocated')
                ni = NetworkInterface.new(
                    iface_uuid, netdesc, inst.uuid, order)
                order += 1

                if 'float' in netdesc and netdesc['float']:
                    err = _assign_floating_ip(ni)
                    if err:
                        inst.enqueue_delete_due_error(
                            'interface float failed: %s' % err)
                        return err

                    float_tasks.append(FloatNetworkInterfaceTask(
                        netdesc['network_uuid'], iface_uuid))

        if not SCHEDULER:
            SCHEDULER = scheduler.Scheduler()

        try:
            # Have we been placed?
            if not placed_on:
                candidates = SCHEDULER.place_instance(inst, network)
                placement = candidates[0]

            else:
                SCHEDULER.place_instance(inst, network,
                                         candidates=[placed_on])
                placement = placed_on

        except exceptions.LowResourceException as e:
            inst.add_event('schedule', 'failed', None,
                           'Insufficient resources: ' + str(e))
            inst.enqueue_delete_due_error('scheduling failed')
            return error(507, str(e), suppress_traceback=True)

        except exceptions.CandidateNodeNotFoundException as e:
            inst.add_event('schedule', 'failed', None,
                           'Candidate node not found: ' + str(e))
            inst.enqueue_delete_due_error('scheduling failed')
            return error(404, 'node not found: %s' % e, suppress_traceback=True)

        # Record placement
        inst.place_instance(placement)

        # Create a queue entry for the instance start
        tasks = [PreflightInstanceTask(inst.uuid, network)]
        for disk in inst.disk_spec:
            if disk.get('base'):
                tasks.append(FetchImageTask(disk['base'], inst.uuid))
        tasks.append(StartInstanceTask(inst.uuid, network))
        tasks.extend(float_tasks)

        # Enqueue creation tasks on desired node task queue
        db.enqueue(placement, {'tasks': tasks})
        inst.add_event('create', 'enqueued', None, None)
        return inst.external_view()

    @jwt_required
    def delete(self, confirm=False, namespace=None):
        """Delete all instances in the namespace."""

        if confirm is not True:
            return error(400, 'parameter confirm is not set true')

        if get_jwt_identity() == 'system':
            if not isinstance(namespace, str):
                # A client using a system key must specify the namespace. This
                # ensures that deleting all instances in the cluster (by
                # specifying namespace='system') is a deliberate act.
                return error(400, 'system user must specify parameter namespace')

        else:
            if namespace and namespace != get_jwt_identity():
                return error(401, 'you cannot delete other namespaces')
            namespace = get_jwt_identity()

        waiting_for = []
        tasks_by_node = {}
        for inst in instance.Instances([partial(baseobject.namespace_filter, namespace),
                                        instance.active_states_filter]):
            # If this instance is not on a node, just do the DB cleanup locally
            dbplacement = inst.placement
            if not dbplacement.get('node'):
                node = config.NODE_NAME
            else:
                node = dbplacement['node']

            tasks_by_node.setdefault(node, [])
            tasks_by_node[node].append(DeleteInstanceTask(inst.uuid))
            waiting_for.append(inst.uuid)

        for node in tasks_by_node:
            db.enqueue(node, {'tasks': tasks_by_node[node]})

        return waiting_for


class InstanceInterfaces(Resource):
    @jwt_required
    @arg_is_instance_uuid
    @requires_instance_ownership
    def get(self, instance_uuid=None, instance_from_db=None):
        out = []
        for ni in networkinterface.interfaces_for_instance(instance_from_db):
            out.append(ni.external_view())
        return out


class InstanceEvents(Resource):
    @jwt_required
    @arg_is_instance_uuid
    @requires_instance_ownership
    def get(self, instance_uuid=None, instance_from_db=None):
        return list(db.get_events('instance', instance_uuid))


class InstanceSnapshot(Resource):
    @jwt_required
    @arg_is_instance_uuid
    @requires_instance_ownership
    @redirect_instance_request
    @requires_instance_active
    def post(self, instance_uuid=None, instance_from_db=None, all=None):
        disks = instance_from_db.block_devices['devices']
        if not all:
            disks = [disks[0]]

        out = {}
        for disk in disks:
            if disk['snapshot_ignores']:
                continue

            if disk['type'] != 'qcow2':
                continue

            a = Artifact.from_url(
                Artifact.TYPE_SNAPSHOT,
                'sf://instance/%s/%s' % (instance_uuid, disk['device']))

            blob_uuid = str(uuid.uuid4())
            blob = instance_from_db.snapshot(blob_uuid, disk)
            blob.observe()
            entry = a.add_index(blob_uuid)

            out[disk['device']] = {
                'source_url': a.source_url,
                'artifact_uuid': a.uuid,
                'artifact_index': entry['index'],
                'blob_uuid': blob.uuid,
                'blob_size': blob.size,
                'blob_modified': blob.modified
            }

            LOG.with_fields({
                'instance': instance_uuid,
                'artifact': a.uuid,
                'blob': blob.uuid,
                'device': disk['device']
            }).info('Created snapshot')
            instance_from_db.add_event('api', 'snapshot %s' % disk,
                                       None, a.uuid)
            if a.state == dbo.STATE_INITIAL:
                a.state = dbo.STATE_CREATED

        return out

    @jwt_required
    @arg_is_instance_uuid
    @requires_instance_ownership
    def get(self, instance_uuid=None, instance_from_db=None):
        out = []
        for snap in Artifacts([partial(artifact.instance_snapshot_filter, instance_uuid)]):
            ev = snap.external_view_without_index()
            for idx in snap.get_all_indexes():
                # Give the blob uuid a better name
                b = Blob.from_db(idx['blob_uuid']).external_view()
                b['blob_uuid'] = b['uuid']
                del b['uuid']

                # Merge it with the parent artifact
                a = copy.copy(ev)
                a.update(b)
                out.append(a)
        return out


class LabelEndpoint(Resource):
    @jwt_required
    def post(self, label_name=None, blob_uuid=None):
        a = Artifact.from_url(
            Artifact.TYPE_LABEL, 'sf://label/%s/%s' % (get_jwt_identity(), label_name))
        if not a:
            a = Artifact.new(
                Artifact.TYPE_LABEL, 'sf://label/%s/%s' % (get_jwt_identity(), label_name))

        a.add_index(blob_uuid)
        a.state = dbo.STATE_CREATED
        return a.external_view()

    @jwt_required
    def get(self, label_name=None):
        a = Artifact.from_url(
            Artifact.TYPE_LABEL, 'sf://label/%s/%s' % (get_jwt_identity(), label_name))
        if not a:
            error(404, 'label %s not found' % label_name)

        return a.external_view()

    @jwt_required
    def delete(self, label_name=None):
        a = Artifact.from_url(
            Artifact.TYPE_LABEL, 'sf://label/%s/%s' % (get_jwt_identity(), label_name))
        if not a:
            error(404, 'label %s not found' % label_name)

        a.state = dbo.STATE_DELETED


class InstanceRebootSoft(Resource):
    @jwt_required
    @arg_is_instance_uuid
    @requires_instance_ownership
    @redirect_instance_request
    @requires_instance_active
    def post(self, instance_uuid=None, instance_from_db=None):
        with db.get_lock(
                'instance', None, instance_uuid, ttl=120, timeout=120,
                op='Instance reboot soft'):
            instance_from_db.add_event('api', 'soft reboot')
            return instance_from_db.reboot(hard=False)


class InstanceRebootHard(Resource):
    @jwt_required
    @arg_is_instance_uuid
    @requires_instance_ownership
    @redirect_instance_request
    @requires_instance_active
    def post(self, instance_uuid=None, instance_from_db=None):
        with db.get_lock(
                'instance', None, instance_uuid, ttl=120, timeout=120,
                op='Instance reboot hard'):
            instance_from_db.add_event('api', 'hard reboot')
            return instance_from_db.reboot(hard=True)


class InstancePowerOff(Resource):
    @jwt_required
    @arg_is_instance_uuid
    @requires_instance_ownership
    @redirect_instance_request
    @requires_instance_active
    def post(self, instance_uuid=None, instance_from_db=None):
        with db.get_lock(
                'instance', None, instance_uuid, ttl=120, timeout=120,
                op='Instance power off'):
            instance_from_db.add_event('api', 'poweroff')
            return instance_from_db.power_off()


class InstancePowerOn(Resource):
    @jwt_required
    @arg_is_instance_uuid
    @requires_instance_ownership
    @redirect_instance_request
    @requires_instance_active
    def post(self, instance_uuid=None, instance_from_db=None):
        with db.get_lock(
                'instance', None, instance_uuid, ttl=120, timeout=120,
                op='Instance power on'):
            instance_from_db.add_event('api', 'poweron')
            return instance_from_db.power_on()


class InstancePause(Resource):
    @jwt_required
    @arg_is_instance_uuid
    @requires_instance_ownership
    @redirect_instance_request
    @requires_instance_active
    def post(self, instance_uuid=None, instance_from_db=None):
        with db.get_lock(
                'instance', None, instance_uuid, ttl=120, timeout=120,
                op='Instance pause'):
            instance_from_db.add_event('api', 'pause')
            return instance_from_db.pause()


class InstanceUnpause(Resource):
    @jwt_required
    @arg_is_instance_uuid
    @requires_instance_ownership
    @redirect_instance_request
    @requires_instance_active
    def post(self, instance_uuid=None, instance_from_db=None):
        with db.get_lock(
                'instance', None, instance_uuid, ttl=120, timeout=120,
                op='Instance unpause'):
            instance_from_db.add_event('api', 'unpause')
            return instance_from_db.unpause()


def _safe_get_network_interface(interface_uuid):
    ni = NetworkInterface.from_db(interface_uuid)
    if not ni:
        return None, None, error(404, 'interface not found')

    log = LOG.with_fields({'network': ni.network_uuid,
                           'networkinterface': ni.uuid})

    n = net.Network.from_db(ni.network_uuid)
    if not n:
        log.info('Network not found or deleted')
        return None, None, error(404, 'interface network not found')

    if get_jwt_identity() not in [n.namespace, 'system']:
        log.info('Interface not found, failed ownership test')
        return None, None, error(404, 'interface not found')

    i = instance.Instance.from_db(ni.instance_uuid)
    if get_jwt_identity() not in [i.namespace, 'system']:
        log.with_object(i).info('Instance not found, failed ownership test')
        return None, None, error(404, 'interface not found')

    return ni, n, None


class Interface(Resource):
    @jwt_required
    @redirect_to_network_node
    def get(self, interface_uuid=None):
        ni, _, err = _safe_get_network_interface(interface_uuid)
        if err:
            return err
        return ni.external_view()


class InterfaceFloat(Resource):
    @jwt_required
    def post(self, interface_uuid=None):
        ni, n, err = _safe_get_network_interface(interface_uuid)
        if err:
            return err

        err = _assign_floating_ip(ni)
        if err:
            return err

        db.enqueue('networknode',
                   FloatNetworkInterfaceTask(n.uuid, interface_uuid))


class InterfaceDefloat(Resource):
    @jwt_required
    def post(self, interface_uuid=None):
        ni, n, err = _safe_get_network_interface(interface_uuid)
        if err:
            return err

        float_net = net.Network.from_db('floating')
        if not float_net:
            return error(404, 'floating network not found')

        # Address is freed as part of the job, so code is "unbalanced" compared
        # to above for reasons.
        db.enqueue('networknode',
                   DefloatNetworkInterfaceTask(n.uuid, interface_uuid))


class InstanceMetadatas(Resource):
    @jwt_required
    @arg_is_instance_uuid
    @requires_instance_ownership
    def get(self, instance_uuid=None, instance_from_db=None):
        md = db.get_metadata('instance', instance_uuid)
        if not md:
            return {}
        return md

    @jwt_required
    @arg_is_instance_uuid
    @requires_instance_ownership
    def post(self, instance_uuid=None, key=None, value=None, instance_from_db=None):
        return _metadata_putpost('instance', instance_uuid, key, value)


class InstanceMetadata(Resource):
    @jwt_required
    @arg_is_instance_uuid
    @requires_instance_ownership
    def put(self, instance_uuid=None, key=None, value=None, instance_from_db=None):
        return _metadata_putpost('instance', instance_uuid, key, value)

    @jwt_required
    @arg_is_instance_uuid
    @requires_instance_ownership
    def delete(self, instance_uuid=None, key=None, instance_from_db=None):
        if not key:
            return error(400, 'no key specified')

        with db.get_lock('metadata', 'instance', instance_uuid, op='Instance metadata delete'):
            md = db.get_metadata('instance', instance_uuid)
            if md is None or key not in md:
                return error(404, 'key not found')
            del md[key]
            db.persist_metadata('instance', instance_uuid, md)


class InstanceConsoleData(Resource):
    @jwt_required
    @arg_is_instance_uuid
    @requires_instance_ownership
    @redirect_instance_request
    def get(self, instance_uuid=None, length=None, instance_from_db=None):
        parsed_length = None

        if not length:
            parsed_length = -1
        else:
            try:
                parsed_length = int(length)
            except ValueError:
                pass

            # This is done this way so that there is no active traceback for
            # the error call, otherwise it would be logged.
            if parsed_length is None:
                return error(400, 'length is not an integer')

        resp = flask.Response(
            instance_from_db.get_console_data(parsed_length),
            mimetype='text/plain')
        resp.status_code = 200
        return resp


class Images(Resource):
    @jwt_required
    def get(self, node=None):
        f = []

        # If gluster is enabled, there is no concept of an image being on a
        # single node.
        if not config.GLUSTER_ENABLED and node:
            f.append(partial(images.placement_filter, node))

        retval = []
        for i in images.Images(filters=f):
            retval.append(i.external_view())
        return retval

    @jwt_required
    def post(self, url=None):
        db.add_event('image', url, 'api', 'cache', None, None)

        # We ensure that the image exists in the database in an initial state
        # here so that it will show up in image list requests. The image is
        # fetched by the queued job later.
        img = images.Image.new(url)
        db.enqueue(config.NODE_NAME, {
            'tasks': [FetchImageTask(url)],
        })
        return img.external_view()


class ImageEvents(Resource):
    @jwt_required
    # TODO(andy): Should images be owned? Personalised images should be owned.
    def get(self, url):
        return list(db.get_events('image', url))


def _delete_network(network_from_db):
    # Load network from DB to ensure obtaining correct lock.
    n = net.Network.from_db(network_from_db.uuid)
    if not n:
        LOG.with_fields({'network_uuid': n.uuid}).warning(
            'delete_network: network does not exist')
        return error(404, 'network does not exist')

    if n.is_dead():
        # The network has been deleted. No need to attempt further effort.
        LOG.with_fields({'network_uuid': n.uuid,
                         'state': n.state.value
                         }).warning('delete_network: network is dead')
        return error(404, 'network is deleted')

    n.add_event('api', 'delete')
    db.enqueue('networknode', DestroyNetworkTask(n.uuid))


class Network(Resource):
    @jwt_required
    @arg_is_network_uuid
    @requires_network_ownership
    def get(self, network_uuid=None, network_from_db=None):
        return network_from_db.external_view()

    @jwt_required
    @arg_is_network_uuid
    @requires_network_ownership
    @redirect_to_network_node
    def delete(self, network_uuid=None, network_from_db=None):
        if network_uuid == 'floating':
            return error(403, 'you cannot delete the floating network')

        n = net.Network.from_db(network_from_db.uuid)
        if not n:
            LOG.with_fields({'network_uuid': n.uuid}).warning(
                'delete_network: network does not exist')
            return error(404, 'network does not exist')

        # We only delete unused networks
        ifaces = list(networkinterface.interfaces_for_network(n))
        if len(ifaces) > 0:
            for iface in ifaces:
                LOG.withFields({'network_interface': iface.uuid,
                                'state': iface.state}).info('Blocks network delete')
            return error(403, 'you cannot delete an in use network')

        # Check if network has already been deleted
        if network_from_db.state.value in dbo.STATE_DELETED:
            return

        _delete_network(network_from_db)


class Networks(Resource):
    @marshal_with({
        'uuid': fields.String,
        'vxlan_id': fields.Integer,
        'netblock': fields.String,
        'provide_dhcp': fields.Boolean,
        'provide_nat': fields.Boolean,
        'namespace': fields.String,
        'name': fields.String,
        'state': fields.String
    })
    @jwt_required
    def get(self, all=False):
        filters = [partial(baseobject.namespace_filter, get_jwt_identity())]
        if not all:
            filters.append(baseobject.active_states_filter)

        retval = []
        for n in net.Networks(filters):
            # This forces the network through the external view rehydration
            retval.append(n.external_view())
        return retval

    @jwt_required
    def post(self, netblock=None, provide_dhcp=None, provide_nat=None, name=None,
             namespace=None):
        try:
            n = ipaddress.ip_network(netblock)
            if n.num_addresses < 8:
                return error(400, 'network is below minimum size of /29')
        except ValueError as e:
            return error(400, 'cannot parse netblock: %s' % e,
                         suppress_traceback=True)

        if not namespace:
            namespace = get_jwt_identity()

        # If accessing a foreign name namespace, we need to be an admin
        if get_jwt_identity() not in [namespace, 'system']:
            return error(
                401,
                'only admins can create resources in a different namespace')

        network = net.Network.new(name, namespace, netblock, provide_dhcp,
                                  provide_nat)
        return network.external_view()

    @jwt_required
    @redirect_to_network_node
    def delete(self, confirm=False, namespace=None):
        """Delete all networks in the namespace."""

        if confirm is not True:
            return error(400, 'parameter confirm is not set true')

        if get_jwt_identity() == 'system':
            if not isinstance(namespace, str):
                # A client using a system key must specify the namespace. This
                # ensures that deleting all networks in the cluster (by
                # specifying namespace='system') is a deliberate act.
                return error(400, 'system user must specify parameter namespace')

        else:
            if namespace and namespace != get_jwt_identity():
                return error(401, 'you cannot delete other namespaces')
            namespace = get_jwt_identity()

        networks_del = []
        networks_unable = []
        for n in net.Networks([partial(baseobject.namespace_filter, namespace),
                               baseobject.active_states_filter]):
            if len(list(networkinterface.interfaces_for_network(n))) > 0:
                LOG.with_object(n).warning(
                    'Network in use, cannot be deleted by delete-all')
                networks_unable.append(n.uuid)
                continue

            _delete_network(n)
            networks_del.append(n.uuid)

        if networks_unable:
            return error(403, {'deleted': networks_del,
                               'unable': networks_unable})

        return networks_del


class NetworkEvents(Resource):
    @jwt_required
    @arg_is_network_uuid
    @requires_network_ownership
    def get(self, network_uuid=None, network_from_db=None):
        return list(db.get_events('network', network_uuid))


class NetworkInterfacesEndpoint(Resource):
    @jwt_required
    @arg_is_network_uuid
    @requires_network_ownership
    @requires_network_active
    def get(self, network_uuid=None, network_from_db=None):
        out = []
        for ni in networkinterface.interfaces_for_network(self.network):
            out.append(ni.external_view())
        return out


class NetworkMetadatas(Resource):
    @jwt_required
    @arg_is_network_uuid
    @requires_network_ownership
    def get(self, network_uuid=None, network_from_db=None):
        md = db.get_metadata('network', network_uuid)
        if not md:
            return {}
        return md

    @jwt_required
    @arg_is_network_uuid
    @requires_network_ownership
    def post(self, network_uuid=None, key=None, value=None, network_from_db=None):
        return _metadata_putpost('network', network_uuid, key, value)


class NetworkMetadata(Resource):
    @jwt_required
    @arg_is_network_uuid
    @requires_network_ownership
    def put(self, network_uuid=None, key=None, value=None, network_from_db=None):
        return _metadata_putpost('network', network_uuid, key, value)

    @jwt_required
    @arg_is_network_uuid
    @requires_network_ownership
    def delete(self, network_uuid=None, key=None, network_from_db=None):
        if not key:
            return error(400, 'no key specified')

        with db.get_lock('metadata', 'network', network_uuid, op='Network metadata delete'):
            md = db.get_metadata('network', network_uuid)
            if md is None or key not in md:
                return error(404, 'key not found')
            del md[key]
            db.persist_metadata('network', network_uuid, md)


class NetworkPing(Resource):
    @jwt_required
    @arg_is_network_uuid
    @requires_network_ownership
    @redirect_to_network_node
    @requires_network_active
    def get(self, network_uuid=None, address=None, network_from_db=None):
        ipm = IPManager.from_db(network_uuid)
        if not ipm.is_in_range(address):
            return error(400, 'ping request for address outside network block')

        n = net.Network.from_db(network_uuid)
        if not n:
            return error(404, 'network %s not found' % network_uuid)

        out, err = util.execute(
            None, 'ip netns exec %s ping -c 10 %s' % (
                network_uuid, address),
            check_exit_code=[0, 1])
        return {
            'stdout': out,
            'stderr': err
        }


class NodesEndpoint(Resource):
    @jwt_required
    @caller_is_admin
    @marshal_with({
        'name': fields.String(attribute='fqdn'),
        'ip': fields.String,
        'lastseen': fields.Float,
        'version': fields.String,
    })
    def get(self):
        out = []
        for n in Nodes([]):
            out.append(n.external_view())
        return out


api.add_resource(Root, '/')

api.add_resource(AdminLocks, '/admin/locks')

api.add_resource(Auth, '/auth')
api.add_resource(AuthNamespaces, '/auth/namespaces')
api.add_resource(AuthNamespace, '/auth/namespaces/<namespace>')
api.add_resource(AuthNamespaceKeys,
                 '/auth/namespaces/<namespace>/keys')
api.add_resource(AuthNamespaceKey,
                 '/auth/namespaces/<namespace>/keys/<key_name>')
api.add_resource(AuthMetadatas, '/auth/namespaces/<namespace>/metadata')
api.add_resource(AuthMetadata,
                 '/auth/namespaces/<namespace>/metadata/<key>')

api.add_resource(BlobEndpoint, '/blob/<blob_uuid>')

api.add_resource(Instances, '/instances')
api.add_resource(Instance, '/instances/<instance_uuid>')
api.add_resource(InstanceEvents, '/instances/<instance_uuid>/events')
api.add_resource(InstanceInterfaces, '/instances/<instance_uuid>/interfaces')
api.add_resource(InstanceSnapshot, '/instances/<instance_uuid>/snapshot')
api.add_resource(InstanceRebootSoft, '/instances/<instance_uuid>/rebootsoft')
api.add_resource(InstanceRebootHard, '/instances/<instance_uuid>/reboothard')
api.add_resource(InstancePowerOff, '/instances/<instance_uuid>/poweroff')
api.add_resource(InstancePowerOn, '/instances/<instance_uuid>/poweron')
api.add_resource(InstancePause, '/instances/<instance_uuid>/pause')
api.add_resource(InstanceUnpause, '/instances/<instance_uuid>/unpause')
api.add_resource(Interface, '/interfaces/<interface_uuid>')
api.add_resource(InterfaceFloat, '/interfaces/<interface_uuid>/float')
api.add_resource(InterfaceDefloat, '/interfaces/<interface_uuid>/defloat')
api.add_resource(InstanceMetadatas, '/instances/<instance_uuid>/metadata')
api.add_resource(InstanceMetadata,
                 '/instances/<instance_uuid>/metadata/<key>')
api.add_resource(InstanceConsoleData, '/instances/<instance_uuid>/consoledata',
                 defaults={'length': 10240})

api.add_resource(Images, '/images')
api.add_resource(ImageEvents, '/images/events')

api.add_resource(LabelEndpoint, '/label/<label_name>')

api.add_resource(Networks, '/networks')
api.add_resource(Network, '/networks/<network_uuid>')
api.add_resource(NetworkEvents, '/networks/<network_uuid>/events')
api.add_resource(NetworkInterfacesEndpoint,
                 '/networks/<network_uuid>/interfaces')
api.add_resource(NetworkMetadatas, '/networks/<network_uuid>/metadata')
api.add_resource(NetworkMetadata,
                 '/networks/<network_uuid>/metadata/<key>')
api.add_resource(NetworkPing,
                 '/networks/<network_uuid>/ping/<address>')

api.add_resource(NodesEndpoint, '/nodes')
