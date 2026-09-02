"""Keep repository test imports from contaminating the trusted runtime."""

import sys


sys.dont_write_bytecode = True
