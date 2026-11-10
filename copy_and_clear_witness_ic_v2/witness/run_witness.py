"""Primary command: ``python run_witness.py``.

Generates the primary scenario, executes the registered institutions through
the common kernel, validates everything, writes ``outputs/``, and exits 0 only
when the complete requested witness passes.
"""

import sys

from witness.app import main

if __name__ == "__main__":
    sys.exit(main())
