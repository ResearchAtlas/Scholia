"""Entry point of the packaged app. Until the desktop app exists, its only mode is the
self-test: `AAB Research --self-test --model <embedding model>`."""

import sys

from backend import self_test

if __name__ == "__main__":
    sys.exit(self_test.main(sys.argv[1:]))
