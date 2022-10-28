#
# Copyright (c) 2022 YunoHost Contributors
#
# This file is part of YunoHost (see https://yunohost.org)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.
#
import os
import json
import time
import shutil
import subprocess
import csv
import tempfile
import re
import urllib.parse
import datetime
from packaging import version

from moulinette import Moulinette, m18n
from moulinette.utils.log import getActionLogger
from moulinette.utils.filesystem import mkdir, write_to_yaml, read_yaml, rm, chown, chmod, write_to_file
from moulinette.utils.process import check_output

import yunohost.domain
from yunohost.app import (
    app_info,
    _is_installed,
    _make_environment_for_app_script,
    _make_tmp_workdir_for_app,
    _get_manifest_of_app,
)
from yunohost.hook import (
    hook_list,
    hook_info,
    hook_callback,
    hook_exec,
    hook_exec_with_script_debug_if_failure,
    CUSTOM_HOOK_FOLDER,
)
from yunohost.tools import (
    tools_postinstall,
    _tools_migrations_run_after_system_restore,
    _tools_migrations_run_before_app_restore,
)
from yunohost.regenconf import regen_conf
from yunohost.log import OperationLogger, is_unit_operation
from yunohost.repository import BackupRepository, BackupArchive, BACKUP_PATH
from yunohost.utils.config import ConfigPanel
from yunohost.utils.error import YunohostError, YunohostValidationError
from yunohost.utils.system import (
    free_space_in_directory,
    get_ynh_package_version,
    binary_to_human,
    space_used_by_directory
)
from yunohost.settings import settings_get

APP_MARGIN_SPACE_SIZE = 100  # In MB
CONF_MARGIN_SPACE_SIZE = 10  # IN MB
POSTINSTALL_ESTIMATE_SPACE_SIZE = 5  # In MB
logger = getActionLogger("yunohost.backup")


class BackupRestoreTargetsManager:

    """
    BackupRestoreTargetsManager manage the targets
    in BackupManager and RestoreManager
    """

    def __init__(self):
        self.targets = {}
        self.results = {"system": {}, "apps": {}}

    def set_result(self, category, element, value):
        """
        Change (or initialize) the current status/result of a given target.

        Args:
            category -- The category of the target

            element  -- The target for which to change the status/result

            value    -- The new status/result, among "Unknown", "Success",
                     "Warning", "Error" and "Skipped"
        """

        levels = ["Unknown", "Success", "Warning", "Error", "Skipped"]

        assert value in levels

        if element not in self.results[category].keys():
            self.results[category][element] = value
        else:
            currentValue = self.results[category][element]

            if levels.index(currentValue) > levels.index(value):
                return
            else:
                self.results[category][element] = value

    def set_wanted(
        self,
        category,
        wanted_targets,
        available_targets,
        error_if_wanted_target_is_unavailable,
    ):
        """
        Define and validate targets to be backuped or to be restored (list of
        system parts, apps..). The wanted targets are compared and filtered
        with respect to the available targets. If a wanted targets is not
        available, a call to "error_if_wanted_target_is_unavailable" is made.

        Args:
        category       -- The category (apps or system) for which to set the
                          targets ;

        wanted_targets -- List of targets which are wanted by the user. Can be
                          "None" or [], corresponding to "No targets" or "All
                          targets" ;

        available_targets -- List of targets which are really available ;

        error_if_wanted_target_is_unavailable
                          -- Callback for targets which are not available.
        """

        # If no targets wanted, set as empty list

        if wanted_targets is None:
            self.targets[category] = []

        # If all targets wanted, use all available targets
        elif wanted_targets == []:
            self.targets[category] = available_targets

        # If the user manually specified which targets to backup, we need to
        # validate that each target is actually available
        else:
            self.targets[category] = [
                part for part in wanted_targets if part in available_targets
            ]

            # Display an error for each target asked by the user but which is
            # unknown
            unavailable_targets = [
                part for part in wanted_targets if part not in available_targets
            ]

            for target in unavailable_targets:
                self.set_result(category, target, "Skipped")
                error_if_wanted_target_is_unavailable(target)

        # For target with no result yet (like 'Skipped'), set it as unknown

        if self.targets[category] is not None:
            for target in self.targets[category]:
                self.set_result(category, target, "Unknown")

        return self.list(category, exclude=["Skipped"])

    def list(self, category, include=None, exclude=None):
        """
        List targets in a given category.

        The list is filtered with a whitelist (include) or blacklist (exclude)
        with respect to the current 'result' of the target.
        """

        assert (include and isinstance(include, list) and not exclude) or (
            exclude and isinstance(exclude, list) and not include
        )

        if include:
            return [
                target

                for target in self.targets[category]

                if self.results[category][target] in include
            ]

        if exclude:
            return [
                target

                for target in self.targets[category]

                if self.results[category][target] not in exclude
            ]


class BackupManager:

    """
    This class collect files to backup in a list and apply one or several
    backup method on it.

    The list contains dict with source and dest properties. The goal of this csv
    is to list all directories and files which need to be backup in this
    archive.  The `source` property is the path of the source (dir or file).
    The `dest` property is the path where it could be placed in the archive.

    The list is filled by app backup scripts and system/user backup hooks.
    Files located in the work_dir are automatically added.

    With this list, "backup methods" are able to apply their backup strategy on
    data listed in it.  It's possible to tar each path (tar methods), to mount
    each dir into the work_dir, to copy each files (copy method) or to call a
    custom method (via a custom script).

    Note: some future backups methods (like borg) are not able to specify a
    different place than the original path. That's why the ynh_restore_file
    helpers use primarily the SOURCE_PATH as argument.

    Public properties:
        info (getter)
        work_dir (getter) # FIXME currently it's not a getter
        is_tmp_work_dir (getter)
        paths_to_backup (getter) # FIXME not a getter and list is not protected
        name (getter) # FIXME currently it's not a getter
        size (getter) # FIXME currently it's not a getter

    Public methods:
        add(self, method)
        set_system_targets(self, system_parts=[])
        set_apps_targets(self, apps=[])
        collect_files(self)
        backup(self)

    Usage:
        backup_manager = BackupManager(name="mybackup", description="bkp things")

        # Add backup method to apply
        backup_manager.add('copy', output_directory='/mnt/local_fs')
        backup_manager.add('tar', output_directory='/mnt/remote_fs')

        # Define targets to be backuped
        backup_manager.set_system_targets(["data"])
        backup_manager.set_apps_targets(["wordpress"])

        # Collect files to backup from targets
        backup_manager.collect_files()

        # Apply backup methods
        backup_manager.backup()
    """

    def __init__(self, name=None, prefix="", description="", repositories=[], work_dir=None):
        """
        BackupManager constructor

        Args:
        name        -- (string) The name of this backup (without spaces). If
                        None, the name will be generated (default: None)

        description -- (string) A description for this future backup archive
                        (default: '')

        repositories-- (List<BackupRepository>) A list of repositories

        work_dir    -- (None|string) A path where prepare the archive. If None,
                        temporary work_dir will be created (default: None)
        """
        self.description = description or ""
        self.repositories = repositories
        self.created_at = int(time.time())
        self.apps_return = {}
        self.system_return = {}
        self.paths_to_backup = []
        self.size_details = {"system": {}, "apps": {}}
        self.targets = BackupRestoreTargetsManager()

        # Define backup name if needed
        self.prefix = prefix
        if not name:
            name = self._define_backup_name()
        self.name = name

        # Define working directory if needed and initialize it
        self.work_dir = work_dir

        if self.work_dir is None:
            self.work_dir = os.path.join(BACKUP_PATH, "tmp", name)
        self._init_work_dir()

    #
    # Misc helpers
    #

    @property
    def info(self):
        """(Getter) Dict containing info about the archive being created"""

        return {
            "description": self.description,
            "created_at": self.created_at,
            "size": self.size,
            "size_details": self.size_details,
            "apps": self.apps_return,
            "system": self.system_return,
            "from_yunohost_version": get_ynh_package_version("yunohost")["version"],
        }

    @property
    def is_tmp_work_dir(self):
        """(Getter) Return true if the working directory is temporary and should
        be clean at the end of the backup"""

        return self.work_dir == os.path.join(BACKUP_PATH, "tmp", self.name)

    def __repr__(self):
        return json.dumps(self.info)

    def _define_backup_name(self):
        """Define backup name

        Return:
            (string) A backup name created from current date 'YYMMDD-HHMMSS'
        """
        # FIXME: case where this name already exist

        return self.prefix + time.strftime("%Y%m%d-%H%M%S", time.gmtime())

    def _init_work_dir(self):
        """Initialize preparation directory

        Ensure the working directory exists and is empty
        """

        # FIXME replace isdir by exists ? manage better the case where the path
        # exists

        if not os.path.isdir(self.work_dir):
            mkdir(self.work_dir, 0o750, parents=True)
        elif self.is_tmp_work_dir:

            logger.debug(
                "temporary directory for backup '%s' already exists... attempting to clean it",
                self.work_dir,
            )

            # Try to recursively unmount stuff (from a previously failed backup ?)

            if not _recursive_umount(self.work_dir):
                raise YunohostValidationError("backup_output_directory_not_empty")
            else:
                # If umount succeeded, remove the directory (we checked that
                # we're in /home/yunohost.backup/tmp so that should be okay...
                # c.f. method clean() which also does this)
                rm(self.work_dir, recursive=True, force=True)
                mkdir(self.work_dir, 0o750, parents=True)

    def clean_work_dir(self, umount=True):

        if umount and not _recursive_umount(self.work_dir):
            raise YunohostError("backup_cleaning_failed")

        if self.is_tmp_work_dir:
            rm(self.work_dir, True, True)

    #
    # Backup target management                                              #
    #

    def set_system_targets(self, system_parts=[]):
        """
        Define and validate targetted apps to be backuped

        Args:
            system_parts -- (list) list of system parts which should be backuped.
                            If empty list, all system will be backuped. If None,
                            no system parts will be backuped.
        """

        def unknown_error(part):
            logger.error(m18n.n("backup_hook_unknown", hook=part))

        self.targets.set_wanted(
            "system", system_parts, hook_list("backup")["hooks"], unknown_error
        )

    def set_apps_targets(self, apps=[]):
        """
        Define and validate targetted apps to be backuped

        Args:
        apps -- (list) list of apps which should be backuped. If given an empty
                list, all apps will be backuped. If given None, no apps will be
                backuped.
        """

        def unknown_error(app):
            logger.error(m18n.n("unbackup_app", app=app))

        target_list = self.targets.set_wanted(
            "apps", apps, os.listdir("/etc/yunohost/apps"), unknown_error
        )

        # Additionnaly, we need to check that each targetted app has a
        # backup and restore scripts

        for app in target_list:
            app_script_folder = f"/etc/yunohost/apps/{app}/scripts"
            backup_script_path = os.path.join(app_script_folder, "backup")
            restore_script_path = os.path.join(app_script_folder, "restore")

            if not os.path.isfile(backup_script_path):
                logger.warning(m18n.n("backup_with_no_backup_script_for_app", app=app))
                self.targets.set_result("apps", app, "Skipped")

            elif not os.path.isfile(restore_script_path):
                logger.warning(m18n.n("backup_with_no_restore_script_for_app", app=app))
                self.targets.set_result("apps", app, "Warning")

    #
    # Management of files to backup / "The CSV"                             #
    #

    def _import_to_list_to_backup(self, tmp_csv):
        """
        Commit collected path from system hooks or app scripts

        Args:
        tmp_csv -- (string) Path to a temporary csv file with source and
                   destinations column to add to the list of paths to backup
        """
        _call_for_each_path(self, BackupManager._add_to_list_to_backup, tmp_csv)

    def _add_to_list_to_backup(self, source, dest=None):
        """
        Mark file or directory to backup

        This method add source/dest couple to the "paths_to_backup" list.

        Args:
        source -- (string) Source path to backup

        dest   -- (string) Destination path in the archive. If it ends by a
                  slash the basename of the source path will be added. If None,
                  the source path will be used, so source files will be set up
                  at the same place and with same name than on the system.
                  (default: None)

        Usage:
        self._add_to_list_to_backup('/var/www/wordpress', 'sources')
        # => "wordpress" dir will be move and rename as "sources"

        self._add_to_list_to_backup('/var/www/wordpress', 'sources/')
        # => "wordpress" dir will be put inside "sources/" and won't be renamed

        """

        if dest is None:
            dest = source
            source = os.path.join(self.work_dir, source)

        if dest.endswith("/"):
            dest = os.path.join(dest, os.path.basename(source))
        self.paths_to_backup.append({"source": source, "dest": dest})

    def _write_csv(self):
        """
        Write the backup list into a CSV

        The goal of this csv is to list all directories and files which need to
        be backup in this archive.  For the moment, this CSV contains 2 columns.
        The first column `source` is the path of the source (dir or file).  The
        second `dest` is the path where it could be placed in the archive.

        This CSV is filled by app backup scripts and system/user hooks.
        Files in the work_dir are automatically added.

        With this CSV, "backup methods" are able to apply their backup strategy
        on data listed in it.  It's possible to tar each path (tar methods), to
        mount each dir into the work_dir, to copy each files (copy methods) or
        a custom method (via a custom script).

        Note: some future backups methods (like borg) are not able to specify a
        different place than the original path. That's why the ynh_restore_file
        helpers use primarily the SOURCE_PATH as argument.

        Error:
        backup_csv_creation_failed -- Raised if the CSV couldn't be created
        backup_csv_addition_failed -- Raised if we can't write in the CSV
        """
        self.csv_path = os.path.join(self.work_dir, "backup.csv")
        try:
            self.csv_file = open(self.csv_path, "a")
            self.fieldnames = ["source", "dest"]
            self.csv = csv.DictWriter(
                self.csv_file, fieldnames=self.fieldnames, quoting=csv.QUOTE_ALL
            )
        except (IOError, OSError, csv.Error):
            logger.error(m18n.n("backup_csv_creation_failed"))

        for row in self.paths_to_backup:
            try:
                self.csv.writerow(row)
            except csv.Error:
                logger.error(m18n.n("backup_csv_addition_failed"))
        self.csv_file.close()

    #
    # File collection from system parts and apps                            #
    #

    def collect_files(self):
        """
        Collect all files to backup, write its into a CSV and create a
        info.json file

        Files to backup are listed by system parts backup hooks and by backup
        app scripts that have been defined with the set_targets() method.

        Some files or directories inside the working directory are added by
        default:

        info.json  -- info about the archive
        backup.csv -- a list of paths to backup
        apps/      -- some apps generate here temporary files to backup (like
                      database dump)
        conf/      -- system configuration backup scripts could generate here
                      temporary files to backup
        data/      -- system data backup scripts could generate here temporary
                      files to backup
        hooks/     -- restore scripts associated to system backup scripts are
                      copied here
        """

        self._collect_system_files()
        self._collect_apps_files()

        # Check if something has been saved ('success' or 'warning')
        successfull_apps = self.targets.list("apps", include=["Success", "Warning"])
        successfull_system = self.targets.list("system", include=["Success", "Warning"])

        if not successfull_apps and not successfull_system:
            rm(self.work_dir, True, True)
            raise YunohostError("backup_nothings_done")

        # Add unlisted files from backup tmp dir
        self._add_to_list_to_backup("backup.csv")
        self._add_to_list_to_backup("info.json")

        for app in self.apps_return.keys():
            self._add_to_list_to_backup(f"apps/{app}")

        if os.path.isdir(os.path.join(self.work_dir, "conf")):
            self._add_to_list_to_backup("conf")

        if os.path.isdir(os.path.join(self.work_dir, "data")):
            self._add_to_list_to_backup("data")

        # Write CSV file
        self._write_csv()

        # Calculate total size
        self._compute_backup_size()

        # Create backup info file
        with open(f"{self.work_dir}/info.json", "w") as f:
            f.write(json.dumps(self.info))

    def _get_env_var(self, app=None):
        """
        Define environment variables for apps or system backup scripts.

        Args:
        app -- (string|None) The instance name of the app we want the variable
        environment. If you want a variable environment for a system backup
        script keep None. (default: None)

        Return:
            (Dictionnary) The environment variables to apply to the script
        """
        env_var = {}

        _, tmp_csv = tempfile.mkstemp(prefix="backupcsv_")
        env_var["YNH_BACKUP_DIR"] = self.work_dir
        env_var["YNH_BACKUP_CSV"] = tmp_csv

        if app is not None:
            env_var.update(_make_environment_for_app_script(app, action="backup"))
            env_var["YNH_APP_BACKUP_DIR"] = os.path.join(
                self.work_dir, "apps", app, "backup"
            )

        return env_var

    def _collect_system_files(self):
        """
        List file to backup for each selected system part

        This corresponds to scripts in data/hooks/backup/ (system hooks) and
        to those in /etc/yunohost/hooks.d/backup/ (user hooks)

        Environment variables:
        YNH_BACKUP_DIR -- The backup working directory (in
                          "/home/yunohost.backup/tmp/BACKUPNAME" or could be
                          defined by the user)
        YNH_BACKUP_CSV -- A temporary CSV where the script whould list paths toi
                          backup
        """

        system_targets = self.targets.list("system", exclude=["Skipped"])

        # If nothing to backup, return immediately

        if system_targets == []:
            return

        logger.debug(m18n.n("backup_running_hooks"))

        # Prepare environnement
        env_dict = self._get_env_var()

        # Actual call to backup scripts/hooks

        ret = hook_callback(
            "backup",
            system_targets,
            args=[self.work_dir],
            env=env_dict,
            chdir=self.work_dir,
        )

        ret_succeed = {
            hook: [
                path for path, result in infos.items() if result["state"] == "succeed"
            ]

            for hook, infos in ret.items()

            if any(result["state"] == "succeed" for result in infos.values())
        }
        ret_failed = {
            hook: [
                path for path, result in infos.items() if result["state"] == "failed"
            ]

            for hook, infos in ret.items()

            if any(result["state"] == "failed" for result in infos.values())
        }

        if list(ret_succeed.keys()) != []:
            self.system_return = ret_succeed

        # Add files from targets (which they put in the CSV) to the list of
        # files to backup
        self._import_to_list_to_backup(env_dict["YNH_BACKUP_CSV"])

        # Save restoration hooks for each part that suceeded (and which have
        # a restore hook available)

        restore_hooks_dir = os.path.join(self.work_dir, "hooks", "restore")

        if not os.path.exists(restore_hooks_dir):
            mkdir(restore_hooks_dir, mode=0o700, parents=True, uid="root")

        restore_hooks = hook_list("restore")["hooks"]

        for part in ret_succeed.keys():
            if part in restore_hooks:
                part_restore_hooks = hook_info("restore", part)["hooks"]

                for hook in part_restore_hooks:
                    self._add_to_list_to_backup(hook["path"], "hooks/restore/")
                self.targets.set_result("system", part, "Success")
            else:
                logger.warning(m18n.n("restore_hook_unavailable", hook=part))
                self.targets.set_result("system", part, "Warning")

        for part in ret_failed.keys():
            logger.error(m18n.n("backup_system_part_failed", part=part))
            self.targets.set_result("system", part, "Error")

    def _collect_apps_files(self):
        """Prepare backup for each selected apps"""

        apps_targets = self.targets.list("apps", exclude=["Skipped"])

        for app_instance_name in apps_targets:
            self._collect_app_files(app_instance_name)

    def _collect_app_files(self, app):
        """
        List files to backup for the app into the paths_to_backup dict.

        If the app backup script fails, paths from this app already listed for
        backup aren't added to the general list and will be ignored

        Environment variables:
        YNH_BACKUP_DIR -- The backup working directory (in
                          "/home/yunohost.backup/tmp/BACKUPNAME" or could be
                          defined by the user)
        YNH_BACKUP_CSV -- A temporary CSV where the script whould list paths toi
                          backup
        YNH_APP_BACKUP_DIR -- The directory where the script should put
                              temporary files to backup like database dump,
                              files in this directory don't need to be added to
                              the temporary CSV.
        YNH_APP_ID     -- The app id (eg wordpress)
        YNH_APP_INSTANCE_NAME -- The app instance name (eg wordpress__3)
        YNH_APP_INSTANCE_NUMBER  -- The app instance number (eg 3)


        Args:
        app -- (string) an app instance name (already installed) to backup
        """
        from yunohost.permission import user_permission_list

        app_setting_path = os.path.join("/etc/yunohost/apps/", app)

        # Prepare environment
        env_dict = self._get_env_var(app)
        env_dict["YNH_APP_BASEDIR"] = os.path.join(
            self.work_dir, "apps", app, "settings"
        )
        tmp_app_bkp_dir = env_dict["YNH_APP_BACKUP_DIR"]
        settings_dir = os.path.join(self.work_dir, "apps", app, "settings")

        logger.info(m18n.n("app_start_backup", app=app))
        tmp_workdir_for_app = _make_tmp_workdir_for_app(app=app)
        try:
            # Prepare backup directory for the app
            mkdir(tmp_app_bkp_dir, 0o700, True, uid="root")

            # Copy the app settings to be able to call _common.sh
            shutil.copytree(app_setting_path, settings_dir)

            hook_exec(
                f"{tmp_workdir_for_app}/scripts/backup",
                raise_on_error=True,
                chdir=tmp_app_bkp_dir,
                env=env_dict,
            )[0]

            self._import_to_list_to_backup(env_dict["YNH_BACKUP_CSV"])

            # backup permissions
            logger.debug(m18n.n("backup_permission", app=app))
            permissions = user_permission_list(full=True, apps=[app])["permissions"]
            this_app_permissions = {name: infos for name, infos in permissions.items()}
            write_to_yaml(f"{settings_dir}/permissions.yml", this_app_permissions)

        except Exception as e:
            logger.debug(e)
            abs_tmp_app_dir = os.path.join(self.work_dir, "apps/", app)
            shutil.rmtree(abs_tmp_app_dir, ignore_errors=True)
            logger.error(m18n.n("backup_app_failed", app=app))
            self.targets.set_result("apps", app, "Error")
        else:
            # Add app info
            i = app_info(app)
            self.apps_return[app] = {
                "version": i["version"],
                "name": i["name"],
                "description": i["description"],
            }
            self.targets.set_result("apps", app, "Success")

        # Remove tmp files in all situations
        finally:
            shutil.rmtree(tmp_workdir_for_app)
            rm(env_dict["YNH_BACKUP_CSV"], force=True)

    #
    # Actual backup archive creation / method management                    #
    #

    def backup(self):
        """Backup files in each repository"""
        result = {}
        for repo in self.repositories:
            logger.debug(m18n.n("backuping_in_repository", repository=repo.entity))
            try:
                archive = BackupArchive(repo, name=self.name, manager=self)
                archive.organize_and_backup()
            except Exception:
                import traceback
                result[repo.entity] = "Error"
                logger.error(m18n.n("backup_in_repository_error", repository=repo.entity, error=traceback.format_exc()))
            else:
                result[repo.entity] = "Sent"
                logger.debug(m18n.n("backup_in_repository_finished", repository=repo.entity))

        return result

    def _compute_backup_size(self):
        """
        Compute backup global size and details size for each apps and system
        parts

        Update self.size and self.size_details

        Note: currently, these sizes are the size in this archive, not really
        the size of needed to restore the archive. To know the size needed to
        restore we should consider apt/npm/pip dependencies space and database
        dump restore operations.

        Return:
            (int) The global size of the archive in bytes
        """
        # FIXME Database dump will be loaded, so dump should use almost the
        # double of their space
        # FIXME Some archive will set up dependencies, those are not in this
        # size info
        self.size = 0

        for system_key in self.system_return:
            self.size_details["system"][system_key] = 0

        for app_key in self.apps_return:
            self.size_details["apps"][app_key] = 0

        for row in self.paths_to_backup:
            if row["dest"] == "info.json":
                continue

            size = space_used_by_directory(row["source"], follow_symlinks=False)

            # Add size to apps details
            splitted_dest = row["dest"].split("/")
            category = splitted_dest[0]

            if category == "apps":
                for app_key in self.apps_return:
                    if row["dest"].startswith("apps/" + app_key):
                        self.size_details["apps"][app_key] += size

                        break

            # OR Add size to the correct system element
            elif category == "data" or category == "conf":
                for system_key in self.system_return:
                    if row["dest"].startswith(system_key.replace("_", "/")):
                        self.size_details["system"][system_key] += size

                        break

            self.size += size

        return self.size


class RestoreManager:

    """
    RestoreManager allow to restore a past backup archive

    Currently it's a tar file, but it could be another kind of archive

    Public properties:
        info (getter)i # FIXME
        work_dir (getter) # FIXME currently it's not a getter
        name (getter) # FIXME currently it's not a getter
        success (getter)
        result (getter) # FIXME

    Public methods:
        set_targets(self, system_parts=[], apps=[])
        restore(self)

    Usage:
        restore_manager = RestoreManager(name)

        restore_manager.set_targets(None, ['wordpress__3'])

        restore_manager.restore()

        if restore_manager.success:
            logger.success(m18n.n('restore_complete'))

        return restore_manager.result
    """

    def __init__(self, archive):
        """
        RestoreManager constructor

        Args:
        archive -- (BackupArchive) The archive to restore
        """
        # Retrieve and open the archive
        # FIXME this way to get the info is not compatible with copy or custom
        self.archive = archive

        # backup methods
        self.info = archive.info()  # FIXME with_details=True

        from_version = self.info.get("from_yunohost_version", "")
        # Remove any '~foobar' in the version ... c.f ~alpha, ~beta version during
        # early dev for next debian version
        from_version = re.sub(r"~\w+", "", from_version)

        if not from_version or version.parse(from_version) < version.parse("4.2.0"):
            raise YunohostValidationError("restore_backup_too_old")

        self.targets = BackupRestoreTargetsManager()

    #
    # Misc helpers
    #

    @property
    def success(self):

        successful_apps = self.targets.list("apps", include=["Success", "Warning"])
        successful_system = self.targets.list("system", include=["Success", "Warning"])

        return len(successful_apps) != 0 or len(successful_system) != 0

    def _postinstall_if_needed(self):
        """
        Post install yunohost if needed
        """
        # Check if YunoHost is installed

        if not os.path.isfile("/etc/yunohost/installed"):
            # Retrieve the domain from the backup
            try:
                with open(f"{self.work_dir}/conf/ynh/current_host", "r") as f:
                    domain = f.readline().rstrip()
            except IOError:
                logger.debug(
                    "unable to retrieve current_host from the backup", exc_info=1
                )
                # FIXME include the current_host by default ?
                raise YunohostError(
                    "The main domain name cannot be retrieved from inside the archive, and is needed to perform the postinstall",
                    raw_msg=True,
                )

            logger.debug("executing the post-install...")
            tools_postinstall(domain, "Yunohost", True)

    def clean(self):
        """
        End a restore operations by cleaning the working directory and
        regenerate ssowat conf (if some apps were restored)
        """
        from .permission import permission_sync_to_user

        permission_sync_to_user()

        if os.path.ismount(self.work_dir):
            ret = subprocess.call(["umount", self.work_dir])

            if ret != 0:
                logger.warning(m18n.n("restore_cleaning_failed"))
        rm(self.work_dir, recursive=True, force=True)

    #
    # Restore target manangement                                            #
    #

    def set_system_targets(self, system_parts=[]):
        """
        Define system parts that will be restored

        Args:
        system_parts -- (list) list of system parts which should be restored.
                        If an empty list if given, restore all system part in
                        the archive. If None is given, no system will be restored.
        """

        def unknown_error(part):
            logger.error(m18n.n("backup_archive_system_part_not_available", part=part))

        target_list = self.targets.set_wanted(
            "system", system_parts, self.info["system"].keys(), unknown_error
        )

        # Now we need to check that the restore hook is actually available for
        # all targets we want to restore

        # These are the hooks on the current installation
        available_restore_system_hooks = hook_list("restore")["hooks"]

        custom_restore_hook_folder = os.path.join(CUSTOM_HOOK_FOLDER, "restore")
        mkdir(custom_restore_hook_folder, 755, parents=True, force=True)

        for system_part in target_list:
            # By default, we'll use the restore hooks on the current install
            # if available

            # FIXME: so if the restore hook exist we use the new one and not
            # the one from backup. So hook should not break compatibility..

            if system_part in available_restore_system_hooks:
                continue

            # Otherwise, attempt to find it (or them?) in the archive

            # If we didn't find it, we ain't gonna be able to restore it

            if (
                system_part not in self.info["system"]
                or "paths" not in self.info["system"][system_part]
                or len(self.info["system"][system_part]["paths"]) == 0
            ):
                logger.error(m18n.n("restore_hook_unavailable", part=system_part))
                self.targets.set_result("system", system_part, "Skipped")

                continue

            hook_paths = self.info["system"][system_part]["paths"]
            hook_paths = [f"hooks/restore/{os.path.basename(p)}" for p in hook_paths]

            # Otherwise, add it from the archive to the system
            # FIXME: Refactor hook_add and use it instead

            for hook_path in hook_paths:
                logger.debug(
                    f"Adding restoration script '{hook_path}' to the system "
                    f"from the backup archive '{self.archive.archive_path}'"
                )
                self.method.copy(hook_path, custom_restore_hook_folder)

    def set_apps_targets(self, apps=[]):
        """
        Define and validate targetted apps to be restored

        Args:
        apps -- (list) list of apps which should be restored. If [] is given,
                all apps in the archive will be restored. If None is given,
                no apps will be restored.
        """

        def unknown_error(app):
            logger.error(m18n.n("backup_archive_app_not_found", app=app))

        to_be_restored = self.targets.set_wanted(
            "apps", apps, self.info["apps"].keys(), unknown_error
        )

        # If all apps to restore are already installed, stop right here.
        # Otherwise, if at least one app can be restored, we keep going on
        # because those which can be restored will indeed be restored
        already_installed = [app for app in to_be_restored if _is_installed(app)]

        if already_installed != []:
            if already_installed == to_be_restored:
                raise YunohostValidationError(
                    "restore_already_installed_apps", apps=", ".join(already_installed)
                )
            else:
                logger.warning(
                    m18n.n(
                        "restore_already_installed_apps",
                        apps=", ".join(already_installed),
                    )
                )

    #
    # Archive mounting                                                      #
    #

    def extract(self):
        """
        Mount the archive. We avoid copy to be able to restore on system without
        too many space.

        Use the mount method from the BackupMethod instance and read info about
        this archive
        """

        self.work_dir = os.path.join(BACKUP_PATH, "tmp", self.archive.name)

        if os.path.ismount(self.work_dir):
            logger.debug("An already mounting point '%s' already exists", self.work_dir)
            ret = subprocess.call(["umount", self.work_dir])

            if ret == 0:
                subprocess.call(["rmdir", self.work_dir])
                logger.debug(f"Unmount dir: {self.work_dir}")
            else:
                raise YunohostError("restore_removing_tmp_dir_failed")
        elif os.path.isdir(self.work_dir):
            logger.debug(
                "temporary restore directory '%s' already exists", self.work_dir
            )
            ret = subprocess.call(["rm", "-Rf", self.work_dir])

            if ret == 0:
                logger.debug(f"Delete dir: {self.work_dir}")
            else:
                raise YunohostError("restore_removing_tmp_dir_failed")

        mkdir(self.work_dir, parents=True)

        # Select paths to extract
        paths = ["backup.csv", "info.json", "hooks"]
        paths += [f"apps/{app}" for app in self.targets.list("apps", exclude=["Skipped"])]
        for system in self.targets.list("system", exclude=["Skipped"]):
            if system.startswith("data"):
                paths.append(f"data/{system}")
            elif system.startswith("conf_ynh"):
                if "conf/ynh" not in paths:
                    paths.append("conf/ynh")
            else:
                paths.append(system.replace("_", "/", 1))

        if not self.targets.list("system", exclude=["Skipped"]):
            paths.remove("hooks")

        logger.debug(f"List of paths to extract: {paths}")

        self.archive.extract(paths=paths, destination=self.work_dir)

    #
    # Space computation / checks                                            #
    #

    def _compute_needed_space(self):
        """
        Compute needed space to be able to restore

        Return:
        size   -- (int) needed space to backup in bytes
        margin -- (int) margin to be sure the backup don't fail by missing space
                  in bytes
        """
        system = self.targets.list("system", exclude=["Skipped"])
        apps = self.targets.list("apps", exclude=["Skipped"])
        restore_all_system = system == self.info["system"].keys()
        restore_all_apps = apps == self.info["apps"].keys()

        # If complete restore operations (or legacy archive)
        margin = CONF_MARGIN_SPACE_SIZE * 1024 * 1024

        if (restore_all_system and restore_all_apps) or "size_details" not in self.info:
            size = self.info["size"]

            if (
                "size_details" not in self.info
                or self.info["size_details"]["apps"] != {}
            ):
                margin = APP_MARGIN_SPACE_SIZE * 1024 * 1024
        # Partial restore don't need all backup size
        else:
            size = 0

            if system is not None:
                for system_element in system:
                    size += self.info["size_details"]["system"][system_element]

            # TODO how to know the dependencies size ?

            if apps is not None:
                for app in apps:
                    size += self.info["size_details"]["apps"][app]
                    margin = APP_MARGIN_SPACE_SIZE * 1024 * 1024

        if not os.path.isfile("/etc/yunohost/installed"):
            size += POSTINSTALL_ESTIMATE_SPACE_SIZE * 1024 * 1024

        return (size, margin)

    def assert_enough_free_space(self):
        """
        Check available disk space
        """

        free_space = free_space_in_directory(BACKUP_PATH)

        (needed_space, margin) = self._compute_needed_space()

        if free_space >= needed_space + margin:
            return True
        elif free_space > needed_space:
            # TODO Add --force options to avoid the error raising
            raise YunohostValidationError(
                "restore_may_be_not_enough_disk_space",
                free_space=free_space,
                needed_space=needed_space,
                margin=margin,
            )
        else:
            raise YunohostValidationError(
                "restore_not_enough_disk_space",
                free_space=free_space,
                needed_space=needed_space,
                margin=margin,
            )

    #
    # "Actual restore" (reverse step of the backup collect part)            #
    #

    def restore(self):
        """
        Restore the archive

        Restore system parts and apps after mounting the archive, checking free
        space and postinstall if needed
        """

        try:
            self._postinstall_if_needed()

            # Apply dirty patch to redirect php5 file on php7
            self._patch_legacy_php_versions_in_csv_file()

            self._restore_system()
            self._restore_apps()
        except Exception as e:
            raise YunohostError(
                f"The following critical error happened during restoration: {e}"
            )
        finally:
            self.clean()

    def _patch_legacy_php_versions_in_csv_file(self):
        """
        Apply dirty patch to redirect php5 and php7.0 files to php7.4
        """
        from yunohost.utils.legacy import LEGACY_PHP_VERSION_REPLACEMENTS

        backup_csv = os.path.join(self.work_dir, "backup.csv")

        if not os.path.isfile(backup_csv):
            return

        replaced_something = False
        with open(backup_csv) as csvfile:
            reader = csv.DictReader(csvfile, fieldnames=["source", "dest"])
            newlines = []

            for row in reader:
                for pattern, replace in LEGACY_PHP_VERSION_REPLACEMENTS:
                    if pattern in row["source"]:
                        replaced_something = True
                        row["source"] = row["source"].replace(pattern, replace)

                newlines.append(row)

        if not replaced_something:
            return

        with open(backup_csv, "w") as csvfile:
            writer = csv.DictWriter(
                csvfile, fieldnames=["source", "dest"], quoting=csv.QUOTE_ALL
            )

            for row in newlines:
                writer.writerow(row)

    def _restore_system(self):
        """Restore user and system parts"""

        system_targets = self.targets.list("system", exclude=["Skipped"])

        # If nothing to restore, return immediately

        if system_targets == []:
            return

        from yunohost.permission import (
            permission_create,
            permission_delete,
            user_permission_list,
            permission_sync_to_user,
        )

        # Backup old permission for apps
        # We need to do that because in case of an app is installed we can't remove the permission for this app
        old_apps_permission = user_permission_list(ignore_system_perms=True, full=True)[
            "permissions"
        ]

        # Start register change on system
        operation_logger = OperationLogger("backup_restore_system")
        operation_logger.start()

        logger.debug(m18n.n("restore_running_hooks"))

        env_dict = {
            "YNH_BACKUP_DIR": self.work_dir,
            "YNH_BACKUP_CSV": os.path.join(self.work_dir, "backup.csv"),
        }
        operation_logger.extra["env"] = env_dict
        operation_logger.flush()
        ret = hook_callback(
            "restore",
            system_targets,
            args=[self.work_dir],
            env=env_dict,
            chdir=self.work_dir,
        )

        ret_succeed = [
            hook

            for hook, infos in ret.items()

            if any(result["state"] == "succeed" for result in infos.values())
        ]
        ret_failed = [
            hook

            for hook, infos in ret.items()

            if any(result["state"] == "failed" for result in infos.values())
        ]

        for part in ret_succeed:
            self.targets.set_result("system", part, "Success")

        error_part = []

        for part in ret_failed:
            logger.error(m18n.n("restore_system_part_failed", part=part))
            self.targets.set_result("system", part, "Error")
            error_part.append(part)

        if ret_failed:
            operation_logger.error(
                m18n.n("restore_system_part_failed", part=", ".join(error_part))
            )
        else:
            operation_logger.success()

        yunohost.domain.domain_list_cache = {}

        regen_conf()

        _tools_migrations_run_after_system_restore(
            backup_version=self.info["from_yunohost_version"]
        )

        # Remove all permission for all app still in the LDAP

        for permission_name in user_permission_list(ignore_system_perms=True)[
            "permissions"
        ].keys():
            permission_delete(permission_name, force=True, sync_perm=False)

        # Restore permission for apps installed

        for permission_name, permission_infos in old_apps_permission.items():
            app_name, perm_name = permission_name.split(".")

            if _is_installed(app_name):
                permission_create(
                    permission_name,
                    allowed=permission_infos["allowed"],
                    url=permission_infos["url"],
                    additional_urls=permission_infos["additional_urls"],
                    auth_header=permission_infos["auth_header"],
                    label=permission_infos["label"]

                    if perm_name == "main"
                    else permission_infos["sublabel"],
                    show_tile=permission_infos["show_tile"],
                    protected=permission_infos["protected"],
                    sync_perm=False,
                )

        permission_sync_to_user()

    def _restore_apps(self):
        """Restore all apps targeted"""

        apps_targets = self.targets.list("apps", exclude=["Skipped"])

        for app in apps_targets:
            self._restore_app(app)

    def _restore_app(self, app_instance_name):
        """
        Restore an app

        Environment variables:
        YNH_BACKUP_DIR -- The backup working directory (in
                          "/home/yunohost.backup/tmp/BACKUPNAME" or could be
                          defined by the user)
        YNH_BACKUP_CSV -- A temporary CSV where the script whould list paths to
                          backup
        YNH_APP_BACKUP_DIR -- The directory where the script should put
                              temporary files to backup like database dump,
                              files in this directory don't need to be added to
                              the temporary CSV.
        YNH_APP_ID               -- The app id (eg wordpress)
        YNH_APP_INSTANCE_NAME    -- The app instance name (eg wordpress__3)
        YNH_APP_INSTANCE_NUMBER  -- The app instance number (eg 3)

        Args:
        app_instance_name -- (string) The app name to restore (no app with this
                             name should be already install)
        """
        from yunohost.utils.legacy import (
            _patch_legacy_php_versions,
            _patch_legacy_php_versions_in_settings,
            _patch_legacy_helpers,
        )
        from yunohost.user import user_group_list
        from yunohost.permission import (
            permission_create,
            permission_delete,
            user_permission_list,
            permission_sync_to_user,
        )

        def copytree(src, dst, symlinks=False, ignore=None):
            for item in os.listdir(src):
                s = os.path.join(src, item)
                d = os.path.join(dst, item)

                if os.path.isdir(s):
                    shutil.copytree(s, d, symlinks, ignore)
                else:
                    shutil.copy2(s, d)

        # Check if the app is not already installed

        if _is_installed(app_instance_name):
            logger.error(m18n.n("restore_already_installed_app", app=app_instance_name))
            self.targets.set_result("apps", app_instance_name, "Error")

            return

        # Start register change on system
        related_to = [("app", app_instance_name)]
        operation_logger = OperationLogger("backup_restore_app", related_to)
        operation_logger.start()

        logger.info(m18n.n("app_start_restore", app=app_instance_name))

        app_dir_in_archive = os.path.join(self.work_dir, "apps", app_instance_name)
        app_backup_in_archive = os.path.join(app_dir_in_archive, "backup")
        app_settings_in_archive = os.path.join(app_dir_in_archive, "settings")
        app_scripts_in_archive = os.path.join(app_settings_in_archive, "scripts")

        # Attempt to patch legacy helpers...
        _patch_legacy_helpers(app_settings_in_archive)

        # Apply dirty patch to make php5 apps compatible with php7
        _patch_legacy_php_versions(app_settings_in_archive)
        _patch_legacy_php_versions_in_settings(app_settings_in_archive)

        # Delete _common.sh file in backup
        common_file = os.path.join(app_backup_in_archive, "_common.sh")
        rm(common_file, force=True)

        # Check if the app has a restore script
        app_restore_script_in_archive = os.path.join(app_scripts_in_archive, "restore")

        if not os.path.isfile(app_restore_script_in_archive):
            logger.warning(m18n.n("unrestore_app", app=app_instance_name))
            self.targets.set_result("apps", app_instance_name, "Warning")

            return

        try:
            # Restore app settings
            app_settings_new_path = os.path.join(
                "/etc/yunohost/apps/", app_instance_name
            )
            app_scripts_new_path = os.path.join(app_settings_new_path, "scripts")
            shutil.copytree(app_settings_in_archive, app_settings_new_path)
            chmod(app_settings_new_path, 0o400, 0o400, True)
            chown(app_scripts_new_path, "root", None, True)

            # Copy the app scripts to a writable temporary folder
            tmp_workdir_for_app = _make_tmp_workdir_for_app()
            copytree(app_scripts_in_archive, tmp_workdir_for_app)
            chmod(tmp_workdir_for_app, 0o700, 0o700, True)
            chown(tmp_workdir_for_app, "root", None, True)
            restore_script = os.path.join(tmp_workdir_for_app, "restore")

            # Restore permissions

            if not os.path.isfile(f"{app_settings_new_path}/permissions.yml"):
                raise YunohostError(
                    "Didnt find a permssions.yml for the app !?", raw_msg=True
                )

            permissions = read_yaml(f"{app_settings_new_path}/permissions.yml")
            existing_groups = user_group_list()["groups"]

            for permission_name, permission_infos in permissions.items():

                if "allowed" not in permission_infos:
                    logger.warning(
                        f"'allowed' key corresponding to allowed groups for permission {permission_name} not found when restoring app {app_instance_name} … You might have to reconfigure permissions yourself."
                    )
                    should_be_allowed = ["all_users"]
                else:
                    should_be_allowed = [
                        g for g in permission_infos["allowed"] if g in existing_groups
                    ]

                perm_name = permission_name.split(".")[1]
                permission_create(
                    permission_name,
                    allowed=should_be_allowed,
                    url=permission_infos.get("url"),
                    additional_urls=permission_infos.get("additional_urls"),
                    auth_header=permission_infos.get("auth_header"),
                    label=permission_infos.get("label")

                    if perm_name == "main"
                    else permission_infos.get("sublabel"),
                    show_tile=permission_infos.get("show_tile", True),
                    protected=permission_infos.get("protected", False),
                    sync_perm=False,
                )

            permission_sync_to_user()

            os.remove(f"{app_settings_new_path}/permissions.yml")

            _tools_migrations_run_before_app_restore(
                backup_version=self.info["from_yunohost_version"],
                app_id=app_instance_name,
            )
        except Exception:
            import traceback

            error = m18n.n("unexpected_error", error="\n" + traceback.format_exc())
            msg = m18n.n("app_restore_failed", app=app_instance_name, error=error)
            logger.error(msg)
            operation_logger.error(msg)

            self.targets.set_result("apps", app_instance_name, "Error")

            # Cleanup
            shutil.rmtree(app_settings_new_path, ignore_errors=True)
            shutil.rmtree(tmp_workdir_for_app, ignore_errors=True)

            return

        logger.debug(m18n.n("restore_running_app_script", app=app_instance_name))

        # Prepare env. var. to pass to script
        # FIXME : workdir should be a tmp workdir
        app_workdir = os.path.join(self.work_dir, "apps", app_instance_name, "settings")
        env_dict = _make_environment_for_app_script(
            app_instance_name, workdir=app_workdir, action="restore"
        )
        env_dict.update(
            {
                "YNH_BACKUP_DIR": self.work_dir,
                "YNH_BACKUP_CSV": os.path.join(self.work_dir, "backup.csv"),
                "YNH_APP_BACKUP_DIR": os.path.join(
                    self.work_dir, "apps", app_instance_name, "backup"
                ),
            }
        )

        operation_logger.extra["env"] = env_dict
        operation_logger.flush()

        manifest = _get_manifest_of_app(app_settings_in_archive)
        if manifest["packaging_format"] >= 2:
            from yunohost.utils.resources import AppResourceManager
            try:
                AppResourceManager(app_instance_name, wanted=manifest, current={}).apply(rollback_if_failure=True)
            except Exception:
                # FIXME : improve error handling ....
                raise

        # Execute the app install script
        restore_failed = True
        try:
            (
                restore_failed,
                failure_message_with_debug_instructions,
            ) = hook_exec_with_script_debug_if_failure(
                restore_script,
                chdir=app_backup_in_archive,
                env=env_dict,
                operation_logger=operation_logger,
                error_message_if_script_failed=m18n.n("app_restore_script_failed"),
                error_message_if_failed=lambda e: m18n.n(
                    "app_restore_failed", app=app_instance_name, error=e
                ),
            )
        finally:
            # Cleaning temporary scripts directory
            shutil.rmtree(tmp_workdir_for_app, ignore_errors=True)

            if not restore_failed:
                self.targets.set_result("apps", app_instance_name, "Success")
                operation_logger.success()
            else:

                self.targets.set_result("apps", app_instance_name, "Error")

                remove_script = os.path.join(app_scripts_in_archive, "remove")

                # Setup environment for remove script
                env_dict_remove = _make_environment_for_app_script(
                    app_instance_name, workdir=app_workdir
                )
                remove_operation_logger = OperationLogger(
                    "remove_on_failed_restore",
                    [("app", app_instance_name)],
                    env=env_dict_remove,
                )
                remove_operation_logger.start()

                # Execute remove script

                if hook_exec(remove_script, env=env_dict_remove)[0] != 0:
                    msg = m18n.n("app_not_properly_removed", app=app_instance_name)
                    logger.warning(msg)
                    remove_operation_logger.error(msg)
                else:
                    remove_operation_logger.success()

                # Cleaning app directory
                shutil.rmtree(app_settings_new_path, ignore_errors=True)

                # Remove all permission in LDAP for this app

                for permission_name in user_permission_list()["permissions"].keys():
                    if permission_name.startswith(app_instance_name + "."):
                        permission_delete(permission_name, force=True)

                # TODO Cleaning app hooks

                logger.error(failure_message_with_debug_instructions)


#
# "Front-end"                                                               #
#

@is_unit_operation()
def backup_create(
    operation_logger,
    name=None,
    prefix="",
    description=None,
    repositories=[],
    system=[],
    apps=[],
    dry_run=False,
):
    """
    Create a backup local archive

    Keyword arguments:
        name -- Name of the backup archive
        description -- Short description of the backup
        repositories -- Repositories in which we want to save the backup
        system -- List of system elements to backup
        apps -- List of application names to backup
        dry_run -- Run ynh backup script without send the files into a repo
    """

    # TODO: Add a 'clean' argument to clean output directory

    #
    # Validate / parse arguments                                            #
    #

    # Add backup repositories

    if not repositories:
        repositories = settings_get("misc.backup.backup_default_repositories")

    # Validate there is no archive with the same name
    archives = backup_list(repositories=repositories)
    archives_already_exists = []
    for repository in archives:
        if name and name in archives[repository]:
            repositories.remove(repository)
            archives_already_exists.append(repository)
            logger.error(m18n.n("backup_archive_name_exists", repository=repository))

    if not repositories:
        raise YunohostValidationError("backup_nowhere_to_backup")

    # If no --system or --apps given, backup everything

    if system is None and apps is None:
        system = []
        apps = []

    #
    # Intialize                                                             #
    #

    operation_logger.start()

    repositories = [BackupRepository(repo) for repo in repositories]

    # Prepare files to backup
    backup_manager = BackupManager(name, prefix=prefix,
                                   description=description,
                                   repositories=repositories)

    # Add backup targets (system and apps)
    backup_manager.set_system_targets(system)
    backup_manager.set_apps_targets(apps)

    for app in backup_manager.targets.list("apps", exclude=["Skipped"]):
        operation_logger.related_to.append(("app", app))
    operation_logger.flush()

    #
    # Collect files and put them in the archive                             #
    #

    # Collect files to be backup (by calling app backup script / system hooks)
    backup_manager.collect_files()

    parts_results = backup_manager.targets.results
    parts_results = list(parts_results["apps"].values()) + list(parts_results["system"].values())
    parts_states = [v in ["Success", "Skipped"] for v in parts_results]
    if not any(parts_states):
        raise YunohostError("backup_nothings_done")

    if dry_run:
        return {
            "size": backup_manager.size,
            "size_details": backup_manager.size_details,
        }

    # Apply backup methods on prepared files
    logger.info(m18n.n("backup_actually_backuping"))
    logger.info(
        m18n.n(
            "backup_create_size_estimation",
            size=binary_to_human(backup_manager.size) + "B",
        )
    )
    repo_results = backup_manager.backup()
    repo_results.update({repo: "Not sent" for repo in archives_already_exists})
    repo_states = [repo_result == "Sent" for repository, repo_result in repo_results.items()]

    if all(repo_states) and all(parts_states):
        logger.success(m18n.n("backup_created"))
        operation_logger.success()
    else:
        if not any(repo_states):
            error = m18n.n("backup_not_sent")
        elif not all(repo_states):
            error = m18n.n("backup_partially_sent")

        if not all(parts_states):
            error += "\n" + m18n.n("backup_files_not_fully_collected")
            for repository, repo_result in repo_results.items():
                if repo_result == "Sent":
                    repo_results[repository] = "Incomplete"

        logger.error(error)
        operation_logger.error(error)

    return {
        "name": backup_manager.name,
        "size": backup_manager.size,
        "results": backup_manager.targets.results,
        "states": repo_results
    }


def backup_restore(name, repository=None, system=[], apps=[], force=False):
    """
    Restore from a local backup archive

    Keyword argument:
        name -- Name of the local backup archive
        force -- Force restauration on an already installed system
        system -- List of system parts to restore
        apps -- List of application names to restore
    """

    #
    # Validate / parse arguments                                            #
    #

    # If no --system or --apps given, restore everything

    if system is None and apps is None:
        system = []
        apps = []

    if not repository:
        repository = settings_get("misc.backup.backup_default_repositories")[0]

    #
    # Initialize                                                            #
    #

    if name.endswith(".tar.gz"):
        name = name[: -len(".tar.gz")]
    elif name.endswith(".tar"):
        name = name[: -len(".tar")]

    repo = BackupRepository(repository)
    archive = BackupArchive(repo, name)

    restore_manager = RestoreManager(archive)

    restore_manager.set_system_targets(system)
    restore_manager.set_apps_targets(apps)

    restore_manager.assert_enough_free_space()

    #
    # Add validation if restoring system parts on an already-installed system
    #

    if restore_manager.targets.targets["system"] != [] and os.path.isfile(
        "/etc/yunohost/installed"
    ):
        logger.warning(m18n.n("yunohost_already_installed"))

        if not force:
            try:
                # Ask confirmation for restoring
                i = Moulinette.prompt(
                    m18n.n("restore_confirm_yunohost_installed", answers="y/N")
                )
            except NotImplemented:
                pass
            else:
                if i == "y" or i == "Y":
                    force = True

            if not force:
                raise YunohostError("restore_failed")

    #
    # Mount the archive then call the restore for each system part / app    #
    #

    logger.info(m18n.n("backup_mount_archive_for_restore"))
    restore_manager.extract()
    restore_manager.restore()

    # Check if something has been restored

    if restore_manager.success:
        logger.success(m18n.n("restore_complete"))
    else:
        raise YunohostError("restore_nothings_done")

    return restore_manager.targets.results


def backup_list(name=None, repositories=[], prefix="", with_info=False, human_readable=False):
    """
    List available local backup archives

    Keyword arguments:
        repos -- Repositories from which list archives
        with_info -- Show backup information for each archive
        human_readable -- Print sizes in human readable format

    """
    if not repositories:
        repositories = settings_get("misc.backup.backup_default_repositories")[0]

    if name:
        repo = BackupRepository(repositories[0])
        archive = BackupArchive(repo, name)
        return archive.list(with_info)

    return {
        name: BackupRepository(name).list_archives(with_info=with_info, prefix=prefix)

        for name in repositories or BackupRepository.list(full=False)
    }


def backup_download(name, repository=None):

    if not repository:
        repository = settings_get("misc.backup.backup_default_repositories")[0]
    repo = BackupRepository(repository)
    archive = BackupArchive(repo, name)

    return archive.download()


def backup_extract(name, paths, repository=None, target=None, exclude=[]):

    if not repository:
        repository = settings_get("misc.backup.backup_default_repositories")[0]

    if not target:
        target = os.getcwd()

    repo = BackupRepository(repository)
    archive = BackupArchive(repo, name)

    archive.extract(path=paths, target=target, exclude_paths=exclude)


def backup_mount(name, path, repository=None):

    if not repository:
        repository = settings_get("misc.backup.backup_default_repositories")[0]
    repo = BackupRepository(repository)
    archive = BackupArchive(repo, name)

    archive.mount(path)


def backup_info(name, repository=None, with_details=False, human_readable=False):
    """
    Get info about a local backup archive

    Keyword arguments:
        name -- Name of the local backup archive
        with_details -- Show additional backup information
        human_readable -- Print sizes in human readable format

    """
    if not repository:
        repository = settings_get("misc.backup.backup_default_repositories")[0]
    repo = BackupRepository(repository)
    archive = BackupArchive(repo, name)

    return archive.info(with_details=with_details, human_readable=human_readable)


def backup_delete(name, repository=None):
    """
    Delete a backup

    Keyword arguments:
        name -- Name of the local backup archive

    """
    if not repository:
        repository = settings_get("misc.backup.backup_default_repositories")[0]
    for _name in name:
        repo = BackupRepository(repository)
        BackupArchive(repo, _name).delete()

    logger.success(m18n.n("backup_deleted"))


#
# Repository subcategory
#


def backup_repository_list(space_used=False, full=False):
    """
    List available repositories where put archives
    """

    return {"repositories": BackupRepository.list(space_used, full)}


def backup_repository_info(shortname, space_used=False):
    return BackupRepository(shortname).info(space_used)


@is_unit_operation()
def backup_repository_add(operation_logger, shortname, name=None, location=None,
                          method=None, quota=None, passphrase=None,
                          alert=None, alert_delay=None, creation=True):
    """
    Add a backup repository
    """
    args = {k: v for k, v in locals().items() if v is not None}
    repository = BackupRepository(shortname, creation=True)

    return repository.set(
        operation_logger=args.pop('operation_logger'),
        args=urllib.parse.urlencode(args, doseq=True)
    )


@is_unit_operation()
def backup_repository_update(operation_logger, shortname, name=None,
                             quota=None, passphrase=None,
                             alert=None, alert_delay=None):
    """
    Update a backup repository
    """

    backup_repository_add(creation=False, **locals())


@is_unit_operation()
def backup_repository_remove(operation_logger, shortname, purge=False):
    """
    Remove a backup repository
    """
    BackupRepository(shortname).remove(purge)


@is_unit_operation()
def backup_repository_prune(operation_logger, shortname, prefix=None, keep_hourly=None, keep_daily=None, keep_weekly=None, keep_monthly=None, keep_last=None, keep_within=None):
    """
    Remove a backup repository
    """

    BackupRepository(shortname).prune(
        prefix=prefix,
        keep_hourly=keep_hourly,
        keep_daily=keep_daily,
        keep_weekly=keep_weekly,
        keep_monthly=keep_monthly,
        keep_last=keep_last,
        keep_within=keep_within,
    )


#
# Timer subcategory
#

class BackupTimer(ConfigPanel):
    """
    BackupRepository manage all repository the admin added to the instance
    """
    entity_type = "backup_timer"
    timer_name_tpl = "backup_{entity}"
    save_path_tpl = "/etc/yunohost/backup/timer/{entity}.yml"
    timer_path_tpl = "/etc/systemd/system/{timer_name}.timer"
    service_path_tpl = "/etc/systemd/system/{timer_name}.service"
    save_mode = "full"

    # TODO prefill apps and system question with good values
    # TODO validate calendar entry

    @classmethod
    def list(cls, full=False):
        """
        List backup timer
        """
        timers = super().list()

        if not full:
            return timers

        full_timers = {}
        for timer in timers:
            try:
                full_timers[timer] = BackupTimer(timer).info()
            except Exception as e:
                logger.error(f"Unable to open timer {timer}: {e}")

        return full_timers

    @property
    def timer_name(self):
        return self.timer_name_tpl.format(entity=self.entity)

    @property
    def service_path(self):
        return self.service_path_tpl.format(timer_name=self.timer_name)

    @property
    def timer_path(self):
        return self.timer_path_tpl.format(timer_name=self.timer_name)

    def _reload_systemd(self):
        try:
            check_output("systemctl daemon-reload")
        except Exception as e:
            logger.warning(f"Failed to reload daemon : {e}")

    def _run_service_command(self, action, *args):
        # TODO improve services to support timers
        # See https://github.com/YunoHost/issues/issues/1519
        try:
            check_output(f"systemctl {action} {self.timer_name}.timer")
        except Exception as e:
            logger.warning(f"Failed to {action} {self.timer_name}.timer : {e}")

    def _load_current_values(self):
        super()._load_current_values()

        # Search OnCalendar schedule property
        if os.path.exists(self.timer_path) and os.path.isfile(self.timer_path):
            with open(self.timer_path, 'r') as f:
                for index, line in enumerate(f):
                    if line.startswith("OnCalendar="):
                        self.values["schedule"] = line[11:].strip()
                        break
                else:
                    logger.debug(f"No OnCalendar property found in {self.timer_path}")

    def _apply(self):

        super()._apply()

        # TODO Add RandomizedDelaySec for daily and other special event
        write_to_file(self.timer_path, f"""[Unit]
Description=Run backup {self.entity} regularly

[Timer]
OnCalendar={self.values['schedule']}

[Install]
WantedBy=timers.target
""")
        write_to_file(self.service_path, f"""[Unit]
Description=Run backup {self.entity}
After=network.target

[Service]
Type=oneshot
ExecStart=/usr/bin/yunohost backup timer run '{self.entity}'
User=root
Group=root
""")
        self._reload_systemd()
        self._run_service_command("reset-failed")
        self.start()

    def info(self):
        return self.get(mode="export")

    def remove(self):

        self.stop()
        rm(self.save_path, force=True)
        rm(self.service_path, force=True)
        rm(self.timer_path, force=True)
        self._reload_systemd()
        self._run_service_command("reset-failed")
        logger.success(m18n.n("backup_timer_removed", timer=self.entity))

    def start(self):
        self._run_service_command("enable")
        self._run_service_command("start")

    def stop(self):
        self._run_service_command("stop")
        self._run_service_command("disable")

    def run(self):
        self._load_current_values()
        backup_create(
            prefix=f"{self.entity}_",
            description=self.name,
            repositories=self.repositories,
            system=self.system,
            apps=self.apps
        )
        for repository in self.repositories:
            backup_repository_prune(
                shortname=repository,
                prefix=f"{self.entity}_",
                keep_hourly=self.keep_hourly,
                keep_daily=self.keep_daily,
                keep_weekly=self.keep_weekly,
                keep_monthly=self.keep_monthly,
            )


def backup_timer_list(full=False):
    """
    List all backup timer
    """
    return {"backup_timer": BackupTimer.list(full)}


def backup_timer_info(shortname):
    return BackupTimer(shortname).info()


@is_unit_operation()
def backup_timer_create(
    operation_logger,
    shortname=None,
    name=None,
    repositories=[],
    system=[],
    apps=[],
    schedule=None,
    alert=[],
    keep_hourly=None,
    keep_daily=None,
    keep_weekly=None,
    keep_monthly=None,
    creation=True,
):
    """
    Add a backup timer
    """
    if not repositories:
        repositories = settings_get("misc.backup.backup_default_repositories")
    args = {k: v for k, v in locals().items() if v is not None}
    timer = BackupTimer(shortname, creation=True)
    return timer.set(
        operation_logger=args.pop('operation_logger'),
        args=urllib.parse.urlencode(args, doseq=True)
    )


@is_unit_operation()
def backup_timer_update(operation_logger, shortname, name=None,
                        quota=None, passphrase=None,
                        alert=None, alert_delay=None):
    """
    Update a backup timer
    """

    backup_timer_create(creation=False, **locals())


@is_unit_operation()
def backup_timer_remove(operation_logger, shortname):
    """
    Remove a backup timer
    """
    BackupTimer(shortname).remove()


@is_unit_operation()
def backup_timer_start(operation_logger, shortname, now=False):
    """
    Start a backup timer
    """
    if now:
        BackupTimer(shortname).run()

    BackupTimer(shortname).start()


@is_unit_operation()
def backup_timer_pause(operation_logger, shortname):
    """
    Pause a backup timer
    """
    BackupTimer(shortname).stop()


#
# Misc helpers                                                              #
#


def _call_for_each_path(self, callback, csv_path=None):
    """Call a callback for each path in csv"""

    if csv_path is None:
        csv_path = self.csv_path
    with open(csv_path, "r") as backup_file:
        backup_csv = csv.DictReader(backup_file, fieldnames=["source", "dest"])

        for row in backup_csv:
            callback(self, row["source"], row["dest"])


def _recursive_umount(directory):
    """
    Recursively umount sub directories of a directory

    Args:
        directory -- a directory path
    """
    mount_lines = check_output("mount").split("\n")

    points_to_umount = [
        line.split(" ")[2]

        for line in mount_lines

        if len(line) >= 3 and line.split(" ")[2].startswith(os.path.realpath(directory))
    ]

    everything_went_fine = True

    for point in reversed(points_to_umount):
        ret = subprocess.call(["umount", point])

        if ret != 0:
            everything_went_fine = False
            logger.warning(m18n.n("backup_cleaning_failed", point))

            continue

    return everything_went_fine
