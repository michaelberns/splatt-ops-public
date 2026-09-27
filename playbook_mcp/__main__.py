"""Entry point for `python -m playbook_mcp`.

MCP clients launch this with no shell and no particular working
directory, so it is started as a module, the same way as mcp_server,
rather than by a file path.
"""

from .server import mcp

mcp.run()
