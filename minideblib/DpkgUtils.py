# DpkgUtils.py
#
# This module contains a set of utility functions that are used
# throughout the dpkg suite.
#
# Copyright 2002 Wichert Akkerman <wichert@deephackmode.org>
#
# This file is free software; you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA 02111-1307, USA.

import os, re, string, sys
import DpkgOptions

# Message levels as used by inform()
VERB_QUIET		= 0	# Quiet operation (default)
VERB_INFORMATIVE	= 1	# Informative messages
VERB_DETAIL		= 2	# Detailed infomration on what we're doing
VERB_DEBUG		= 3	# Debug information


def inform(msg, level=VERB_INFORMATIVE):
	"Print an informative message if the verbose-level is high enough."

	if DpkgOptions.Options["verbose"]>=level:
		print msg

def abort(str):
	"Print a message and exit with an error."
	sys.stderr.write(str + "\n")
	sys.exit(1)


def SlurpFile(file, sep='\n'):
	"Read the contents of a file."

	fd=open(file, 'r')
	return string.split(fd.read(), sep)


def SlurpCommand(command, sep='\n'):
	"Run a command and return its output."

	fd=os.popen(command)
	data=fd.read()
	if data=='':
		return ()
	else:
		return string.split(data, sep)


def __FilterData(data, regexp):
	"Filter the data through a regexp and return the matching groups."

	lst=[]
	matcher=re.compile(regexp)
	for d in data:
		mo=matcher.search(d)
		if mo:
			lst.append(mo.groups())
	
	return lst


def FilterFile(file, regexp, sep='\n'):
	"Read a file return the regexp matches."

	return __FilterData(SlurpFile(file, sep), regexp)


def FilterCommand(command, regexp, sep='\n'):
	"Run a command and return the regexp matches."

	return __FilterData(SlurpCommand(command, sep), regexp)


def ValidPackageName(name):
	"Check if a package name is valid"

	if re.match("^%s$" % DpkgOptions.PackageRegex, name):
		return 1
	return 0


def ValidPackagevVersion(version):
	"Check if a package version is valid"

	if re.match("^%s$" % DpkgOptions.VersionRegex, version):
		return 1
	return 0


def HandleArgOption(keyword, sopt, lopt, opt, args):
	'''Utility function for argument parsers. Check for a specific
	option-taking argument and processes it.'''
	if opt==sopt:
		DpkgOptions.Options[keyword]=args.pop(0)
		return 1
	elif opt[:2]==sopt:
		DpkgOptions.Options[keyword]=ol[2:]
		return 1
	elif opt==lopt:
		DpkgOptions.Options[keyword]=args.pop(0)
		return 1
	elif lopt and opt[:len(lopt)]==lopt and opt[len(lopt)]=='=':
		DpkgOptions.Options[keyword]=opt[len(lopt)+1:]
		return 1

	return 0


def HandleNoArgOption(keyword, sopt, lopt, opt):
	'''Utility function for argument parsers. Check for a specific
	no-option-taking argument and processes it.'''
	if opt==sopt or (lopt and opt==lopt):
		DpkgOptions.Options[keyword]=DpkgOptions.Options[keyword]+1
		return 1

	return 0


# Global initialization
if not DpkgOptions.Options.has_key("verbose"):
	DpkgOptions.Options["verbose"]=VERB_QUIET

