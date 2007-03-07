#!/usr/bin/python -tt
# -*- coding: UTF-8 -*-
# vim: sw=4 ts=4 expandtab ai
#
# DpkgChangelog.py
#
# This module implements parser for Debian changelog files
#
# Copyright (C) 2005,2006 Alexandr Kanevskiy
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

import re
from minideblib import DpkgVersion
import types
import rfc822
import cStringIO as StringIO

__revision__ = "r"+"$Revision$"[11:-2]
__all__ = ['DpkgChangelog', 'DpkgChangelogEntry', 'DpkgChangelogException']


class DpkgChangelogException(Exception):
    def __init__(self, msg):
        Exception.__init__(self)
        self.msg = msg
    def __repr__(self):
        return self.msg

# Fixed settings, do not change these unless you really know what you are doing
PackageRegex    = "[a-z0-9][a-z0-9.+-]+"        # Regular expression package names must comply with
VersionRegex    = "(?:[0-9]+:)?[a-zA-Z0-9~.+-]+" # Regular expression package versions must comply with

# Regular expressions for various bits of the syntax used
ClosesRegex     = "closes:\s*(?:bug)?#?\s?\d+(?:,\s?(?:bug)?#?\s?\d+)*"
NBugRegex       = "Fixe[sd]:\s*NB#\d+(?:\s*,\s*NB#\d+)*"
MBugRegex       = "Fixe[sd]:\s*MB#\d+(?:\s*,\s*MB#\d+)*"
BugRegex        = "(\d+)"
NMBugRegex      = "B#(\d+)"
NReqImplRegex   = "Implemented:\s*NR#\d{1,6}(?:,\s*NR#\d{1,6})*"
NReqUpdRegex    = "Updated:\s*NR#\d{1,6}(?:,\s*NR#\d{1,6})*"
NReqPartRegex   = "Partial:\s*NR#\d{1,6}(?:,\s*NR#\d{1,6})*"
NReqDropRegex   = "Dropped:\s*NR#\d{1,6}(?:,\s*NR#\d{1,6})*"
NReqRegex       = "R#(\d{1,6})"

# Precompile the regular expressions
ClosesMatcher    = re.compile(ClosesRegex, re.IGNORECASE)
BugMatcher       = re.compile(BugRegex)
NMBugMatcher     = re.compile(NMBugRegex)
NBugMatcher      = re.compile(NBugRegex, re.IGNORECASE)
MBugMatcher      = re.compile(MBugRegex, re.IGNORECASE)
NReqImplMatcher  = re.compile(NReqImplRegex, re.IGNORECASE)
NReqUpdMatcher   = re.compile(NReqUpdRegex, re.IGNORECASE)
NReqPartMatcher  = re.compile(NReqPartRegex, re.IGNORECASE)
NReqDropMatcher  = re.compile(NReqDropRegex, re.IGNORECASE)
NReqMatcher      = re.compile(NReqRegex)


# Changelog regexps
StartRegex      = "(?P<package>%s) \((?P<version>%s)\) (?P<distribution>\w+(?:\s+\w+)*); (?P<attrs>.*)" % (PackageRegex, VersionRegex)
EndRegex        = " -- (?P<changedby>.+? <.+?>)  (?P<date>.*)"
AttrRegex       = "(?P<key>.+?)=(?P<value>.*)"

# Precompile the regular expressions
StartMatcher    = re.compile(StartRegex)
EndMatcher      = re.compile(EndRegex)
AttrMatcher     = re.compile(AttrRegex)


class DpkgChangelogEntry:
    '''Simple class to represent a single changelog entry. The list of
    attributes in the entry header is stored in the attributes map. The
    timestamp associated with the changes are stored in time.mktime()
    comptabile tuple format in the
    date member.'''

    def __init__(self):
        self.package = ""
        self.version = ""
        self.distribution = None
        self.date = None
        self.strdate = ""
        self.changedby = ""
        self.bugsfixed = []
        self.nbugsfixed = []
        self.mbugsfixed = []
        self.nreqsimplemented = []
        self.nreqsupdated = []
        self.nreqspartial = []
        self.nreqsdropped = []
        self.attributes = {}
        self.entries = []

    def add_entry(self, entry):
        '''Utility function to add a changelog entry. Also takes care
        of extracting the bugs closed by this change and adding them to
        the self.bugsfixed array.'''

        # Check if we have a proper Closes command
        match = ClosesMatcher.search(entry)
        if match:
            self.bugsfixed.extend(BugMatcher.findall(match.group(0)))
        # Check if we have a proper NBugs
        match = NBugMatcher.search(entry)
        if match:
            self.nbugsfixed.extend(NMBugMatcher.findall(match.group(0)))
        # Check if we have a proper MBugs
        match = MBugMatcher.search(entry)
        if match:
            self.mbugsfixed.extend(NMBugMatcher.findall(match.group(0)))
        # Check if we have implemented requirements
        match = NReqImplMatcher.search(entry)
        if match:
            self.nreqsimplemented.extend(NReqMatcher.findall(match.group(0)))
        # Check if we have updated requirements
        match = NReqUpdMatcher.search(entry)
        if match:
            self.nreqsupdated.extend(NReqMatcher.findall(match.group(0)))
        # Check if we have partially implemented requirements
        match = NReqPartMatcher.search(entry)
        if match:
            self.nreqspartial.extend(NReqMatcher.findall(match.group(0)))
        # Check if we have dropped requirements
        match = NReqDropMatcher.search(entry)
        if match:
            self.nreqsdropped.extend(NReqMatcher.findall(match.group(0)))
        self.entries.append(entry)


class DpkgChangelog:
    '''Simple class to repsresent Debian changlog'''
    def __init__(self):
        self.entries = []
        self.lineno = 0
        self.package = None
        self.version = None
        self.distribution = None
        self.changedby = None
 
    def __get_next_nonempty_line(self, infile):
        "Return the next line that is not empty"
        self.lineno += 1
        line = infile.readline()
        while not line.strip():
            self.lineno += 1
            line = infile.readline()
            if line == '':
                return ''
        if line[-1] == "\n":
            return line[:-1]
        else:
            return line

    def _parse_one_entry(self, infile):

        line = self.__get_next_nonempty_line(infile)
        match = StartMatcher.match(line)
        if not match:
            raise DpkgChangelogException, "Invalid first line"

        entry = DpkgChangelogEntry()
        entry.package = match.group("package")
        try:
            entry.version = DpkgVersion.DpkgVersion(match.group("version"))
        except Exception, e:
            raise DpkgChangelogException, "Invalid version: %s" % e

        entry.distribution = match.group("distribution").split()

        # Extract the attributes from the line
        for attr in match.group("attrs").split():
            am = AttrMatcher.match(attr)
            if not am:
                raise DpkgChangelogException, "Invalid syntax for attribute"
            entry.attributes[am.group("key")] = am.group("value")

        # Check for essential urgency attribute
        if not entry.attributes.has_key("urgency"):
            raise DpkgChangelogException, "Missing urgency attribute"

        # Read the changelog entries themselves
        line = self.__get_next_nonempty_line(infile)
        buf = ""
        while line.startswith("  "):
            if line.startswith("  *"):
                if buf:
                    entry.add_entry(buf.strip())
                buf = line[2:]
            else:
                buf += "\n" + line[2:]
            line = self.__get_next_nonempty_line(infile)

        # Commit last seen line
        if buf:
            entry.add_entry(buf.strip())
            

        # Try and parse the last line
        em = EndMatcher.match(line)
        if not em:
            raise DpkgChangelogException, "Invalid line in changelog entry"

        entry.changedby = em.group("changedby")
        try:
            entry.strdate = em.group("date")
            entry.date = rfc822.parsedate(entry.strdate)
            if not entry.date:
                raise DpkgChangelogException, "Invalid date in changelog entry: %s" % entry.strdate
        except:
            raise DpkgChangelogException, "Invalid date in changelog entry: %s" % entry.strdate

        # Return the parsed changelog entry
        return entry


    def parse_changelog(self, changelog):
        '''Parses changelog argument (could be file or string)
        and represents it's content as array of DpkgChangelogEntry'''
        if type(changelog) is types.StringType:
            fh = StringIO.StringIO(changelog)
        elif type(changelog) is types.FileType:
            fh = changelog
        else: 
            raise DpkgChangelogException, "Invalid argument type"
        
        while True:
            try:
                entry = self._parse_one_entry(fh)
                self.entries.append(entry)
            except DpkgChangelogException, e:
                last_err = e.msg
                break
        
        if len(self.entries) > 0:
            self.package = self.entries[0].package
            self.version = self.entries[0].version
            self.distribution = self.entries[0].distribution
            self.changedby = self.entries[0].changedby
        else:
            raise DpkgChangelogException, "Unable to get entries from changelog: %s" % last_err


