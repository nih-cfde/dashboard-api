#
# Copyright 2018 University of Southern California
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

from setuptools import setup

setup(
    name='cfde-dashboard',
    description='CFDE Dashboard REST API',
    version='1.2',
    zip_safe=False,  # we need to unpack for mod_wsgi to find dashboard.wsgi
    packages=[
        'dashboard',
    ],
    package_data={
        'dashboard': ['dashboard.conf', 'dashboard_api.wsgi', 'wsgi_dashboard_api.conf'],
    },
    requires=['flask'],
    install_requires=['flask'],
    license='Apache License, Version 2.0',
    classifiers=[
        'Intended Audience :: Developers',
        'License :: OSI Approved :: Apache Software License',
        'Programming Language :: Python',
        'Topic :: Internet :: WWW/HTTP',
        'Topic :: Software Development :: Libraries :: Python Modules'
    ])
