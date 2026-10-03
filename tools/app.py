"""Entry point of the packaged app: the desktop app, or its self-test with
`Scholia --self-test --model <embedding model>`."""

import sys

if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        from backend import self_test

        sys.exit(self_test.main(sys.argv[1:]))
    from backend import desktop

    sys.exit(desktop.main(sys.argv[1:]))
