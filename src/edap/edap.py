import hashlib
import os
import codecs
import argparse

import ldap
import ldap.modlist

from edap import constants as c


def transform_ldap_response(ldap_response):
    """
    Transform list of ldap tuples to list of dicts
    Args:
        ldap_response (list):

    Returns:
    """
    return [ldap_tuple_to_object(each) for each in ldap_response]


def ldap_tuple_to_object(ldap_tuple):
    """
    Transform tuple from ldap response (dn, attributes) to dict with all attributes and dn as fqdn

    Args:
        ldap_tuple (tuple): object from ldap response

    Returns:
    """
    return {
        'fqdn': ldap_tuple[0],
        **ldap_tuple[1]
    }


class ConstraintError(RuntimeError):
    pass


class ObjectDoesNotExist(Exception):
    """ Base exception if searched object cannot be found """
    pass


class MultipleObjectsFound(Exception):
    """ Base exception if found more than one object """
    pass


def get_single_object(data):
    """ Get first element of a list, or raise Exception if list length > 1 or equals 0 """
    if len(data) == 0:
        raise ObjectDoesNotExist('Object does not exist')
    elif len(data) > 1:
        raise MultipleObjectsFound('Multiple objects found')
    return data[0]


def _hashPassword(password):
    salt = os.urandom(4)
    h = hashlib.sha1(password.encode("ASCII"))
    h.update(salt)
    hashed = "{SSHA}".encode() + codecs.encode(h.digest() + salt, "base64").strip()
    return hashed


class LdapObjectsMixin(object):

    def object_exists(self, search, obj_class=None):
        if obj_class is not None:
            search = f"&({search})(objectClass={obj_class})"
        found = self.search_s(self.BASE_DN, ldap.SCOPE_SUBTREE, f"({search})")
        return len(found)

    def object_exists_at(self, root, obj_class, additional_search=None):
        search = f"objectClass={obj_class}"
        if additional_search is not None:
            search = f"&({search})({additional_search})"
        try:
            found = self.search_s(root, ldap.SCOPE_BASE, f"({search})")
        except Exception:
            return 0
        return len(found)

    def subobject_exists_at(self, relative_pos, obj_class, additional_search=None):
        root = f"{relative_pos},{self.BASE_DN}"
        return self.object_exists_at(root, obj_class, additional_search)

    def get_objects(self, search=None, relative_pos=None, obj_class=None):
        root = self.BASE_DN
        if obj_class is not None:
            if search:
                search = f"(&({search})(objectClass={obj_class}))"
            else:
                search = f"(objectClass={obj_class})"
        if relative_pos:
            root = f"{relative_pos},{root}"
        return transform_ldap_response(self.search_s(root, ldap.SCOPE_SUBTREE, search))

    def get_subobjects(self, relative_pos, search=None, obj_class=None):
        return self.get_objects(search=search, relative_pos=relative_pos, obj_class=obj_class)


class LdapUserMixin(object):

    def add_user(self, uid, name, surname, password):
        if self.subobject_exists_at("ou=people", "organizationalUnit") == 0:
            raise ConstraintError(f"The people group '{self.PEOPLE_GROUP}' doesn't exist.")
        if self.user_of_uid_exists(uid) > 0:
            raise ConstraintError(f"User of uid '{uid}' already exists.")
        modlist = self.mk_add_user_modlist(uid, name, surname, password)
        self.add_s(f"uid={uid},{self.PEOPLE_GROUP}", modlist)

    def get_users(self, search=None):
        """
        Get subobjects of organizational unit "people"

        Args:
            search (str): search filter

        Returns:
        """
        return self.get_subobjects('ou=people', search, obj_class='inetOrgPerson')

    def get_user(self, uid):
        """
        Search in subobjects of organizational unit "people" by uid
        Args:
            uid (str):

        Returns:
        """
        return get_single_object(self.get_users(search=f"uid={uid}"))

    def get_user_groups(self, uid):
        """
        Get groups where user is a member

        Args:
            uid (str): user id

        Returns (list):
        """
        search = f"(&(memberUid={uid})(objectClass=posixGroup))"
        return transform_ldap_response(self.search_s(self.BASE_DN, ldap.SCOPE_SUBTREE, search))

    def mk_add_user_modlist(self, uid, name, surname, password):
        mail = f"{uid}@example.com".encode("ASCII")
        dic = dict(
            uid=uid.encode("ASCII"), givenName=name.encode("UTF-8"),
            mail=mail, objectclass=(b"inetOrgPerson", b"top"),
            sn=surname.encode("UTF-8"), userPassword=_hashPassword(password),
            cn=f"{name} {surname}".encode("UTF-8"),
        )
        modlist = ldap.modlist.addModlist(dic)
        return modlist

    def user_of_uid_exists(self, uid):
        if self.subobject_exists_at("ou=people", "organizationalUnit") == 0:
            raise ConstraintError(f"The people group '{self.PEOPLE_GROUP}' doesn't exist.")
        found = self.search_s(f"{self.PEOPLE_GROUP}", ldap.SCOPE_ONELEVEL, f"(uid={uid})")
        return len(found)

    def uid_is_member_of_group(self, group_fqdn, uid):
        search = f"memberUid={uid}"
        found = self.search_s(group_fqdn, ldap.SCOPE_BASE, f"({search})")
        return len(found)

    def make_uid_member_of(self, uid, group_fqdn):
        if self.object_exists_at(group_fqdn, "posixGroup") == 0:
            raise ConstraintError(f"Group {group_fqdn} doesn't exist.")
        if self.user_of_uid_exists(uid) == 0:
            msg = f"User of uid '{uid}' doesn't exist, so we can't add it to any group."
            raise ConstraintError(msg)
        if self.uid_is_member_of_group(group_fqdn, uid):
            return
        modlist = [(ldap.MOD_ADD, "memberUid", [uid.encode("ASCII")])]
        self.modify_s(group_fqdn, modlist)

    def make_uid_member_of_division(self, uid, name):
        group_fqdn = f"cn={name},{self.DIVISIONS_GROUP}"
        return self.make_uid_member_of(uid, group_fqdn)

    def make_uid_member_of_service_group(self, uid, name):
        group_fqdn = f"cn={name},{self.SERVICES_GROUP}"
        return self.make_uid_member_of(uid, group_fqdn)

    def remove_uid_member_of(self, uid, group_fqdn):
        if self.object_exists_at(group_fqdn, "posixGroup") == 0:
            raise ConstraintError(f"Group {group_fqdn} doesn't exist.")
        if not self.uid_is_member_of_group(group_fqdn, uid):
            return
        if self.user_of_uid_exists(uid) == 0:
            msg = f"User of uid '{uid}' doesn't exist, so we can't add it to any group."
            raise ConstraintError(msg)
        modlist = [(ldap.MOD_DELETE, "memberUid", [uid.encode("ASCII")])]
        self.modify_s(group_fqdn, modlist)

    def remove_uid_member_of_division(self, uid, name):
        group_fqdn = f"cn={name},{self.DIVISIONS_GROUP}"
        return self.remove_uid_member_of(uid, group_fqdn)

    def remove_uid_member_of_service_group(self, uid, name):
        group_fqdn = f"cn={name},{self.SERVICES_GROUP}"
        return self.remove_uid_member_of(uid, group_fqdn)


class OrganizationalUnitMixin(object):

    def create_org_unit(self, name, fqdn):
        dic = dict(
             ou=name.encode("ASCII"),
             objectclass=(b"organizationalUnit", b"top"),
        )
        modlist = ldap.modlist.addModlist(dic)
        self.add_s(fqdn, modlist)

    def get_org_unit(self, organizational_unit):
        return self.get_objects(search=f'ou={organizational_unit}')

    def org_unit_exists(self, organizational_unit):
        return self.subobject_exists_at(f"ou={organizational_unit}", "organizationalUnit")


class LdapGroupMixin(object):

    def create_group(self, name, organizational_unit, description=None):
        org_unit = f"ou={organizational_unit}"
        if not self.org_unit_exists(organizational_unit):
            self.create_org_unit(org_unit, f"{org_unit},{self.BASE_DN}")
        if self.group_exists(name, organizational_unit):
            raise ConstraintError("Group with such name under this organizational unit already exists")
        dic = self.create_group_dict(f"{name}")
        if description:
            dic['description'] = description
        return self.create_group_from_dict(f"cn={name},{org_unit},{self.BASE_DN}", dic)

    def get_groups(self, search=None, organizational_unit=None):
        """
        Get objects with object class "posixGroup"

        Args:
            search (str): search filter

        Returns (list):
        """
        relative_pos = f"ou={organizational_unit}" if organizational_unit else None
        return self.get_objects(search=search, relative_pos=relative_pos, obj_class='posixGroup')

    def get_group(self, cname, organizational_unit):
        """
        Get group by cname
        Args:
            cname (str): cname of a group

        Returns:
        """
        return get_single_object(self.get_groups(f"cn={cname}", organizational_unit=organizational_unit))

    def create_group_dict(self, name):
        dic = dict(
            cn=name.encode("ASCII"), objectclass=(b"posixGroup", b"top"), gidNumber=b"500",
        )
        return dic

    def create_group_from_dict(self, fqdn, dic):
        modlist = ldap.modlist.addModlist(dic)
        return self.add_s(fqdn, modlist)

    def group_exists(self, name, organizational_unit):
        return self.subobject_exists_at(f"cn={name},ou={organizational_unit}", "posixGroup")


class LdapServiceMixin(object):

    def create_service(self, name):
        return self.create_group(name=name, organizational_unit="services")


class LdapDivisionMixin(object):
    """
    Division is a posixGroup, child of organizationalUnit ou=divisions that is just below the base DN.

    A division has machine and display names. A division's DN begins with cn=<machine name>,
    e.g. the full division DN of a publishing division is cn=PUB,ou=divisions,dc=entint,dc=org.
    The description attribute of a division stores it's display name, e.g. Publishing in this case.

    The group's gidNumber is not important.
    """

    def get_divisions(self, search=None):
        """ Get objects (of posixGroup class) with organizational unit 'divisions' by given search """
        return self.get_objects(search=search, relative_pos='ou=divisions', obj_class='posixGroup')

    def get_division(self, name):
        """
        Get division by cname

        Args:
            name (str): division name

        Returns:

        """
        return get_single_object(self.get_divisions(f'cn={name}'))

    def create_division(self, machine_name, display_name=None):
        """
        Create division

        Args:
            machine_name (str): division's cname
            display_name (str): division's display name, stored in description

        Returns:

        """
        display_name_bytes = display_name.encode('utf-8') if isinstance(display_name, str) else display_name
        return self.create_group(name=machine_name, organizational_unit="divisions", description=display_name_bytes)

    def create_all_divisions(self, source):
        for dname in source:
            self.create_division(dname)


class LdapFranchiseMixin(object):

    def create_franchise(self, name):
        description = self.label_franchise(name).encode("UTF-8")
        return self.create_group(name, "franchises", description=description)

    def label_franchise(self, name):
        for code, country_name in c.COUNTRIES_CODES.items():
            if name.startswith(code):
                return country_name
        raise KeyError(f"Invalid country code to match '{name}'")

    def create_all_franchises(self, source):
        for frname in source:
            self.create_franchise(frname)


def ensure_org_sanity(edap, source):
    edap.create_all_divisions(source["divisions"])
    edap.create_all_franchises(source["countries"])
    edap.create_org_unit("people", edap.ldap.PEOPLE_GROUP)
    edap.create_org_unit("people", edap.ldap.PEOPLE_GROUP)


def update_parser(parser=None):
    if parser is None:
        parser = argparse.ArgumentParser()
    parser.add_argument("hostname")
    parser.add_argument("--password", "-p")
    parser.add_argument("--admin-dn", "-u")
    return parser


class Edap(LdapObjectsMixin, LdapGroupMixin, OrganizationalUnitMixin, LdapUserMixin,
           LdapFranchiseMixin, LdapDivisionMixin, LdapServiceMixin):

    def __init__(self, hostname, admin_cn, password, domain=None):
        if domain is None:
            domain = "example.com"
        domain_components = domain.split(".")
        basedn_components = [f"dc={c}" for c in domain_components]
        self.BASE_DN = ",".join(basedn_components)

        admin_dn = f"{admin_cn},{self.BASE_DN}"
        self.ldap = ldap.initialize(f"ldap://{hostname}")
        self.ldap.bind_s(admin_dn, password)

        self.PEOPLE_GROUP = f"ou=people,{self.BASE_DN}"
        self.DIVISIONS = "ou=divisions"
        self.DIVISIONS_GROUP = f"{self.DIVISIONS},{self.BASE_DN}"
        self.FRANCHISES = "ou=franchises"
        self.FRANCHISES_GROUP = f"{self.FRANCHISES},{self.BASE_DN}"
        self.SERVICES = "ou=services"
        self.SERVICES_GROUP = f"{self.SERVICES},{self.BASE_DN}"

    def add_s(self, *args, **kwargs):
        return self.ldap.add_s(*args, **kwargs)

    def modify_s(self, *args, **kwargs):
        return self.ldap.modify_s(*args, **kwargs)

    def search_s(self, *args, **kwargs):
        return self.ldap.search_s(*args, **kwargs)

    def unbind_s(self):
        return self.ldap.unbind_s()


if __name__ == "__main__":
    parser = update_parser()
    args = parser.parse_args()

    edap = Edap(args.hostname, args.admin_dn, args.password)