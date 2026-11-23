#!/usr/bin/env python3

import sys
from argparse import ArgumentParser
from vyos.remote import download

parser = ArgumentParser()
parser.add_argument('--local-file', help='local file', required=True)
parser.add_argument('--remote-path', help='remote path', required=True)
parser.add_argument('--resume', action='store_true',
                    help='keep a partial download and resume it on the next run')

args = parser.parse_args()

try:
    download(args.local_file, args.remote_path,
             check_space=True, raise_error=True, resume=args.resume)
except Exception as e:
    print(e)
    sys.exit(1)

sys.exit()
