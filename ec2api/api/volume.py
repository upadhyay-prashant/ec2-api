# Copyright 2014
# The Cloudscaling Group, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from cinderclient import exceptions as cinder_exception
from novaclient import exceptions as nova_exception

from ec2api.api import clients
from ec2api.api import common
from ec2api.api import ec2utils
from ec2api.db import api as db_api
from ec2api import exception
from ec2api.i18n import _


"""Volume related API implementation
"""


Validator = common.Validator


def create_volume(context, availability_zone=None, size=None,
                  snapshot_id=None, volume_type=None, iops=None,
                  encrypted=None, kms_key_id=None):
    if snapshot_id is not None:
        snapshot = ec2utils.get_db_item(context, snapshot_id)
        os_snapshot_id = snapshot['os_id']
    else:
        os_snapshot_id = None

    cinder = clients.cinder(context)
    with common.OnCrashCleaner() as cleaner:
        os_volume = cinder.volumes.create(
                size, snapshot_id=os_snapshot_id, volume_type=volume_type,
                availability_zone=availability_zone)
        cleaner.addCleanup(os_volume.delete)

        #volume = db_api.add_item(context, 'vol', {'os_id': os_volume.id})
        cleaner.addCleanup(db_api.delete_item, context, os_volume.id)
        #os_volume.update(display_name=volume['id'])

    return _format_volume(context, os_volume, snapshot_id=snapshot_id)


def show_delete_on_termination_flag(context, volume_id):
    #volume = ec2utils.get_db_item(context, volume_id)
    #if not volume:
    #    _msg = ("No volume found corresponding to volume_id=" + volume_id)
    #    raise exception.InvalidRequest(_msg)
    #volume_id = volume['os_id']
    nova = clients.nova(context)
    try:
        response = nova.volumes.show_delete_on_termination_flag(volume_id)
        return {"volume": response._info}
    except (nova_exception.Conflict, nova_exception.BadRequest):
        # TODO(anant): raise correct errors for different cases
        raise exception.UnsupportedOperation()


def update_delete_on_termination_flag(context, volume_id, 
                                   delete_on_termination):
    #volume = ec2utils.get_db_item(context, volume_id)
    #if not volume:
    #    _msg = ("No volume found corresponding to volume_id=" + volume_id)
    #    raise exception.InvalidRequest(_msg)
    #volume_id = volume['os_id']
    nova = clients.nova(context)
    try:
        response = nova.volumes.update_delete_on_termination_flag(volume_id,
                                                 str(delete_on_termination))
        return {"volume": response._info}
    except (nova_exception.Conflict, nova_exception.BadRequest):
        # TODO(anant): raise correct errors for different cases
        raise exception.UnsupportedOperation()


def attach_volume(context, volume_id, instance_id, device):
    #volume = ec2utils.get_db_item(context, volume_id)
    instance = ec2utils.get_db_item(context, instance_id)

    nova = clients.nova(context)
    try:
        nova.volumes.create_server_volume(instance['os_id'], volume_id,
                                          device)
    except (nova_exception.Conflict, nova_exception.BadRequest):
        # TODO(andrey-mp): raise correct errors for different cases
        raise exception.UnsupportedOperation()
    cinder = clients.cinder(context)
    os_volume = cinder.volumes.get(volume_id)
    # [varun]: Sending delete on termination as false (last param below)
    # when volume is attached delete on termination flag will be false by
    # default therefore sending false to make consistent with AWS
    return _format_attachment(context, os_volume,
                              instance_id=instance_id,
                              delete_on_termination_flag=False)


def detach_volume(context, volume_id, instance_id=None, device=None,
                  force=None):
    #volume = ec2utils.get_db_item(context, volume_id)

    cinder = clients.cinder(context)
    os_volume = cinder.volumes.get(volume_id)
    os_instance_id = next(iter(os_volume.attachments), {}).get('server_id')
    if not os_instance_id:
        # TODO(ft): Change the message with the real AWS message
        reason = _('Volume %(vol_id)s is not attached to anything')
        raise exception.IncorrectState(reason=reason % {'vol_id': volume_id})

    nova = clients.nova(context)
    nova.volumes.delete_server_volume(os_instance_id, os_volume.id)
    os_volume.get()
    instance_id = next((i['id'] for i in db_api.get_items(context, 'i')
                        if i['os_id'] == os_instance_id), None)
    # [varun]: Sending delete on termination as false (last param below)
    # when volume is detached delete on termination flag does not make sense
    # therefore sending false to make consistent with AWS
    return _format_attachment(context, os_volume,
                              instance_id=instance_id,
                              delete_on_termination_flag=False)


def delete_volume(context, volume_id):
    #volume = ec2utils.get_db_item(context, volume_id)
    cinder = clients.cinder(context)
    try:
        cinder.volumes.delete(volume_id)
    except cinder_exception.BadRequest:
        # TODO(andrey-mp): raise correct errors for different cases
        raise exception.UnsupportedOperation()
    except cinder_exception.NotFound:
        pass
    # NOTE(andrey-mp) Don't delete item from DB until it disappears from Cloud
    # It will be deleted by describer in the future
    return True

class VolumeDescriber(common.TaggableItemsDescriber):

    KIND = 'vol'
    FILTER_MAP = {'availability-zone': 'availabilityZone',
                  'create-time': 'createTime',
                  'encrypted': 'encrypted',
                  'size': 'size',
                  'snapshot-id': 'snapshotId',
                  'status': 'status',
                  'volume-id': 'volumeId',
                  'volume-type': 'volumeType',
                  'attachment.device': ['attachmentSet', 'device'],
                  'attachment.instance-id': ['attachmentSet', 'instanceId'],
                  'attachment.status': ['attachmentSet', 'status']}

    def format(self, os_volume):
        return _format_volume(self.context, os_volume,
                              self.instances, self.snapshots)

    def get_db_items(self):
        self.instances = {i['os_id']: i
                          for i in db_api.get_items(self.context, 'i')}
        self.snapshots = {s['os_id']: s
                          for s in db_api.get_items(self.context, 'snap')}
        return super(VolumeDescriber, self).get_db_items()

    def get_os_items(self):
        return clients.cinder(self.context).volumes.list()

    def get_name(self, os_item):
        return ''


def describe_volumes(context, volume_id=None, filter=None,
                     max_results=None, next_token=None):
    formatted_volumes = VolumeDescriber().describe(
        context, ids=volume_id, filter=filter)
    return {'volumeSet': formatted_volumes}


def _format_volume(context, os_volume, instances={},
                   snapshots={}, snapshot_id=None):
    valid_ec2_api_volume_status_map = {
        'attaching': 'in-use',
        'detaching': 'in-use'}

    ec2_volume = {
            'volumeId': os_volume.id,
            'status': valid_ec2_api_volume_status_map.get(os_volume.status,
                                                          os_volume.status),
            'size': os_volume.size,
            'availabilityZone': os_volume.availability_zone,
            'createTime': os_volume.created_at,
            'volumeType': os_volume.volume_type,
            'encrypted': os_volume.encrypted,
    }
    if ec2_volume['status'] == 'in-use':
        ec2_volume['attachmentSet'] = (
                [_format_attachment(context, volume, os_volume, instances)])
    else:
        ec2_volume['attachmentSet'] = {}
    if snapshot_id is None and os_volume.snapshot_id:
        snapshot = ec2utils.get_db_item_by_os_id(
                context, 'snap', os_volume.snapshot_id, snapshots)
        snapshot_id = snapshot['id']
    ec2_volume['snapshotId'] = snapshot_id

    return ec2_volume


def _format_attachment(context, os_volume, instances={},
                       instance_id=None, delete_on_termination_flag=False):
    os_attachment = next(iter(os_volume.attachments), {})
    os_instance_id = os_attachment.get('server_id')
    if not instance_id and os_instance_id:
        instance = ec2utils.get_db_item_by_os_id(
                context, 'i', os_instance_id, instances)
        instance_id = instance['id']
    ec2_attachment = {
            'device': os_attachment.get('device'),
            'instanceId': instance_id,
            'status': (os_volume.status
                       if os_volume.status in ('attaching', 'detaching') else
                       'attached' if os_attachment else 'detached'),
            'volumeId': os_volume.id,
            'deleteOnTermination': delete_on_termination_flag}
    return ec2_attachment
