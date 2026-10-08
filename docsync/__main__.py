"""支持通过 ``python -m docsync`` 运行。"""

import sys

if __name__ == "__main__":
    from docsync.cli import main

    sys.exit(main())
