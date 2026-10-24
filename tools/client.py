"""簡易 TCP 客戶端，用來手動對 DUT 模擬器下指令（取代 Windows 沒有的 nc）。

互動模式：   python tools/client.py
一次多個指令：python tools/client.py HELLO GET_FW GET_VOLT
"""

import argparse
import socket


class LineClient:
    """以「行」為單位收發的 TCP 連線。

    不用 sock.makefile()：它在讀取逾時一次後就無法再讀，
    但產測程式遇到 timeout 後通常還要 retry，所以自己管理接收緩衝區。
    """

    def __init__(self, host: str, port: int, timeout: float):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self.buf = b""

    def query(self, cmd: str) -> str:
        self.buf = b""  # 丟掉上一個指令遲到的殘留回應
        self.sock.sendall((cmd + "\n").encode("ascii"))
        while b"\n" not in self.buf:
            try:
                chunk = self.sock.recv(1024)
            except socket.timeout:
                return "<TIMEOUT>"
            if not chunk:
                return "<DISCONNECTED>"
            self.buf += chunk
        line, _, self.buf = self.buf.partition(b"\n")
        return line.decode("ascii", errors="replace").strip()

    def close(self):
        self.sock.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("commands", nargs="*", help="要依序送出的指令；不給就進入互動模式")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5000)
    p.add_argument("--timeout", type=float, default=2.0, help="等回應的秒數")
    a = p.parse_args()

    cli = LineClient(a.host, a.port, a.timeout)
    print(f"Connected to {a.host}:{a.port}")
    try:
        if a.commands:
            for cmd in a.commands:
                print(f"> {cmd}\n{cli.query(cmd)}")
            return

        print("輸入指令後按 Enter，輸入 quit 離開")
        while True:
            try:
                cmd = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if cmd.lower() in ("quit", "exit"):
                break
            if cmd:
                print(cli.query(cmd))
    finally:
        cli.close()


if __name__ == "__main__":
    main()
