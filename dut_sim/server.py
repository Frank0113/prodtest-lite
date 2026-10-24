"""DUT 模擬器：一個 TCP server，每條連線代表一片 DUT。

協定（純文字、一行一個指令，以 \\n 結尾）：
    HELLO            -> OK READY          （握手，其他指令必須在它之後）
    GET_SN           -> OK <序號>
    GET_FW           -> OK <韌體版本>
    GET_VOLT         -> OK <電壓 V>
    GET_TEMP         -> OK <溫度 °C>
    SET_MAC <mac>    -> OK
    BYE              -> OK BYE（並斷線）
錯誤回應：ERR <代碼>，例如 ERR NOT_READY、ERR UNKNOWN_CMD、ERR BAD_ARG、ERR SEQ

執行：
    python -m dut_sim.server --product A --port 5000
"""

import argparse
import logging
import random
import re
import socketserver
import time
from dataclasses import dataclass

log = logging.getLogger("dut_sim")


# ---------- 產品規格（模擬兩條產品線） ----------
@dataclass
class ProductProfile:
    name: str
    sn_prefix: str
    fw: str
    volt_mean: float     # 電壓平均值
    volt_sigma: float    # 量測雜訊
    unit_sigma: float    # 片與片之間的製程差異
    temp_mean: float
    temp_sigma: float


PROFILES = {
    "A": ProductProfile("ProductA", "PA", "1.2.3", 3.30, 0.015, 0.010, 40.0, 3.0),
    "B": ProductProfile("ProductB", "PB", "2.0.1", 5.00, 0.030, 0.020, 45.0, 4.0),
}


# ---------- 故障注入設定 ----------
@dataclass
class FaultConfig:
    timeout: float = 0.0     # 不回應
    oos: float = 0.0         # out of spec：數值偏離規格
    malformed: float = 0.0   # 回應格式錯誤
    seq: float = 0.0         # 回 ERR SEQ（指令順序錯誤）

    def pick(self, rng: random.Random) -> str | None:
        """依機率抽一種故障；回傳 None 代表這次正常。"""
        r = rng.random()
        for name in ("timeout", "oos", "malformed", "seq"):
            p = getattr(self, name)
            if r < p:
                return name
            r -= p
        return None


MAC_RE = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$")
MALFORMED_SAMPLES = ["OK 3.3x0", "0K", "OK", "###", "OK  ", "ERR"]


class DutHandler(socketserver.StreamRequestHandler):
    """每一條 TCP 連線會建立一個 DutHandler，也就是一片新的 DUT。"""

    def setup(self):
        super().setup()
        srv = self.server  # type: DutServer
        self.rng = random.Random(srv.rng.random())  # 每片 DUT 有自己的亂數
        self.profile: ProductProfile = srv.profile
        self.faults: FaultConfig = srv.faults
        srv.unit_count += 1
        self.sn = f"{self.profile.sn_prefix}{time.strftime('%y%m%d')}{srv.unit_count:04d}"
        # 這片 DUT 的「真實」電壓：平均值 + 製程差異（之後算 Cpk 會用到）
        self.true_volt = self.rng.gauss(self.profile.volt_mean, self.profile.unit_sigma)
        self.ready = False
        self.mac = None
        log.info("[%s] connected from %s", self.sn, self.client_address)

    def handle(self):
        while True:
            raw = self.rfile.readline()
            if not raw:  # 對方斷線
                break
            line = raw.decode("ascii", errors="replace").strip()
            if not line:
                continue

            reply = self.process(line)
            if reply is None:  # 模擬 timeout：故意不回
                log.info("[%s] <- %-18s -> (no reply)", self.sn, line)
                continue

            log.info("[%s] <- %-18s -> %s", self.sn, line, reply)
            self.wfile.write((reply + "\n").encode("ascii"))
            if reply == "OK BYE":
                break

    def finish(self):
        log.info("[%s] disconnected", self.sn)
        super().finish()

    # ---------- 指令處理 ----------
    def process(self, line: str) -> str | None:
        parts = line.split()
        cmd, args = parts[0].upper(), parts[1:]

        if cmd == "HELLO":
            self.ready = True
            return "OK READY"
        if cmd == "BYE":
            return "OK BYE"
        if not self.ready:
            return "ERR NOT_READY"   # 真實的順序錯誤：沒握手就下指令

        handlers = {
            "GET_SN": self.cmd_get_sn,
            "GET_FW": self.cmd_get_fw,
            "GET_VOLT": self.cmd_get_volt,
            "GET_TEMP": self.cmd_get_temp,
            "SET_MAC": self.cmd_set_mac,
        }
        if cmd not in handlers:
            return "ERR UNKNOWN_CMD"

        fault = self.faults.pick(self.rng)
        if fault == "timeout":
            return None
        if fault == "malformed":
            return self.rng.choice(MALFORMED_SAMPLES)
        if fault == "seq":
            return "ERR SEQ"
        return handlers[cmd](args, oos=(fault == "oos"))

    def cmd_get_sn(self, args, oos=False):
        return f"OK {self.sn}"

    def cmd_get_fw(self, args, oos=False):
        return "OK 0.9.9" if oos else f"OK {self.profile.fw}"

    def cmd_get_volt(self, args, oos=False):
        v = self.rng.gauss(self.true_volt, self.profile.volt_sigma)
        if oos:
            v += self.rng.choice([-1, 1]) * self.profile.volt_mean * 0.1  # 偏 ±10%
        return f"OK {v:.4f}"

    def cmd_get_temp(self, args, oos=False):
        t = self.rng.gauss(self.profile.temp_mean, self.profile.temp_sigma)
        if oos:
            t += 40
        return f"OK {t:.2f}"

    def cmd_set_mac(self, args, oos=False):
        if len(args) != 1 or not MAC_RE.match(args[0].upper()):
            return "ERR BAD_ARG"
        self.mac = args[0].upper()
        return "OK"


class DutServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, addr, profile: ProductProfile, faults: FaultConfig, seed=None):
        super().__init__(addr, DutHandler)
        self.profile = profile
        self.faults = faults
        self.rng = random.Random(seed)
        self.unit_count = 0


def main():
    p = argparse.ArgumentParser(description="ProdTest-Lite DUT simulator")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5000)
    p.add_argument("--product", choices=PROFILES.keys(), default="A")
    p.add_argument("--seed", type=int, default=None, help="固定亂數種子，結果可重現")
    p.add_argument("--timeout-rate", type=float, default=0.0)
    p.add_argument("--oos-rate", type=float, default=0.0)
    p.add_argument("--malformed-rate", type=float, default=0.0)
    p.add_argument("--seq-rate", type=float, default=0.0)
    a = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    faults = FaultConfig(a.timeout_rate, a.oos_rate, a.malformed_rate, a.seq_rate)
    profile = PROFILES[a.product]

    with DutServer((a.host, a.port), profile, faults, a.seed) as srv:
        log.info("DUT simulator [%s] listening on %s:%d  faults=%s",
                 profile.name, a.host, a.port, faults)
        log.info("Press Ctrl+C to stop")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            log.info("Stopped")


if __name__ == "__main__":
    main()
