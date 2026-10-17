"""Future container entry; no dotenv, SDK discovery or secret values in argv."""

import os
import re

import uvicorn


def main():
    port = os.environ.get("PORT", "8080")
    if not re.fullmatch(r"[0-9]{1,5}", port) or not 1 <= int(port) <= 65535:
        raise SystemExit("Invalid PORT")
    uvicorn.run("line_assistant.cloud_run:app_factory", factory=True, host="0.0.0.0", port=int(port),
                workers=1, access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
