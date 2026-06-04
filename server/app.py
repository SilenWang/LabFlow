from http.server import ThreadingHTTPServer

from server.config import HOST, PORT
from server.db import init_db
from server.handler import LabFlowHandler


def main():
    init_db()
    server = ThreadingHTTPServer((HOST, PORT), LabFlowHandler)
    print(f"LabFlow 已启动: http://127.0.0.1:{PORT}")
    print("局域网电脑请访问: http://本机局域网IP:%s" % PORT)
    print("按 Ctrl+C 停止服务")
    server.serve_forever()
