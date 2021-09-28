import flask
from flask_jwt_extended import get_jwt_identity
from flask_jwt_extended import jwt_required
import json
import os
import requests
import time
import uuid

from shakenfist.artifact import Artifact, Artifacts, UPLOAD_URL
from shakenfist.blob import Blob
from shakenfist import baseobject
from shakenfist import constants
from shakenfist.daemons import daemon
from shakenfist.external_api import base as api_base
from shakenfist.config import config
from shakenfist import db
from shakenfist import etcd
from shakenfist import images
from shakenfist import logutil
from shakenfist.tasks import FetchImageTask
from shakenfist.upload import Upload
from shakenfist.util import general as util_general


LOG, HANDLER = logutil.setup(__name__)
daemon.set_log_level(LOG, 'api')


def arg_is_artifact_uuid(func):
    def wrapper(*args, **kwargs):
        if 'artifact_uuid' in kwargs:
            kwargs['artifact_from_db'] = Artifact.from_db(
                kwargs['artifact_uuid'])
        if not kwargs.get('artifact_from_db'):
            LOG.with_field('artifact', kwargs['artifact_uuid']).info(
                'Artifact not found, missing or deleted')
            return api_base.error(404, 'artifact not found')

        return func(*args, **kwargs)
    return wrapper


class ArtifactEndpoint(api_base.Resource):
    @jwt_required
    @arg_is_artifact_uuid
    def get(self, artifact_uuid=None, artifact_from_db=None):
        return artifact_from_db.external_view()


class ArtifactsEndpoint(api_base.Resource):
    @jwt_required
    def get(self, node=None):
        retval = []
        for i in Artifacts(filters=[baseobject.active_states_filter]):
            b = i.most_recent_index
            if b:
                if not node:
                    retval.append(i.external_view())
                elif node in b.locations:
                    retval.append(i.external_view())
        return retval

    @jwt_required
    def post(self, url=None):
        # The only artifact type you can force the cluster to fetch is an
        # image, so TYPE_IMAGE is assumed here.
        db.add_event('image', url, 'api', 'cache', None, None)

        # We ensure that the image exists in the database in an initial state
        # here so that it will show up in image list requests. The image is
        # fetched by the queued job later.
        a = Artifact.from_url(Artifact.TYPE_IMAGE, url)
        etcd.enqueue(config.NODE_NAME, {
            'tasks': [FetchImageTask(url)],
        })
        return a.external_view()


class ArtifactUploadEndpoint(api_base.Resource):
    @jwt_required
    def post(self, artifact_name=None, upload_uuid=None):
        url = '%s%s/%s' % (UPLOAD_URL, get_jwt_identity(), artifact_name)
        a = Artifact.from_url(Artifact.TYPE_IMAGE, url)
        u = Upload.from_db(upload_uuid)
        if not u:
            return api_base.error(404, 'upload not found')

        if u.node != config.NODE_NAME:
            url = 'http://%s:%d%s' % (u.node, config.API_PORT,
                                      flask.request.environ['PATH_INFO'])
            api_token = util_general.get_api_token(
                'http://%s:%d' % (u.node, config.API_PORT),
                namespace=get_jwt_identity())
            r = requests.request(
                flask.request.environ['REQUEST_METHOD'], url,
                data=json.dumps(api_base.flask_get_post_body()),
                headers={'Authorization': api_token,
                         'User-Agent': util_general.get_user_agent()})

            LOG.info('Proxied %s %s returns: %d, %s' % (
                     flask.request.environ['REQUEST_METHOD'], url,
                     r.status_code, r.text))
            resp = flask.Response(r.text,  mimetype='application/json')
            resp.status_code = r.status_code
            return resp

        with a.get_lock(ttl=(12 * constants.LOCK_REFRESH_SECONDS),
                        timeout=config.MAX_IMAGE_TRANSFER_SECONDS) as lock:
            helper = images.ImageFetchHelper(None, url)

            blob_uuid = str(uuid.uuid4())
            blob_dir = os.path.join(config.STORAGE_PATH, 'blobs')
            blob_path = os.path.join(blob_dir, blob_uuid)

            upload_dir = os.path.join(config.STORAGE_PATH, 'uploads')
            upload_path = os.path.join(upload_dir, u.uuid)

            os.rename(upload_path, blob_path)
            st = os.stat(blob_path)
            b = Blob.new(
                blob_uuid, st.st_size,
                time.strftime('%a, %d %b %Y %H:%M:%S GMT', time.gmtime()),
                time.time())
            b.observe()
            helper.transcode_image(lock, b)

            a.add_event('upload', None, None, 'success')
            return a.external_view()


class ArtifactEventsEndpoint(api_base.Resource):
    @jwt_required
    # TODO(andy): Should images be owned? Personalised images should be owned.
    def get(self, artifact_uuid):
        return list(db.get_events('artifact', artifact_uuid))
