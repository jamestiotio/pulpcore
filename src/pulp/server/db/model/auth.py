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

from pulp.server.db.model.base import Model


class Permission(Model):
    """
    Model representing the user permissions associated with a pulp resource.
    @ivar resource: uri path of resource
    @ivar users: dictionary of user id: tuple of allowed operations
    """

    collection_name = 'permissions'
    unique_indicies = ('resource',)

    def __init__(self, resource):
        super(Permission, self).__init__()
        self.resource = resource
        self.users = {}


class Role(Model):
    """
    Model representing a set of users and the permissions granted those users
    as a group.
    @ivar name: role's name
    @ivar permissions: dictionary of resource: tuple of allowed operations
    """

    collection_name = 'roles'
    unique_indicies = ('name',)

    def __init__(self, name):
        self._id = self.name = name
        self.permissions = {}


class User(Model):
    """
    Model representing a user of pulp.
    @ivar login: user's login name
    @ivar password: password for login credentials
    @ivar name: user's full name
    @ivar roles: list of roles user belongs to
    """

    collection_name = 'users'

    def __init__(self, login, id, password, name):
        self._id = id
        self.id = id
        self.login = login
        self.password = password
        self.name = name
        self.roles = []

    def __unicode__(self):
        return unicode(self.name)

    def __str__(self):
        return unicode(self).encode('utf-8')
