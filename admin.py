#  This program is free software; you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation; either version 2 of the License, or
#  (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU Library General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program; if not, write to the Free Software
#  Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA 02111-1307, USA.
#
# admin.py
# Copyright (C) 2011 Simon Newton
# The handlers for the admin page.

import common
from data.controller_data import CONTROLLER_DATA
from data.manufacturer_data import MANUFACTURER_DATA
from data.model_data import DEVICE_MODEL_DATA
from data.product_categories import PRODUCT_CATEGORIES
import controller_loader
import datetime
import html_differ
import logging
import memcache_keys
import model_loader
import pid_data
import timestamp_keys
from google.appengine.api import memcache
from google.appengine.api import taskqueue
from google.appengine.api import users
from google.appengine.ext import webapp
from google.appengine.ext.blobstore import BlobInfo
from google.appengine.ext.webapp import template
from model import *
from utils import StringToInt
from pid_loader import PidLoader


def UpdateModificationTime(timestamp_name):
  """Update a particular timestamp."""
  query = LastUpdateTime.all()
  query.filter('name = ', timestamp_name)
  result = query.fetch(1)
  if not result:
    result = LastUpdateTime(name = timestamp_name)
  else:
    result = result[0]
  result.update_time = datetime.datetime.now()
  result.put()

  # delete the index info cache
  memcache.delete(memcache_keys.INDEX_INFO)


class BaseAdminPageHandler(webapp.RequestHandler):
  """The base handler for admin requests."""
  ALLOWED_USERS = [
      'nomis52@gmail.com',
      'simon@nomis52.net',
  ]

  def get(self):
    self.do_request()

  def post(self):
    self.do_request()

  def do_request(self):
    user = users.get_current_user()
    if not user:
      self.redirect(users.create_login_url(self.request.uri))
      return

    if user.email() not in self.ALLOWED_USERS:
      self.error(403)
      return

    self.HandleRequest()


class AdminPageHandler(BaseAdminPageHandler):
  """Admin functions."""
  def UpdateManufacturers(self):
    new_data = {}
    for id, name in MANUFACTURER_DATA:
      new_data[id] = name

    existing_manufacturers = set()
    manufacturers_to_delete = []
    # invalidate the cache now
    memcache.delete(memcache_keys.MANUFACTURER_CACHE_KEY)
    memcache.delete(memcache_keys.MANUFACTURER_MODEL_COUNTS)
    memcache.delete(memcache_keys.MANUFACTURER_PID_COUNT_KEY)
    added = removed = updated = errors = 0

    for manufacturer in Manufacturer.all():
      id = manufacturer.esta_id
      if id in new_data:
        existing_manufacturers.add(id)
        # update if required
        new_name = new_data[id]
        if new_name != manufacturer.name:
          logging.info('Updating %s -> %s' % (manufacturer.name, new_name))
          manufacturer.name = new_name
          manufacturer.put()
          updated += 1
      else:
        manufacturers_to_delete.append(manufacturer)

    # add any new manufacturers
    manufacturers_to_add = set(new_data.keys()) - existing_manufacturers
    for manufacturer_id in sorted(manufacturers_to_add):
      try:
        manufacturer_name = new_data[manufacturer_id].decode()
      except UnicodeDecodeError as e:
        logging.error('Failed to add 0x%hx: %s' % (manufacturer_id, e))
        errors += 1
        continue

      logging.info('adding %d (%s)' % (manufacturer_id, manufacturer_name))
      manufacturer = Manufacturer(esta_id = manufacturer_id,
                                  name = manufacturer_name)
      manufacturer.put()
      added += 1

    # remove any extra manufacturers
    for manufacturer in manufacturers_to_delete:
      logging.info('removing %s' % manufacturer.name)
      manufacturer.delete()
      removed += 1
    logging.info('update complete')
    UpdateModificationTime(timestamp_keys.MANUFACTURERS)
    return ('Manufacturers: added %d, removed %d, updated %d, errors %d' %
            (added, removed, updated, errors))

  def ClearPids(self):
    memcache.delete(memcache_keys.MANUFACTURER_PID_COUNT_KEY)
    memcache.delete(memcache_keys.MANUFACTURER_PID_COUNTS)
    for item in Command.all():
      item.delete()

    for item in Pid.all():
      item.delete()
    return ''

  def LoadPids(self):
    memcache.delete(memcache_keys.MANUFACTURER_PID_COUNTS)
    loader = PidLoader()
    added = 0
    for pid in pid_data.ESTA_PIDS:
      loader.AddPid(pid)
      added += 1
    UpdateModificationTime(timestamp_keys.PIDS)
    return 'Added %d PIDs' % added

  def RankDevices(self):
    task = taskqueue.Task(method='GET', url='/tasks/rank_devices')
    task.add()

  def LoadManufacturerPids(self):
    loader = PidLoader()
    added = 0
    memcache.delete(memcache_keys.MANUFACTURER_PID_COUNT_KEY)
    memcache.delete(memcache_keys.MANUFACTURER_PID_COUNTS)
    for manufacturer in pid_data.MANUFACTURER_PIDS:
      for pid in manufacturer['pids']:
        loader.AddPid(pid, manufacturer['id'])
        added += 1
    UpdateModificationTime(timestamp_keys.PIDS)
    return 'Added %d PIDs' % added

  def ClearModels(self):
    memcache.delete(memcache_keys.MODEL_COUNT_KEY)
    for item in Responder.all():
      item.delete()

    for item in SoftwareVersion.all():
      item.delete()

    for item in ResponderTag.all():
      item.delete()

    for item in ResponderTagRelationship.all():
      item.delete()
    return ''

  def UpdateModels(self):
    loader = model_loader.ModelLoader(DEVICE_MODEL_DATA)
    added, updated = loader.Update()
    if added or updated:
      memcache.delete(memcache_keys.INDEX_INFO)
      memcache.delete(memcache_keys.MODEL_COUNT_KEY)
      memcache.delete(memcache_keys.MANUFACTURER_MODEL_COUNTS)
      memcache.delete(memcache_keys.CATEGORY_MODEL_COUNTS)
      memcache.delete(memcache_keys.TAG_MODEL_COUNTS)

    UpdateModificationTime(timestamp_keys.DEVICES)
    return ('Models:\nAdded: %s\nUpdated: %s' %
            (', '.join(added), ', '.join(updated)))

  def UpdateProductCategories(self):
    """Update the list of Product Categories."""
    added = removed = updated = 0
    new_data = {}
    for name, id in PRODUCT_CATEGORIES.iteritems():
      new_data[id] = name

    existing_categories = set()
    categories_to_delete = []

    for category in ProductCategory.all():
      id = category.id
      if id in new_data:
        existing_categories.add(id)
        # update if required
        new_name = new_data[id]
        if new_name != category.name:
          logging.info('Updating %s -> %s' % (category.name, new_name))
          category.name = new_name
          category.put()
          updated += 1
      else:
        categories_to_delete.append(category)

    # add any new categories
    categories_to_add = set(new_data.keys()) - existing_categories
    for category_id in sorted(categories_to_add):
      logging.info('adding %d (%s)' %
                   (category_id, new_data[category_id]))
      category = ProductCategory(id = category_id,
                                 name = new_data[category_id])
      category.put()
      added += 1

    # remove any extra categories
    for category in categories_to_delete:
      logging.info('removing %s' % category.name)
      category.delete()
      removed += 1
    logging.info('update complete')
    return ('categories: added %d, removed %d, updated %d' %
            (added, removed, updated))

  def GarbageCollectTags(self):
    """Delete any tags that don't have Responders linked to them."""
    deleted_responder_tags = []
    for tag in ResponderTag.all():
      responders = tag.responder_set.fetch(1)
      if responders == []:
        deleted_responder_tags.append(tag.label)
        tag.delete()

    deleted_controller_tags = []
    for tag in ControllerTag.all():
      controllers = tag.controller_set.fetch(1)
      if controllers == []:
        deleted_controller_tags.append(tag.label)
        tag.delete()

    output = ''
    if deleted_responder_tags:
      output += ('Deleted Responder tags: \n%s\n' %
          '\n'.join(deleted_responder_tags))
    if deleted_controller_tags:
      output += ('Deleted Controller tags: \n%s\n' %
          '\n'.join(deleted_controller_tags))

    if output == '':
      output = 'No tags to delete'
    return output

  def GarbageCollectBlobs(self):
    keys_to_blobs = {}
    for blob in BlobInfo.all():
      keys_to_blobs[blob.key()] = blob

    for responder in Responder.all():
      image_blob = responder.image_data
      if image_blob:
        key = image_blob.key()
        if key in keys_to_blobs:
          del keys_to_blobs[key]

    for controller in Controller.all():
      image_blob = controller.image_data
      if image_blob:
        key = image_blob.key()
        if key in keys_to_blobs:
          del keys_to_blobs[key]

    for key, blob_info in keys_to_blobs.iteritems():
      logging.info('deleting %s' % key)
      blob_info.delete()

    if keys_to_blobs:
      return 'Deleted blobs: \n%s' % '\n'.join(str(k) for k in keys_to_blobs)
    else:
      return 'No blobs to delete'

  def InitiateImageFetch(self):
    """Add /fetch_image tasks for all responders missing image data."""
    urls = []
    for responder in Responder.all():
      if responder.image_url and not responder.image_data:
        url = '/tasks/fetch_image?key=%s' % responder.key()
        task = taskqueue.Task(method='GET', url=url)
        task.add()
        urls.append(responder.image_url)

    for controller in Controller.all():
      if controller.image_url and not controller.image_data:
        url = '/tasks/fetch_controller_image?key=%s' % controller.key()
        task = taskqueue.Task(method='GET', url=url)
        task.add()
        urls.append(controller.image_url)

    if urls:
      return 'Fetching urls: \n%s' % '\n'.join(urls)
    else:
      return 'No images to fetch'

  def ClearControllers(self):
    for item in Controller.all():
      item.delete()

    for item in ControllerTag.all():
      item.delete()

    for item in ControllerTagRelationship.all():
      item.delete()
    return ''

  def UpdateControllers(self):
    loader = controller_loader.ControllerLoader(CONTROLLER_DATA)
    added, updated = loader.Update()
    if added or updated:
      memcache.delete(memcache_keys.MANUFACTURER_CONTROLLER_COUNTS)
      memcache.delete(memcache_keys.TAG_CONTROLLER_COUNTS)
      pass

    UpdateModificationTime(timestamp_keys.CONTROLLERS)
    return ('Controllers:\nAdded: %s\nUpdated: %s' %
            (', '.join(added), ', '.join(updated)))

  def HandleRequest(self):
    ACTIONS = {
        'clear_controllers': self.ClearControllers,
        'clear_models': self.ClearModels,
        'clear_p': self.ClearPids,
        'gc_blobs': self.GarbageCollectBlobs,
        'gc_tags': self.GarbageCollectTags,
        'initiate_image_fetch': self.InitiateImageFetch,
        'load_mp': self.LoadManufacturerPids,
        'load_p': self.LoadPids,
        'rank_devices': self.RankDevices,
        'update_categories': self.UpdateProductCategories,
        'update_m': self.UpdateManufacturers,
        'update_models': self.UpdateModels,
        'update_controllers': self.UpdateControllers,
    }

    action = self.request.get('action')
    output = ''
    if action in ACTIONS:
      output = ACTIONS[action]()

    pending_uploads = UploadedResponderInfo.all().filter('processed = ',
                                                         False).count()
    template_data = {
        'logout_url': users.create_logout_url("/"),
        'responders_to_moderate': pending_uploads,
    }

    if output:
      template_data['output'] = output
    self.response.headers['Content-Type'] = 'text/html'
    self.response.out.write(template.render('templates/admin.tmpl',
                                            template_data))


class ResponderModerator(BaseAdminPageHandler):
  """Displays the UI for moderating responder data."""
  def EvalData(self, data):
    try:
      evaled_data = eval(data, {})
      return evaled_data
    except Exception as e:
      logging.info(data)
      logging.error(e)
      return {}

  def ApplyChanges(self, key, fields):
    responder_info = UploadedResponderInfo.get(key)
    if not responder_info:
      return 'Invalid key'
    logging.info(responder_info)

    fields_to_update = set(fields.split(','))
    logging.info(fields_to_update)
    data_dict = self.EvalData(responder_info.info)

    # same format as in data/model_data.py
    model_data = {
      'device_model': responder_info.device_model_id
    }

    if ('model_description' in fields_to_update and
        'model_description' in data_dict):
      model_data['model_description'] = data_dict['model_description']
    if 'image_url' in fields_to_update and responder_info.image_url:
      model_data['image_url'] = responder_info.image_url
    if 'url' in fields_to_update and responder_info.link_url:
      model_data['link'] = responder_info.link_url
    if ('product_category' in fields_to_update and
        'product_category' in data_dict):
      model_data['product_category'] = data_dict['product_category']

    if 'software_versions' in data_dict:
      for version_id, version_data in data_dict['software_versions'].iteritems():
        if type(version_id) in (int, long):
          version_dict = self.BuildVersionDict(version_id, version_data,
                                               fields_to_update)
          if version_dict:
            versions = model_data.setdefault('software_versions', {})
            versions[version_id] = version_dict

    logging.info(model_data)

    manufacturer = common.GetManufacturer(responder_info.manufacturer_id)
    if manufacturer is None:
      return 'Invalid manufacturer_id %d' % responder_info.manufacturer_id

    updater = model_loader.ModelUpdater()
    was_added, was_changed = updater.UpdateResponder(manufacturer, model_data)
    logging.info('Was added %s' % was_added)
    logging.info('Was changed %s' % was_changed)

    # finally mark this one as done
    responder_info.processed = True
    responder_info.put()
    return ''

  def BuildVersionDict(self, version_id, version_data, fields_to_update):
    version_dict = {}

    # sort supported params just in case
    if 'supported_parameters' in version_data:
      version_data['supported_parameters'].sort()
    logging.info(version_data)

    fields = ['label', 'personalities', 'sensors', 'supported_parameters']
    for field in fields:
      if (('%d_%s' % (version_id, field)) in fields_to_update and
          field in version_data):
        version_dict[field] = version_data[field]
    return version_dict

  def HandleRequest(self):
    template_data = {
      'logout_url': users.create_logout_url("/"),
    }

    # this is a bit of a hack
    self._differ = html_differ.HTMLDiffer('left', 'right')

    key = self.request.get('key')
    fields = self.request.get('fields')
    if key and fields is not None:
      error = self.ApplyChanges(key, fields)
      if error:
        template_data.setdefault('errors', []).append(error)

    query = UploadedResponderInfo.all()
    query.filter('processed = ', False)
    responder = query.fetch(1)
    if responder:
      template_data['key'] = responder[0].key()
      self.DiffResponder(responder[0], template_data)

    self.response.headers['Content-Type'] = 'text/html'
    self.response.out.write(template.render(
      'templates/admin-moderate-responder.tmpl',
      template_data))

  def DiffProperty(self, name, key, left_dict, right_dict):
    """
    Args:
      name: The human name of this property
      key: the key by which to look up this property in both dicts.
      left_dict:
      right_dict:

    Returns:
      A dict in the form {

      }
    """
    left = left_dict.get(key)
    right = right_dict.get(key)
    if left and right:
      left_formated, right_formatted = self._differ.Diff(str(left), str(right))
    else:
      left_formated = left
      right_formatted = right
    return  {
      'name': name,
      'key': key,
      'left': left_formated,
      'different': left != right,
      'prefer_left': left is not None and right is None,
      'prefer_right': right is not None and left is None,
      'right': right_formatted,
    }

  def DiffProperties(self, fields, left_dict, right_dict):
    """

    Returns:
      changed_fields, unchanged_fields
    """
    changed_fields = []
    unchanged_fields = []
    for name, key in fields:
      field_dict = self.DiffProperty(name, key, left_dict, right_dict)
      if field_dict['different']:
        changed_fields.append(field_dict)
      else:
        unchanged_fields.append(field_dict)
    return changed_fields, unchanged_fields

  def DiffResponder(self, responder, template_data):
    errors = []

    template_data['device_id'] = responder.device_model_id
    template_data['manufacturer_id'] = responder.manufacturer_id

    if responder.email_or_name:
      template_data['contact'] = responder.email_or_name

    manufacturer = common.GetManufacturer(responder.manufacturer_id)
    template_data['manufacturer'] = manufacturer
    if not manufacturer:
      return

    template_data['manufacturer_name'] = manufacturer.name
    existing_model = common.LookupModel(responder.manufacturer_id,
                                        responder.device_model_id)

    # build a dict for the existing responder
    existing_responder_dict = {}
    if existing_model is not None:
      existing_responder_dict = {
          'model_description': existing_model.model_description,
          'image_url': existing_model.image_url,
          'url': existing_model.link,
      }
      category = existing_model.product_category
      if category:
        existing_responder_dict['product_category'] = category.name

    # Build a dict for the new responder
    new_responder_dict = self.EvalData(responder.info)
    new_responder_dict['image_url'] = responder.image_url or None
    new_responder_dict['url'] = responder.link_url or None
    if 'product_category' in new_responder_dict:
      category = common.LookupProductCategory(
          new_responder_dict['product_category'])
      if category:
        new_responder_dict['product_category'] = category.name
      else:
        errors.append('Unknown product category %d' %
          new_responder_dict['product_category'])

    fields = [
        ('Model Description', 'model_description'),
        ('Image URL', 'image_url'),
        ('URL', 'url'),
        ('Product Category', 'product_category'),
    ]

    changed_fields, unchanged_fields = self.DiffProperties(fields,
        new_responder_dict, existing_responder_dict)

    template_data['changed_fields'] = changed_fields
    template_data['unchanged_fields'] = unchanged_fields

    # populate the model_description
    template_data['model_description'] = new_responder_dict.get(
        'model_description')
    if existing_model:
      template_data['model_description'] = existing_model.model_description

    # now work on the software versions
    new_software_versions = new_responder_dict.get('software_versions', {})
    if new_software_versions:
      versions = self.DiffVersions(new_software_versions, existing_model)
      template_data['versions'] = versions

    template_data.setdefault('errors', []).extend(errors)

  def DiffVersions(self, new_software_versions, existing_responder):
    """

    Args:
      new_software_versions: a dict of version_id : dict mappings
      existing_responder: A Responder Entity, or None

    Returns:
      [{
        'version': <int>'
        'fields': [ <fields> ],
      },
      ]
    """
    known_versions = {}  # version_id : SoftwareVersion mapping
    if existing_responder:
      for known_version in existing_responder.software_version_set:
        known_versions[known_version.version_id] = known_version

    output = []

    for version_id, data in new_software_versions.iteritems():
      if type(version_id) in (int, long):
        fields = self.DiffVersion(version_id, data,
                                  known_versions.get(version_id))
        if fields:
          output.append({
            'version': version_id,
            'fields': fields,
          })
      else:
        logging.error('Invalid version id %s' % version_id)
    return output

  def DiffVersion(self, version_id, new_data, current_version):
    """

    Args:
      version_id: The software version
      new_data: The dict of new data
      existing_version: A SoftwareVersion Entity or None
    """
    current_version_dict = {
      'personalities': self.BuildPersonalityList(current_version),
      'sensors': self.BuildSensorList(current_version),
    }
    if current_version:
      current_version_dict['label'] = current_version.label
      # we need to convert to int
      current_version_dict['supported_parameters'] = sorted(
          int(i) for i in current_version.supported_parameters)

    # sort supported params just in case
    if 'supported_parameters' in new_data:
      new_data['supported_parameters'].sort()

    fields = [
        ('Software Label', 'label'),
        ('Supported Parameters', 'supported_parameters'),
        ('Personalities', 'personalities'),
        ('Sensors', 'sensors'),
    ]

    changed_fields, unchanged_fields = self.DiffProperties(fields,
        new_data, current_version_dict)
    return changed_fields

  def BuildPersonalityList(self, software_version):
    if software_version is None:
      return None

    personalities = []
    for personality in software_version.personality_set:
      personalities.append({
        'index': int(personality.index),
        'description': str(personality.description),
        'slot_count': int(personality.slot_count),
    })
    personalities.sort(key=lambda i: i['index'])
    return personalities

  def BuildSensorList(self, software_version):
    if software_version is None:
      return None

    sensors = []
    for sensor in software_version.sensor_set:
      recording = 0
      if sensor.supports_recording:
        recording |= 1
      if sensor.supports_min_max_recording:
        recording |= 2

      sensors.append({
        'description': str(sensor.description),
        'index': int(sensor.index),
        'supports_recording': recording,
        'type': int(sensor.type),
    })
    sensors.sort(key=lambda i: i['index'])
    return sensors


class AdjustTestScore(BaseAdminPageHandler):
  """Displays the UI for adjusting a responder's test score.
    TODO(simon): automate all of this.
  """
  def HandleRequest(self):
    template_data = {
      'logout_url': users.create_logout_url("/"),
      'message': '',
    }

    responder = common.LookupModelFromRequest(self.request)
    rating = self.request.get('rating')
    if responder is not None and rating is not None:
      rating_int = StringToInt(rating, False)
      if rating_int >= 0 and rating_int <= 100:
        template_data['message'] = (
            'Set rating of %s to %d' %
            (responder.model_description, rating_int))
        responder.rdm_responder_rating = db.Rating(rating_int)
        responder.put()

    self.response.headers['Content-Type'] = 'text/html'
    self.response.out.write(template.render(
      'templates/admin-adjust-test-score.tmpl',
      template_data))


app = webapp.WSGIApplication(
  [
    ('/admin', AdminPageHandler),
    ('/admin/moderate_responder_data', ResponderModerator),
    ('/admin/adjust_test_score', AdjustTestScore),
  ],
  debug=True)
