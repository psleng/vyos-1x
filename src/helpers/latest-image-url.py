#!/usr/bin/env python3

import sys

from vyos.configquery import ConfigTreeQuery
from vyos.version import get_remote_version
from vyos.version import get_version_data


if __name__ == '__main__':
    image_path = ''
    image_version = ''

    config = ConfigTreeQuery()
    if config.exists('system update-check url'):
        configured_url_version = config.value('system update-check url')
    else:
        # Fall back to the default update URL baked into the image at build
        # time (vyos-build writes it into /usr/share/vyos/version.json from the
        # build-flavor TOML). Empty string when no default was baked in.
        configured_url_version = get_version_data().get('update_url', '')

    if configured_url_version:
        remote_url_list = get_remote_version(configured_url_version)
        if remote_url_list:
            image_path = remote_url_list[0].get('url')
            image_version = remote_url_list[0].get('version') or ''
        else:
            sys.exit(1)

    # Line 1: image URL; line 2: its version, so the installer can warn
    # before downloading an image that is not newer than the running one
    print(image_path)
    print(image_version)
