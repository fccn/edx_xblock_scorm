#!/usr/bin/env python
# -*- coding: utf-8 -*-

import mimetypes
import re
import pkg_resources
import zipfile
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlparse, unquote
import boto3

from os import path, walk

from django.conf import settings
from django.template import Context, Template
from webob import Response

from celery import task
from fs.tempfs import TempFS
from djpyfs import djpyfs

from xblock.core import XBlock
from xblock.fields import Scope, String, Float, Boolean, Dict
from xblock.fragment import Fragment


# Make '_' a no-op so we can scrape strings
_ = lambda text: text
FILES_THRESHOLD_FOR_ASYNC = getattr(settings, 'SCORMXBLOCK_ASYNC_THRESHOLD', 150)
DEFAULT_CONTENT_TYPE = 'application/octet-stream'

WRITE_MODEL_DATA = [
    'cmi.core.lesson_location',
    'cmi.core.lesson_status',
    'cmi.core.score.raw',
    'cmi.core.score.max',
    'cmi.core.score.min',
    'cmi.core.exit',
    'cmi.core.session_time',
    'cmi.suspend_data',
    'cmi.comments',
    'cmi.objectives.n.id',
    'cmi.objectives.n.score.raw',
    'cmi.objectives.n.score.max',
    'cmi.objectives.n.score.min',
    'cmi.objectives.n.status',
    'cmi.student_preference.audio',
    'cmi.student_preference.language',
    'cmi.student_preference.speed',
    'cmi.student_preference.text',
    'cmi.interactions.n.id',
    'cmi.interactions.n.objectives.n.id',
    'cmi.interactions.n.time',
    'cmi.interactions.n.type'
    'cmi.interactions.n.correct_responses.n.pattern',
    'cmi.interactions.n.weighting',
    'cmi.interactions.n.student_response',
    'cmi.interactions.n.result',
    'cmi.interactions.n.latency',
]

READ_MODEL_DATA = [
    'cmi.core._children',
    'cmi.core.student_id',
    'cmi.core.student_name',
    'cmi.core.credit',
    'cmi.core.entry',
    'cmi.core.score_children',
    'cmi.core.lesson_status',
    'cmi.core.score.raw',
    'cmi.core.lesson_location',
    'cmi.core.score.max',
    'cmi.core.score.min',
    'cmi.core.total_time',
    'cmi.core.lesson_mode',
    'cmi.suspend_data',
    'cmi.launch_data',
    'cmi.comments',
    'cmi.comments_from_lms',
    'cmi.objectives._children',
    'cmi.objectives._count',
    'cmi.objectives.n.id',
    'cmi.objectives.n.score._children',
    'cmi.objectives.n.score.raw',
    'cmi.objectives.n.score.max',
    'cmi.objectives.n.score.min',
    'cmi.objectives.n.status',
    'cmi.student_data._children',
    'cmi.student_data.mastery_score',
    'cmi.student_data.max_time_allowed',
    'cmi.student_data.time_limit_action',
    'cmi.student_preference._children',
    'cmi.student_preference.audio',
    'cmi.student_preference.language',
    'cmi.student_preference.speed',
    'cmi.student_preference.text',
    'cmi.interactions._children',
    'cmi.interactions._count',
    'cmi.interactions.n.objectives._count',
    'cmi.interactions.n.correct_responses._count',
]


@task(name='scormxblock.scormxblock.s3_upload', routing_key=settings.HIGH_PRIORITY_QUEUE)
def s3_upload(all_content, temp_directory, dest_dir):
    """
    Actual handling of the s3 uploads.
    """
    session = boto3.Session(
        aws_access_key_id=settings.DJFS.get('aws_access_key_id'),
        aws_secret_access_key=settings.DJFS.get('aws_secret_access_key'),
        region_name=settings.DJFS.get('region_name'),
    )
    s3_client = session.resource('s3')
    bucket = s3_client.Bucket(settings.DJFS.get('bucket'))

    for filepath in all_content:
        sourcepath = path.normpath(path.join(temp_directory.root_path, filepath))
        destpath = path.normpath(path.join(dest_dir, filepath))
        content_type = mimetypes.guess_type(sourcepath)[0]

        if not content_type:  # It's possible that the type is not in the mimetypes list.
            content_type = DEFAULT_CONTENT_TYPE

        if isinstance(content_type, bytes):  # In some versions of Python guess_type, it returns bytes instead of str.
            content_type = content_type.decode('utf-8')

        bucket.upload_file(
            sourcepath,
            destpath,
            ExtraArgs={'ACL': 'public-read', 'ContentType': content_type},
        )


def updoad_all_content(temp_directory, fs):
    """
    This standalone function handles the bulk upload of unzipped content.
    """
    if not settings.DJFS.get('type', 'osfs') == "s3fs":
        # Temporary fix
        # TODO: find a better solution for ImportError: No module named fs.utils
        from fs.copy import copy_fs
        copy_fs(temp_directory, fs)
        return

    dest_dir = fs.dir_path
    all_content = []
    for dir_, _, files in walk(temp_directory.root_path):
        for filename in files:
            rel_dir = path.relpath(dir_, temp_directory.root_path)
            rel_file = path.join(rel_dir, filename)
            all_content.append(rel_file)

    if len(all_content) < FILES_THRESHOLD_FOR_ASYNC:
        # We estimate no problem here, just upload the files
        s3_upload(all_content, temp_directory, dest_dir)
    else:
        # The raw number of files is going to make this request time out. Use celery instead
        s3_upload.delay(all_content, temp_directory, dest_dir)


def validate_property(key, valid_keys):
    """
    This replaces variable values on the key and verifies if it exist on valid_keys parameter.
    **Example**
        key = cmi.objectives.555.id
        valid_keys = {
            "cmi.objectives.n.id",
            "cmi.objectives.n.score"
        }
        In this case the new key will be "cmi.objectives.n.id" and it will return True
    """
    words_in_key = key.rsplit(".")
    len_words = len(words_in_key)

    if len_words == 3:
        return key in valid_keys
    elif len_words == 4:
        new_key = "{}.{}.n.{}".format(words_in_key[0], words_in_key[1], words_in_key[3])
        return new_key in valid_keys
    elif len_words == 5:
        new_key = "{}.{}.n.{}.{}".format(words_in_key[0], words_in_key[1], words_in_key[3], words_in_key[4])
        return new_key in valid_keys
    elif len_words == 6:
        new_key = "{}.{}.n.{}.n.{}".format(words_in_key[0], words_in_key[1], words_in_key[3], words_in_key[5])
        return new_key in valid_keys
    return False


class ScormXBlock(XBlock):

    display_name = String(
        display_name=_("Display Name"),
        help=_("Display name for this module"),
        default="Scorm",
        scope=Scope.settings,
    )
    scorm_file = String(
        display_name=_("Upload scorm file"),
        scope=Scope.settings,
    )
    version_scorm = String(
        default="SCORM_12",
        scope=Scope.settings,
    )
    # save completion_status for SCORM_2004
    lesson_status = String(
        scope=Scope.user_state,
        default='not attempted'
    )
    success_status = String(
        scope=Scope.user_state,
        default='unknown'
    )
    lesson_location = String(
        scope=Scope.user_state,
        default=''
    )
    suspend_data = String(
        scope=Scope.user_state,
        default=''
    )
    data_scorm = Dict(
        scope=Scope.user_state,
        default={}
    )
    lesson_score = Float(
        scope=Scope.user_state,
        default=0
    )
    weight = Float(
        default=1,
        scope=Scope.settings
    )
    has_score = Boolean(
        display_name=_("Scored"),
        help=_("Select True if this component will receive a numerical score from the Scorm"),
        default=False,
        scope=Scope.settings
    )
    icon_class = String(
        default="video",
        scope=Scope.settings,
    )

    has_author_view = True

    def resource_string(self, path):
        """Handy helper for getting resources from our kit."""
        data = pkg_resources.resource_string(__name__, path)
        return data.decode("utf8")

    @XBlock.supports('multi_device') # Mark as mobile-friendly
    def student_view(self, context=None):
        context_html = self.get_context_student()
        template = self.render_template('static/html/scormxblock.html', context_html)
        frag = Fragment(template)
        frag.add_css(self.resource_string("static/css/scormxblock.css"))
        frag.add_javascript(self.resource_string("static/js/src/scormxblock.js"))
        settings = {
            'version_scorm': "SCORM_12",
            'valid_write_data': WRITE_MODEL_DATA,
            'valid_read_data': READ_MODEL_DATA,
        }
        frag.initialize_js('ScormXBlock', json_args=settings)
        return frag

    def studio_view(self, context=None):
        context_html = self.get_context_studio()
        template = self.render_template('static/html/studio.html', context_html)
        frag = Fragment(template)
        frag.add_css(self.resource_string("static/css/scormxblock.css"))
        frag.add_javascript(self.resource_string("static/js/src/studio.js"))
        frag.initialize_js('ScormStudioXBlock')
        return frag

    def author_view(self, context):
        context_html = self.get_context_student()
        html = self.render_template('static/html/author_view.html', context_html)
        frag = Fragment(html)
        return frag

    @XBlock.handler
    def studio_submit(self, request, suffix=''):

        self.display_name = request.params['display_name']
        self.has_score = request.params['has_score']
        self.icon_class = 'problem' if self.has_score == 'True' else 'video'
        if hasattr(request.params['file'], 'file'):
            file = request.params['file'].file
            zip_file = zipfile.ZipFile(file, 'r')

            # Create a temporaray directory where the zip will extract.
            temp_directory = TempFS()

            # Extract the files in the temp directory just created.
            zip_file.extractall(temp_directory.root_path)

            manifest_path = '{}/imsmanifest.xml'.format(temp_directory.root_path)
            with open(manifest_path, 'r') as manifest_file:
                manifest = manifest_file.read()
            manifest_file.closed
            self.set_fields_xblock(manifest)

            # Now the part where we copy the data fast fast fast
            fs = djpyfs.get_filesystem(self.location.block_id)
            updoad_all_content(temp_directory, fs)

            # Destroy temp directory after all files are copied.
            temp_directory.close()

        return Response({'result': 'success'}, content_type='application/json')

    @XBlock.json_handler
    def scorm_get_values(self, request=None, suffix=None):
        """
        This method allows a SCO to retrieve data from the LMS.
        """

        if not self.data_scorm.get('cmi.core.entry'):
            self.initialize_user_data()
        else:
            self.data_scorm['cmi.core.entry'] = 'resume'

        values = {
            'cmi.core.lesson_status': self.lesson_status,
            'cmi.completion_status': self.lesson_status,
            'cmi.success_status': self.success_status,
            'cmi.core.lesson_location': self.lesson_location,
            'cmi.suspend_data': self.suspend_data,
        }

        data = {}

        for key, value in self.data_scorm.items():
            if key in READ_MODEL_DATA:
                data[key] = value
            elif key.startswith('cmi.interactions.') and validate_property(key, READ_MODEL_DATA):
                data[key] = value
            elif key.startswith('cmi.objectives.') and validate_property(key, READ_MODEL_DATA):
                data[key] = value

        values.update(data)
        return values

    @XBlock.json_handler
    def scorm_set_values(self, data, suffix=''):
        """
        This method allows the SCO to persist data to the LMS
        """
        context = {'result': 'success'}

        for name, value in data.items():
            if name in ['cmi.core.lesson_status', 'cmi.completion_status']:
                self.lesson_status = value
                if self.has_score and value in ['completed', 'failed', 'passed']:
                    self.publish_grade()
                    context.update({"lesson_score": self.lesson_score})
            elif name == 'cmi.success_status':
                self.success_status = value
                if self.has_score:
                    if self.success_status == 'unknown':
                        self.lesson_score = 0
                    self.publish_grade()
                    context.update({"lesson_score": self.lesson_score})

            elif name in ['cmi.core.score.raw', 'cmi.score.raw'] and self.has_score:
                self.lesson_score = int(value) / 100.0
                context.update({"lesson_score": self.lesson_score})

            elif name == 'cmi.core.lesson_location':
                self.lesson_location = str(value)

            elif name == 'cmi.suspend_data':
                self.suspend_data = value

            elif name.startswith('cmi.interactions.') and validate_property(name, WRITE_MODEL_DATA):
                self.data_scorm['cmi.interactions._count'] = self.data_scorm.get('cmi.interactions._count', 0) + 1
                self.data_scorm[name] = value

            elif name.startswith('cmi.objectives.') and validate_property(name, WRITE_MODEL_DATA):
                self.data_scorm['cmi.objetives._count'] = self.data_scorm.get('cmi.objetives._count', 0) + 1
                self.data_scorm[name] = value

            elif name in WRITE_MODEL_DATA:
                self.data_scorm[name] = value

        context.update({"completion_status": self.get_completion_status()})
        return context

    def publish_grade(self):
        if self.lesson_status == 'failed' or (self.version_scorm == 'SCORM_2004' and self.success_status in ['failed', 'unknown']):
            self.runtime.publish(
                self,
                'grade',
                {
                    'value': 0,
                    'max_value': self.weight,
                })
        else:
            self.runtime.publish(
                self,
                'grade',
                {
                    'value': self.lesson_score,
                    'max_value': self.weight,
                })

    def max_score(self):
        """
        Return the maximum score possible.
        """
        return self.weight if self.has_score else None

    def get_context_studio(self):
        return {
            'field_display_name': self.fields['display_name'],
            'display_name_value': self.display_name,
            'field_scorm_file': self.fields['scorm_file'],
            'field_has_score': self.fields['has_score'],
            'has_score_value': self.has_score
        }

    def get_context_student(self):
        """
        Returns the necessary context to display the units when in the LMS
        """
        fs = djpyfs.get_filesystem(self.location.block_id)

        scorm_file_path = ''
        if self.scorm_file:
            scorm_file_path = fs.get_url(self.scorm_file)

        # Required when working with a S3 djfs confifuguration and a proxy for the files
        # so that the Same-origin security policy does not block the files
        if settings.DJFS.get('use_proxy', False):
            proxy_file = scorm_file_path.split(settings.DJFS.get('prefix'))[-1]
            scorm_file_path = "/{}{}".format(settings.DJFS.get('proxy_root'), proxy_file)

        if settings.DJFS.get('remove_signature', False):
            scorm_file_path = urljoin(scorm_file_path, urlparse(scorm_file_path).path)

        scorm_file_path = unquote(scorm_file_path)

        return {
            'scorm_file_path': scorm_file_path,
            'lesson_score': self.lesson_score,
            'weight': self.weight,
            'has_score': self.has_score,
            'completion_status': self.get_completion_status()
        }

    def render_template(self, template_path, context):
        template_str = self.resource_string(template_path)
        template = Template(template_str)
        return template.render(Context(context))

    def set_fields_xblock(self, manifest):
        path_index_page = 'index.html'
        try:
            tree = ET.fromstring(manifest)

            # Getting the namespace from the tree does not have a clean API.
            # We use the simplest method outlined here: https://stackoverflow.com/a/28283119/2072496
            namespace = tree.tag.split('}')[0].strip('{')

            # By standard a namesapace it's a URI
            # we ensure the namespace we got in the tree object it's in fact a URL
            # if not we return an empty namespace and procced to look for resource tag
            if not namespace.startswith("http"):
                namespace = None

            if namespace:
                resource = tree.find('{{{0}}}resources/{{{0}}}resource'.format(namespace))
                schemaversion = tree.find('{{{0}}}metadata/{{{0}}}schemaversion'.format(namespace))
            else:
                resource = tree.find('resources/resource')
                schemaversion = tree.find('metadata/schemaversion')

            if (not schemaversion is None) and (re.match('^1.2$', schemaversion.text) is None):
                self.version_scorm = 'SCORM_2004'

            path_index_page = resource.get("href")

        except IOError:
            pass

        self.scorm_file = path_index_page

    def get_completion_status(self):
        completion_status = self.lesson_status
        if self.version_scorm == 'SCORM_2004' and self.success_status != 'unknown':
            completion_status = self.success_status
        return completion_status

    def initialize_user_data(self):
        """
        This method initializes the scorm user variables using the student data.
        """
        try:
            user = self.runtime.get_real_user(self.runtime.anonymous_student_id)
            username = user.username
            student_id = self.runtime.anonymous_student_id
        except TypeError:
            username = ''
            student_id = ''

        student_data = {
            'cmi.core.student_id': student_id,
            'cmi.core.entry': 'ab-initio',
            'cmi.core.student_name': username,
            'cmi.core.credit': 'credit' if self.has_score else 'no-credit',
            'cmi.core.lesson_mode': 'normal',
        }

        self.data_scorm.update(student_data)

    @staticmethod
    def workbench_scenarios():
        """A canned scenario for display in the workbench."""
        return [
            ("ScormXBlock",
             """<vertical_demo>
                <scormxblock/>
                </vertical_demo>
             """),
        ]
