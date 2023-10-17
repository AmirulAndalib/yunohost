# -*- coding: utf-8 -*-

""" License

    Copyright (C) 2021 YUNOHOST.ORG

    This program is free software; you can redistribute it and/or modify
    it under the terms of the GNU Affero General Public License as published
    by the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU Affero General Public License for more details.

    You should have received a copy of the GNU Affero General Public License
    along with this program; if not, see http://www.gnu.org/licenses

"""
import logging
from pathlib import Path
from typing import Any, Union

import ldap
from moulinette.utils.filesystem import read_yaml
from yunohost.authenticators.ldap_ynhuser import URI, USERDN, Authenticator as Auth
from yunohost.user import _hash_user_password
from yunohost.utils.error import YunohostError, YunohostValidationError
from yunohost.utils.ldap import LDAPInterface, _ldap_path_extract
from yunohost.utils.password import (
    assert_password_is_compatible,
    assert_password_is_strong_enough,
)

logger = logging.getLogger("portal")

ADMIN_ALIASES = ["root", "admin", "admins", "webmaster", "postmaster", "abuse"]


def _get_user_infos(
    user_attrs: list[str],
) -> tuple[str, str, dict[str, Any], LDAPInterface]:
    auth = Auth().get_session_cookie(decrypt_pwd=True)
    username = auth["user"]
    ldap_interface = LDAPInterface(username, auth["pwd"])
    result = ldap_interface.search("ou=users", f"uid={username}", user_attrs)
    if not result:
        raise YunohostValidationError("user_unknown", user=username)

    return username, auth["host"], result[0], ldap_interface


def _get_apps(username: Union[str, None] = None):
    """Get public + user's authorized apps.
    If `username` is not given, returns only public apps
    (e.g. with `visitors` in group permissions)
    """
    SYSTEM_PERMS = ("mail", "xmpp", "sftp", "ssh")

    ldap_interface = LDAPInterface("root")
    permissions_infos = ldap_interface.search(
        "ou=permission",
        "(objectclass=permissionYnh)",
        [
            "cn",
            "groupPermission",
            "inheritPermission",
            "URL",
            "label",
            "showTile",
        ],
    )

    apps = {}

    for perm in permissions_infos:
        name = perm["cn"][0].replace(".main", "")

        if name in SYSTEM_PERMS or not perm.get("showTile", [False])[0]:
            continue

        groups = [_ldap_path_extract(g, "cn") for g in perm["groupPermission"]]
        users = [
            _ldap_path_extract(u, "uid") for u in perm.get("inheritPermission", [])
        ]

        if username in users or "visitors" in groups:
            apps[name] = {
                "label": perm["label"][0],
                "url": perm["URL"][0],
            }

    return apps


def _get_portal_settings(domain: Union[str, None] = None):
    from yunohost.domain import DOMAIN_SETTINGS_DIR

    if not domain:
        from bottle import request

        domain = request.get_header("host")

    assert domain and "/" not in domain

    settings = {
        "public": False,
        "portal_logo": "",
        "portal_theme": "system",
        "portal_title": "YunoHost",
        # "show_other_domains_apps": False,
        "domain": domain,
    }

    if Path(f"{DOMAIN_SETTINGS_DIR}/{domain}.portal.yml").exists():
        settings.update(read_yaml(f"{DOMAIN_SETTINGS_DIR}/{domain}.portal.yml"))

    return settings


def portal_public():

    portal_settings = _get_portal_settings()
    portal_settings["apps"] = {}
    portal_settings["public"] = (
        portal_settings.pop("default_app", None) == "_yunohost_portal_with_public_apps"
    )

    if portal_settings["public"]:
        portal_settings["apps"] = _get_apps()

        # FIXME/TODO; See: filter apps that are available on specified domain
        # if not portal_settings["show_other_domains_apps"]:
        #     portal_settings["apps"] = {
        #         name: data
        #         for name, data in portal_settings["apps"].items()
        #         if portal_settings["domain"] in data["url"]
        #     }

    return portal_settings


def portal_me():
    """
    Get user informations
    """
    username, domain, user, _ = _get_user_infos(
        ["cn", "mail", "maildrop", "mailuserquota", "memberOf", "permission"]
    )

    groups = [_ldap_path_extract(g, "cn") for g in user["memberOf"]]
    groups = [g for g in groups if g not in [username, "all_users"]]
    apps = _get_apps(username)

    # FIXME / TODO: filter apps that are available on specified domain
    #   settings = _get_portal_settings(domain=domain)
    #   if not settings["show_other_domains_apps"]:
    #       apps = {name: data for name, data in apps.items() if domain in data["url"]}
    # App's `domain` info is not available in LDAP data, we need another config file
    # that would be readable by the `ynh-portal` user. This conf file could be generated
    # in `app_ssowatconf()`

    result_dict = {
        "username": username,
        "fullname": user["cn"][0],
        "mail": user["mail"][0],
        "mailalias": user["mail"][1:],
        "mailforward": user["maildrop"][1:],
        "groups": groups,
        "apps": apps,
    }

    # FIXME / TODO : add mail quota status ?
    #  result_dict["mailbox-quota"] = {
    #      "limit": userquota if is_limited else m18n.n("unlimit"),
    #      "use": storage_use,
    #  }
    # Could use : doveadm -c /dev/null -f flow quota recalc -u johndoe
    # But this requires to be in the mail group ...

    return result_dict


def portal_update(
    fullname: Union[str, None] = None,
    mailforward: Union[list[str], None] = None,
    mailalias: Union[list[str], None] = None,
    currentpassword: Union[str, None] = None,
    newpassword: Union[str, None] = None,
):
    from yunohost.domain import domain_list

    domains = domain_list()["domains"]
    username, domain, current_user, ldap_interface = _get_user_infos(
        ["givenName", "sn", "cn", "mail", "maildrop", "memberOf"]
    )
    new_attr_dict = {}

    if fullname is not None and fullname != current_user["cn"]:
        fullname = fullname.strip()
        firstname = fullname.split()[0]
        lastname = (
            " ".join(fullname.split()[1:]) or " "
        )  # Stupid hack because LDAP requires the sn/lastname attr, but it accepts a single whitespace...
        new_attr_dict["givenName"] = [firstname]  # TODO: Validate
        new_attr_dict["sn"] = [lastname]  # TODO: Validate
        new_attr_dict["cn"] = new_attr_dict["displayName"] = [
            (firstname + " " + lastname).strip()
        ]

    if mailalias is not None:
        mailalias = [mail.strip() for mail in mailalias if mail and mail.strip()]
        # keep first current mail unaltered
        mails = [current_user["mail"][0]]

        for index, mail in enumerate(mailalias):
            if mail in current_user["mail"]:
                if mail != current_user["mail"][0] and mail not in mails:
                    mails.append(mail)
                continue  # already in mails, skip validation

            local_part, domain = mail.split("@")
            if local_part in ADMIN_ALIASES:
                raise YunohostValidationError(
                    "mail_unavailable", path=f"mailalias[{index}]"
                )

            try:
                ldap_interface.validate_uniqueness({"mail": mail})
            except Exception as e:
                raise YunohostError("user_update_failed", user=username, error=e)

            if domain not in domains:
                raise YunohostValidationError(
                    "mail_domain_unknown", domain=domain, path=f"mailalias[{index}]"
                )

            mails.append(mail)

        new_attr_dict["mail"] = mails

    if mailforward is not None:
        new_attr_dict["maildrop"] = [current_user["maildrop"][0]] + [
            mail.strip()
            for mail in mailforward
            if mail and mail.strip() and mail != current_user["maildrop"][0]
        ]

    if newpassword:

        # FIXME: this ldap stuff should be handled in utils/ldap.py imho ?

        # Check that current password is valid
        try:
            con = ldap.ldapobject.ReconnectLDAPObject(URI, retry_max=0)
            con.simple_bind_s(USERDN.format(username=username), currentpassword)
        except ldap.INVALID_CREDENTIALS:
            raise YunohostValidationError("invalid_password", path="currentpassword")
        finally:
            # Free the connection, we don't really need it to keep it open as the point is only to check authentication...
            if con:
                con.unbind_s()

        # Ensure compatibility and sufficiently complex password
        try:
            assert_password_is_compatible(newpassword)
            is_admin = (
                "cn=admins,ou=groups,dc=yunohost,dc=org" in current_user["memberOf"]
            )
            assert_password_is_strong_enough(
                "admin" if is_admin else "user", newpassword
            )
        except YunohostValidationError as e:
            raise YunohostValidationError(e.key, path="newpassword")

        Auth().delete_session_cookie()
        new_attr_dict["userPassword"] = [_hash_user_password(newpassword)]

    try:
        ldap_interface.update(f"uid={username},ou=users", new_attr_dict)
    except Exception as e:
        raise YunohostError("user_update_failed", user=username, error=e)

    # FIXME: Here we could want to trigger "post_user_update" hook but hooks has to
    # be run as root

    return {
        "fullname": new_attr_dict["cn"][0],
        "mailalias": new_attr_dict["mail"][1:],
        "mailforward": new_attr_dict["maildrop"][1:],
    }
