import argparse

import uvicorn

p = argparse.ArgumentParser(description="Run the CoreOne Teller mock")
p.add_argument("--port", type=int, default=8801)
a = p.parse_args()
uvicorn.run("mockbank.app:app", host="127.0.0.1", port=a.port, log_level="warning")
