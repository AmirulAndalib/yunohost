
class HookBackupRepository(BackupRepository):
    method_name = "hook"

    # =================================================
    # Repository actions
    # =================================================
    def install(self):
        raise NotImplementedError()

    def update(self):
        raise NotImplementedError()

    def remove(self, purge=False):
        if self.__class__ == BackupRepository:
            raise NotImplementedError() # purge

        rm(self.save_path, force=True)
        logger.success(m18n.n("repository_removed", repository=self.shortname))

    def list(self):
        raise NotImplementedError()

    def info(self, space_used=False):
        result = super().get(mode="export")

        if self.__class__ == BackupRepository and space_used == True:
            raise NotImplementedError() # purge

        return {self.shortname: result}

    def prune(self):
        raise NotImplementedError()


class HookBackupArchive(BackupArchive):
    # =================================================
    # Archive actions
    # =================================================
    def backup(self):
        raise NotImplementedError()
        """
        Launch a custom script to backup
        """

        self._call('backup', self.work_dir, self.name, self.repo.location, self.manager.size,
                self.manager.description)

    def restore(self):
        raise NotImplementedError()

    def delete(self):
        raise NotImplementedError()

    def list(self):
        raise NotImplementedError()
        """ Return a list of archives names

        Exceptions:
        backup_custom_list_error -- Raised if the custom script failed
        """
        out = self._call('list', self.repo.location)
        result = out.strip().splitlines()
        return result

    def info(self):
        raise NotImplementedError() #compute_space_used
        """ Return json string of the info.json file

        Exceptions:
        backup_custom_info_error -- Raised if the custom script failed
        """
        return self._call('info', self.name, self.repo.location)

    def download(self):
        raise NotImplementedError()

    def mount(self):
        raise NotImplementedError()
        """
        Launch a custom script to mount the custom archive
        """
        super().mount()
        self._call('mount', self.work_dir, self.name, self.repo.location, self.manager.size,
                self.manager.description)

    def extract(self):
        raise NotImplementedError()

    def need_organized_files(self):
        """Call the backup_method hook to know if we need to organize files"""
        if self._need_mount is not None:
            return self._need_mount

        try:
            self._call('nedd_mount')
        except YunohostError:
            return False
        return True
    def _call(self, *args):
        """ Call a submethod of backup method hook

        Exceptions:
        backup_custom_ACTION_error -- Raised if the custom script failed
        """
        ret = hook_callback("backup_method", [self.method],
                            args=args)

        ret_failed = [
            hook
            for hook, infos in ret.items()
            if any(result["state"] == "failed" for result in infos.values())
        ]
        if ret_failed:
            raise YunohostError("backup_custom_" + args[0] + "_error")

        return ret["succeed"][self.method]["stdreturn"]

    def _get_args(self, action):
        """Return the arguments to give to the custom script"""
        return [
            action,
            self.work_dir,
            self.name,
            self.repo,
            self.manager.size,
            self.manager.description,
        ]
