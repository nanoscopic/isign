# encoding: utf-8
import json
import pkg_resources

stream = pkg_resources.resource_stream(__name__, "version.json")
version = json.load(stream)

__version__ = version['version']
__commit__ = version['commit']
__build__ = version['build']
