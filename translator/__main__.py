"""支持通过 ``python -m translator`` 运行。"""

import sys

if __name__ == "__main__":
    from translator.cli import main

    sys.exit(main())
