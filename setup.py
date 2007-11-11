#!/usr/bin/python -tt
# -*- coding: UTF-8 -*-
# vim: sw=4 ts=4 expandtab ai
#
# $Id$

try:
    from setuptools import setup
except ImportError:
    from distutils.core import setup


version = "0.6.21.27"

setup (name = "minideblib",
    description = "Access to deb files and repositories",
    version = version,
    author = "Alexandr D. Kanevskiy",
    author_email = "packages@bifh.org",
    url = "http://bifh.org/wiki/python-minideblib",
    license = "GPL",
    packages = ['minideblib'],
    long_description = "Python library for manipulating debian packages and repositories",
    keywords = "python debian apt dpkg",
    platforms="Python 2.3 and later.",
    classifiers=[
        "Development Status :: 4 - Beta",
        "Operating System :: Unix",
        "License :: OSI Approved :: GNU General Public License (GPL)",
        "Intended Audience :: Developers",
        "Programming Language :: Python",
        "Topic :: System :: Archiving :: Packaging",
        "Topic :: Software Development :: Libraries :: Python Modules"
        ]
    )

