import fnmatch
import io
import json
import os
import yaml
import zipfile
from pathlib import Path
from typing import List
from aztk.spark import models
from aztk.utils import constants, file_utils, secure_utils
from aztk.error import InvalidCustomScriptError

ROOT_PATH = constants.ROOT_PATH

# Constants for node data
NODE_SCRIPT_FOLDER = "node_scripts"
CUSTOM_SCRIPT_FOLDER = "custom-scripts"
CUSTOM_SCRIPT_METADATA_FILE = "custom-scripts.yaml"
PLUGIN_FOLDER = "plugins"


class NodeData:
    """
    Class made to bundle data to be uploaded to the node as a zip
    """

    def __init__(self, cluster_config: models.ClusterConfiguration):
        self.zip_path = os.path.join(ROOT_PATH, "tmp/node-scripts.zip")
        self.cluster_config = cluster_config
        file_utils.ensure_dir(self.zip_path)
        self.zipf = zipfile.ZipFile(self.zip_path, "w", zipfile.ZIP_DEFLATED)

    def __enter__(self):
        self.add_core()
        return self

    def __exit__(self, type, value, traceback):
        self.done()

    def add_core(self):
        self._add_node_scripts()
        self._add_custom_scripts()
        self._add_plugins()
        self._add_spark_configuration()
        self._add_user_conf()
        self.add_file(os.path.join(constants.ROOT_PATH, 'aztk', 'utils', 'command_builder.py'), '', binary=False)
        return self

    def done(self):
        self.zipf.close()
        return self

    def add_file(self, file_path: str, zip_dir: str, binary: bool = True):
        if not file_path:
            return

        full_file_path = Path(file_path)
        if binary:
            self.zipf.write(file_path, os.path.join(zip_dir, full_file_path.name))
        else:
            with io.open(file_path, 'r') as f:
                self.zipf.writestr(os.path.join(zip_dir, full_file_path.name), f.read().replace('\r\n', '\n'))

    def add_files(self, file_paths: List[str], zip_dir, binary: bool = True):
        """
        Add a list of local files to the node data
        """
        for file in file_paths:
            self.add_file(file, zip_dir, binary)

    def add_dir(self, path: str, exclude: List[str] = []):
        """
            Zip all the files in the given directory into the zip file handler
        """
        for base, _, files in os.walk(path):
            relative_folder = os.path.relpath(base, path)
            for file in files:
                if self._includeFile(file, exclude):
                    with io.open(os.path.join(base, file), 'r') as f:
                        self.zipf.writestr(os.path.join(relative_folder, file), f.read().replace('\r\n', '\n'))

    def _add_custom_scripts(self):
        data = []
        for index, custom_script in enumerate(self.cluster_config.custom_scripts):
            new_file_name = str(index) + '_' + os.path.basename(custom_script.script)
            data.append(dict(script=new_file_name, runOn=str(custom_script.run_on)))
            try:
                with io.open(custom_script.script, 'r') as f:
                    self.zipf.writestr(
                        os.path.join(CUSTOM_SCRIPT_FOLDER, new_file_name),
                        f.read().replace('\r\n', '\n'))
            except FileNotFoundError:
                raise InvalidCustomScriptError("Custom script '{0}' doesn't exists.".format(custom_script.script))

        self.zipf.writestr(
            os.path.join(CUSTOM_SCRIPT_FOLDER, CUSTOM_SCRIPT_METADATA_FILE), yaml.dump(data, default_flow_style=False))

    def _add_spark_configuration(self):
        spark_configuration = self.cluster_config.spark_configuration
        if not spark_configuration:
            return
        self.add_files(
            [
                spark_configuration.spark_defaults_conf, spark_configuration.spark_env_sh,
                spark_configuration.core_site_xml
            ],
            'conf',
            binary=False)

        # add ssh keys for passwordless ssh
        self.zipf.writestr( 'id_rsa.pub', spark_configuration.ssh_key_pair['pub_key'])
        self.zipf.writestr( 'id_rsa', spark_configuration.ssh_key_pair['priv_key'])

        if spark_configuration.jars:
            for jar in spark_configuration.jars:
                self.add_file(jar, 'jars', binary=True)

    def _add_user_conf(self):
        user_conf = self.cluster_config.user_configuration
        if not user_conf:
            return
        encrypted_aes_session_key, cipher_aes_nonce, tag, ciphertext = secure_utils.encrypt_password(
            self.cluster_config.spark_configuration.ssh_key_pair['pub_key'], user_conf.password)
        user_conf = yaml.dump({
            'username': user_conf.username,
            'password': ciphertext,
            'ssh-key': user_conf.ssh_key,
            'aes_session_key': encrypted_aes_session_key,
            'cipher_aes_nonce': cipher_aes_nonce,
            'tag': tag,
            'cluster_id': self.cluster_config.cluster_id
        })
        self.zipf.writestr('user.yaml', user_conf)

    def _add_plugins(self):
        plugins = self.cluster_config.plugins
        data = []
        for plugin_conf in plugins:
            plugin = plugin_conf.plugin()
            definition = plugin.definition
            for file in definition.files:
                filePath = os.path.join(plugin.path, file)
                zipf = self.add_file(filePath, 'plugins/{0}'.format(plugin.name), binary=False)
            if definition.execute:
                data.append(
                    dict(
                        name=plugin.name,
                        execute='{0}/{1}'.format(plugin.name, definition.execute),
                        args=plugin.process_args(plugin_conf.args),
                        runOn=definition.run_on.value,
                    ))

        self.zipf.writestr(os.path.join('plugins', 'plugins-manifest.json'), json.dumps(data))
        return zipf

    def _add_node_scripts(self):
        self.add_dir(os.path.join(ROOT_PATH, NODE_SCRIPT_FOLDER), exclude=['*.pyc'])

    def _includeFile(self, filename: str, exclude: List[str] = []) -> bool:
        for pattern in exclude:
            if fnmatch.fnmatch(filename, pattern):
                return False

        return True