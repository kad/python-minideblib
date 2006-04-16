# DpkgVarlist.py
#
# This module implements DpkgVarlist, a class which contains
# variables that are using in dpkg source packages.
#
# Unlike the other dpkg files this file is not in RFC822 syntax. Instead
# each variable definition conists of a single line of the form "key=value".
# Comments are not allowed.
#
# Copyright 2001 Wichert Akkerman <wichert@linux.com>
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

import re, string
import DpkgDatalist

class DpkgVarlist(DpkgDatalist.DpkgDatalist):
	def load(self, fn):
		"Load variable data from a file."

		vf=open(fn, "r")
		matcher=re.compile("^([^=]+)=\s*(.*)\s*$")
		lineno=1
		for line in vf.readlines():
			mo=matcher.search(line)
			if not mo:
				raise DpkgDatalist.DpkgDatalistException("Syntax error in varlistfile", DpkgVarlistException.SYNTAXERROR, fn, lineno)

			self.data[mo.group(1)]=string.strip(mo.group(2))
			lineno=lineno+1

		vf.close()

	def _store(self, fo):
		"Write our variable data to a file object"

		for key in self.data.keys():
			fo.write("%s=%s\n" % (key, self.data[key]))

