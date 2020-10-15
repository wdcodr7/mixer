# GPLv3 License
#
# Copyright (C) 2020 Ubisoft
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""
This module defines utilities for local data.
"""

import os
import logging
import tempfile
from pathlib import Path
import hashlib

logger = logging.getLogger(__name__)


def get_data_directory():
    if "MIXER_DATA_DIR" in os.environ:
        data_path = Path(os.environ["MIXER_DATA_DIR"])
        if os.path.exists(data_path):
            return data_path
        logger.error(
            f"MIXER_DATA_DIR env var set to {data_path}, but directory does not exists. Falling back to default location."
        )
    return os.path.join(os.fspath(tempfile.gettempdir()), "mixer", "data")


def get_resolved_file_path(path: str):
    if os.path.exists(path):
        return path

    return get_cache_file_hash(path)


def get_or_create_cache_file(path: str, data: bytes):
    if os.path.exists(path):
        return path

    cache_path = get_cache_file_hash(path)
    if os.path.exists(cache_path):
        return cache_path

    create_cache_file(path, cache_path, data)
    return cache_path


def get_cache_file_hash(path: str):
    m = hashlib.sha1()
    m.update(path.encode())
    return get_data_directory() + "/images/" + str(m.hexdigest())


def create_cache_file(path: str, cache_path: str, data: bytes):
    os.makedirs(Path(cache_path).parent.absolute())

    with open(cache_path, "wb") as f:
        f.write(data)

    with open(cache_path + ".metadata", "w") as f:
        f.write(path)
