import os
import sys


sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


if __name__ == "__main__":
    print(f"当前进程ID: {os.getpid()}")
    from app.server.mcp_server import start_mcp_server

    start_mcp_server()
