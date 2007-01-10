#!/usr/bin/python -tt
# -*- coding: UTF-8 -*-
# vim: sw=4 ts=4 expandtab ai
#
# LoggableObject.py
#
# This module implements class which has internal _logger object for easy logging.
#
# Copyright (C) 2007 Alexandr Kanevskiy
#
# Contact: Alexandr Kanevskiy <packages@bifh.org>
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# version 2 as published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA
# 02110-1301 USA
#
# $Id$

__revision__ = "r"+"$Revision$"[11:-2]
__all__ = [ 'LoggableObject' ]

import logging

class LazyInit(object):
    def __init__(self, calculate_function):
        self._calculate = calculate_function

    def __get__(self, obj, _=None):
        if obj is None:
            return self
        value = self._calculate(obj)
        setattr(obj, self._calculate.func_name, value)
        return value


class LoggableObject:
    def _logger(self):
        """ Returns logger and initializes default handlers if needed """
        logger = logging.getLogger(__name__)
        c = logger
        found = False
        while c:
            if c.handlers:
                found = True
                break
            c = c.parent
        if not found:
            logging.basicConfig()
        return logger
    _logger = LazyInit(_logger)

