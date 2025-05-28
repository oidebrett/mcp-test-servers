# Test-MCP-Servers 

## Install

create a virtual environment and install the requirements

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
python -m simple-streamablehttp.mcp_simple_streamablehttp --log-level DEBUG --port 8080

```

## Sync with python sdk

At present, you will need to manually copy the sdk code to this directory.
from https://github.com/modelcontextprotocol/python-sdk/tree/main/examples/servers

## Test

Deploy to render.com