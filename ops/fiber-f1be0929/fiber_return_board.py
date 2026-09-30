"""Forced SSH command for the chiwk12 skuser01 controller's card reads only."""
import os
import re
import shlex
import sys

CLI = '/home/skuser01/.skenv/bin/skcapstone'


def command(original):
    """Parse the exact existing authority-read contract without using a shell."""
    args = shlex.split(original)
    if (len(args) != 8 or args[0] != 'env' or args[3:6] != [CLI, 'coord', 'show']
            or args[7] != '--json' or not re.fullmatch('[0-9a-f]{8}', args[6])):
        raise ValueError('read-only-card-command-required')
    owner = args[1].removeprefix('SKAGENT=')
    if (args[1] != 'SKAGENT=' + owner or args[2] != 'SKCAPSTONE_AGENT=' + owner
            or not re.fullmatch('[a-z][a-z0-9-]{0,95}', owner)):
        raise ValueError('worker-identity-mismatch')
    env = {'HOME': '/home/skuser01', 'PATH': '/home/skuser01/.skenv/bin:/usr/bin:/bin',
           'LANG': 'C.UTF-8', 'SKAGENT': owner, 'SKCAPSTONE_AGENT': owner}
    return args[3:], env


if __name__ == '__main__':
    try:
        argv, environment = command(os.environ.get('SSH_ORIGINAL_COMMAND', ''))
    except ValueError:
        print('Only exact native card reads are authorized.', file=sys.stderr)
        sys.exit(77)
    os.execve(CLI, argv, environment)
