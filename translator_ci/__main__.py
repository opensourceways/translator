"""支持通过 ``python -m translator_ci`` 运行。"""

import sys

if __name__ == "__main__":
    from translator_ci.cli import main

    sys.exit(main())
