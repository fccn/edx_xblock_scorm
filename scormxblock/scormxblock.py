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

from celery.task import task
from fs.copy import copy_fs
from fs.tempfs import TempFS
from djpyfs import djpyfs

from xblock.core import XBlock
from xblock.fields import Scope, String, Float, Boolean, Dict
from xblock.fragment import Fragment


# Make '_' a no-op so we can scrape strings
_ = lambda text: text
FILES_THRESHOLD_FOR_ASYNC = getattr(settings, 'SCORMXBLOCK_ASYNC_THRESHOLD', 150)
DEFAULT_CONTENT_TYPE = 'application/octet-stream'


@task(name='scormxblock.scormxblock.s3_upload', routing_key=settings.HIGH_PRIORITY_QUEUE)
def s3_upload(all_content, temp_directory, dest_dir):
    """
    Actual handling of the s3 uploads.
    """
    s3 = boto3.resource('s3',
                        aws_access_key_id=settings.DJFS.get('aws_access_key_id'),
                        aws_secret_access_key=settings.DJFS.get('aws_secret_access_key'),
                        endpoint_url=settings.DJFS.get('endpoint_url'),
                        region_name=settings.DJFS.get('region_name'),
    )

    bucket = s3.Bucket(settings.DJFS.get('bucket'))

    for filepath in all_content:
        sourcepath = path.normpath(path.join(temp_directory.root_path, filepath))
        destpath = path.normpath(path.join(dest_dir, filepath))

        # It's possible that the type is not in the mimetypes list.
        content_type = mimetypes.guess_type(sourcepath)[0]  or DEFAULT_CONTENT_TYPE

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
        s3_upload.apply_async((all_content, temp_directory, dest_dir), serializer='pickle')


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
        default="other",
        display_name=_("Icon"),
        help=_("Change icon"),
        values=[
            { "value": "video",   "display_name": "Video"   },
            { "value": "other",   "display_name": "Other"   },
            { "value": "problem", "display_name": "Problem" }
        ],
        scope=Scope.settings,
    )

    has_author_view = True

    def resource_string(self, path):
        """Handy helper for getting resources from our kit."""
        data = pkg_resources.resource_string(__name__, path)
        return data.decode("utf8")

    def student_view(self, context=None):
        context_html = self.get_context_student()
        template = self.render_template('static/html/scormxblock.html', context_html)
        frag = Fragment(template)
        frag.add_css(self.resource_string("static/css/scormxblock.css"))
        frag.add_javascript(self.resource_string("static/js/src/scormxblock.js"))
        settings = {
            'version_scorm': self.version_scorm
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
        self.icon_class = request.params['icon_class']
        if not self.icon_class:
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

            self.set_fields_xblock(manifest)

            # Now the part where we copy the data fast fast fast
            fs = djpyfs.get_filesystem(self.location.block_id)
            updoad_all_content(temp_directory, fs)

            # Destroy temp directory after all files are copied.
            temp_directory.close()

        return Response({'result': 'success'}, content_type='application/json')

    @XBlock.json_handler
    def scorm_get_value(self, data, suffix=''):
        name = data.get('name')
        if name in ['cmi.core.lesson_status', 'cmi.completion_status']:
            return {'value': self.lesson_status}
        elif name == 'cmi.success_status':
            return {'value': self.success_status}
        elif name == 'cmi.core.lesson_location':
            return {'value': self.lesson_location}
        elif name == 'cmi.suspend_data':
            return {'value': self.suspend_data}
        else:
            return {'value': self.data_scorm.get(name, '')}

    @XBlock.json_handler
    def scorm_set_value(self, data, suffix=''):
        context = {'result': 'success'}
        name = data.get('name')

        if name in ['cmi.core.lesson_status', 'cmi.completion_status']:
            self.lesson_status = data.get('value')
            if self.has_score and data.get('value') in ['completed', 'failed', 'passed']:
                self.publish_grade()
                context.update({"lesson_score": self.format_lesson_score})

        elif name == 'cmi.success_status':
            self.success_status = data.get('value')
            if self.has_score:
                if self.success_status == 'unknown':
                    self.lesson_score = 0
                self.publish_grade()
                context.update({"lesson_score": self.format_lesson_score})

        elif name in ['cmi.core.score.raw', 'cmi.score.raw'] and self.has_score:
            self.lesson_score = float(data.get('value', 0))/100.0
            context.update({"lesson_score": self.format_lesson_score})

        elif name == 'cmi.core.lesson_location':
            self.lesson_location = data.get('value', '')

        elif name == 'cmi.suspend_data':
            self.suspend_data = data.get('value', '')
        else:
            self.data_scorm[name] = data.get('value', '')

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
            'has_score_value': self.has_score,
            'field_icon_class': self.fields['icon_class'],
            'icon_class_value': self.icon_class
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
            'lesson_score': self.format_lesson_score,
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
            namespace = namespace if namespace.startswith("http") else None

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

    @property
    def format_lesson_score(self):
        return '{:.2f}'.format(self.lesson_score)

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
