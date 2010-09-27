# -*- coding: utf-8 -*-

# Copyright © 2010 Red Hat, Inc.
#
# This software is licensed to you under the GNU General Public License,
# version 2 (GPLv2). There is NO WARRANTY for this software, express or
# implied, including the implied warranties of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. You should have received a copy of GPLv2
# along with this software; if not, see
# http://www.gnu.org/licenses/old-licenses/gpl-2.0.txt.
#
# Red Hat trademarks are not licensed under GPLv2. No permission is
# granted to use or replicate Red Hat trademarks that are incorporated
# in this software or its documentation.

import os
import sys
from gettext import gettext as _
from optparse import OptionGroup, OptionParser, SUPPRESS_HELP

from pulp.client.core import load_core_commands


class PulpBase(object):
    """
    Base pulp command line tool class.
    @cvar _modules: list of command modules to load
    """

    _modules = None

    def __init__(self):
        self.commands = load_core_commands(self._modules)
        self.parser = OptionParser(usage=self.usage())
        self.parser.disable_interspersed_args()
        self.parser.add_option('--debug', dest='debug', action='store_true',
                               default=False, help=SUPPRESS_HELP)

    def usage(self):
        """
        Usage string.
        @rtype: str
        @return: command's usage string
        """
        lines = ['Usage: %s <options> <command>' % os.path.basename(sys.argv[0]),
                 'Supported Commands:']
        for name, command in self.commands.items():
            lines.append('\t%-14s %-25s' % (name, command.shortdesc))
        return '\n'.join(lines)

    def parse_args(self, args):
        """
        Parse the command line arguments.
        @note: this method also defines the command line options and may be
               overridden to define new options or to validate option values
        @type args: list of str's
        @param args: command line arguments
        @rtype: optparse options object and list of str's
        @return: options and list of any remaining (unparsed) arguments
        """
        credentials = OptionGroup(self.parser, _('pulp user account credentials'))
        credentials.add_option('-u', '--username', dest='username',
                               default=None, help=_('account username'))
        credentials.add_option('-p', '--password', dest='password',
                               default=None, help=_('account password'))
        self.parser.add_option_group(credentials)
        return self.parser.parse_args(args)

    def find_command(self, command):
        """
        Look up a command by name.
        @type command: str
        @rtype: pulp.client.core.base.BaseCore instance or None
        @return: object corresponding to command on success, None on failure
        """
        if command not in self.commands:
            return None
        return self.commands[command]

    def main(self, args=sys.argv[1:]):
        """
        Run this command.
        @type args: list of str's
        @param args: command line arguments
        """
        opts, args = self.parse_args(args)
        if not args:
            self.parser.error(_('no command given: please see --help'))
        command_module = self.find_command(args[0])
        if command_module is None:
            self.parser.error(_('invalid command: please see --help'))
        command_module.main(opts, args[1:])

