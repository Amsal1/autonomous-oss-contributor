"""Dev-only README demo renderer (``scripts/dev/make-demo.ps1``).

It turns the transcript the hermetic e2e capture recorded (``tests/e2e/test_demo_capture.py``: every Bot API
call the gatekeeper made, with its exact parameters) into static Telegram-like HTML pages, and checks that
every text and button on a page equals the captured payload. Nothing here imports ossbot: the pages are
built only from the transcript.
"""
