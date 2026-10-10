"""Entry point of the packaged app: the desktop app, its self-test with
`Scholia --self-test --model <embedding model>`, or a material's reading in a child process
(`Scholia --read-material`, started by the app itself, backend/reading.py)."""

import sys

if __name__ == "__main__":
    if sys.argv[1:2] == ["--read-material"]:  # first: a reading never takes the data folder's lock or opens a window
        from backend import reading

        sys.exit(reading.main())
    if "--self-test" in sys.argv[1:]:
        from backend import self_test

        sys.exit(self_test.main(sys.argv[1:]))
    from backend import desktop

    sys.exit(desktop.main(sys.argv[1:]))
