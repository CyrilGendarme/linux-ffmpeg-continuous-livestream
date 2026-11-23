#!/usr/bin/env python3
"""Upload local tracks that are not in the R2 bucket yet (see livestream/r2_push.py)."""

from livestream.r2_push import main


if __name__ == "__main__":
    raise SystemExit(main())
