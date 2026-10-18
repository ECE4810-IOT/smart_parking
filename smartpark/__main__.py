"""Command-line entry point:  python -m smartpark <command> [options]

  init-gateway   create the encrypted vault + gateway keys (run once on the gateway)
  init-bay       create a bay's RSA identity and trust the gateway key
  trust          trust another device's public key
  gateway        run the gateway (kiosk on the touch display, or --terminal)
  bay            run a bay node (add --record <label> to collect training data)
  simulate       run the whole system on one computer with mock hardware
  train          train/compare ML models on recorded windows
  benchmark      measure ML inference cost on this device
  synth          generate a SYNTHETIC dataset (pipeline testing only)
  bench-crypto   measure the cost of every cryptographic operation
  sensor-test    read the ultrasonic sensor and diagnose wiring problems (stop the bay service first)
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time


def main(argv=None):
    p = argparse.ArgumentParser(prog="smartpark", description="SmartPark secure smart parking system")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init-gateway")
    s.add_argument("--config", required=True)
    s.add_argument("--force", action="store_true")

    s = sub.add_parser("init-bay")
    s.add_argument("--config", required=True)
    s.add_argument("--gateway-key")

    s = sub.add_parser("trust")
    s.add_argument("--config", required=True)
    s.add_argument("--peer", required=True)
    s.add_argument("--key", required=True)

    s = sub.add_parser("gateway")
    s.add_argument("--config", required=True)
    s.add_argument("--terminal", action="store_true", help="terminal kiosk instead of the touch UI")

    s = sub.add_parser("bay")
    s.add_argument("--config", required=True)
    s.add_argument("--record", metavar="LABEL", help="save every event window with this label")

    s = sub.add_parser("simulate")
    s.add_argument("--bays", type=int, default=4)
    s.add_argument("--kiosk-port", type=int, default=8080)
    s.add_argument("--kiosk-host", default="127.0.0.1")
    s.add_argument("--data-dir")
    s.add_argument("--locked", action="store_true", help="start locked (log in on the kiosk page)")
    s.add_argument("--real-telegram", action="store_true",
                   help="use SMARTPARK_TELEGRAM_TOKEN / SMARTPARK_ADMIN_CHAT env vars")
    s.add_argument("--script", action="store_true", help="run the scripted demo then exit")

    s = sub.add_parser("train")
    s.add_argument("--data", required=True)
    s.add_argument("--out", default="models")
    s.add_argument("--allow-synthetic", action="store_true")

    s = sub.add_parser("benchmark")
    s.add_argument("--model")
    s.add_argument("--data")
    s.add_argument("--n", type=int, default=1000)

    s = sub.add_parser("synth")
    s.add_argument("--out", default="dataset/SYNTHETIC_windows.csv")
    s.add_argument("--per-class", type=int, default=120)

    sub.add_parser("bench-crypto")

    s = sub.add_parser("sensor-test")
    s.add_argument("--config", required=True)
    s.add_argument("--n", type=int, default=40)

    a = p.parse_args(argv)
    from .util import init_metrics, setup_logging

    if a.cmd == "simulate":
        setup_logging("sim", a.data_dir or "/tmp", a.verbose)
        from .sim import run_simulator
        return run_simulator(a)
    if a.cmd in ("train", "benchmark", "synth", "bench-crypto"):
        logging.basicConfig(level=logging.INFO, format="%(message)s")
        if a.cmd == "train":
            from .ml.train import train
            return train(a.data, a.out, a.allow_synthetic)
        if a.cmd == "benchmark":
            from .ml.benchmark import benchmark
            return benchmark(a.model, a.data, a.n)
        if a.cmd == "synth":
            from .ml.synthetic import write_dataset
            return write_dataset(a.out, a.per_class)
        from .security.bench import bench_crypto
        return bench_crypto()

    if a.cmd == "sensor-test":
        from .config import load_config
        from .hardware.diagnose import sensor_test
        return sensor_test(load_config(a.config, "bay"), a.n)

    from .config import load_config
    role = "gateway" if a.cmd in ("init-gateway", "gateway") else ("bay" if a.cmd in ("init-bay", "bay") else None)
    cfg = load_config(a.config, role)
    setup_logging(cfg["device_id"], cfg["data_dir"], a.verbose)
    init_metrics(cfg["data_dir"])

    from . import provision
    if a.cmd == "init-gateway":
        return provision.init_gateway_interactive(cfg, a.force)
    if a.cmd == "init-bay":
        return provision.init_bay(cfg, a.gateway_key)
    if a.cmd == "trust":
        return provision.trust_peer(cfg, a.peer, a.key)

    stop = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))

    if a.cmd == "bay":
        from .bay.app import BayNode
        if a.record:
            cfg["ml"]["record_label"] = a.record
        node = BayNode(cfg)
        node.start()
        _wait(stop)
        node.stop()
        from .hardware import cleanup_gpio
        cleanup_gpio()
        return

    if a.cmd == "gateway":
        from .gateway.launcher import GatewayLauncher
        launcher = GatewayLauncher(cfg)
        if a.terminal:
            from .kiosk.terminal import run_terminal_kiosk
            try:
                run_terminal_kiosk(launcher)
            finally:
                launcher.stop()
            return
        from .kiosk.server import KioskServer
        k = cfg["kiosk"]
        kiosk = KioskServer(launcher, k["host"], k["port"])
        kiosk.start()
        logging.getLogger("smartpark").info("Gateway LOCKED - log in on the kiosk (http://%s:%s)", k["host"], k["port"])
        _wait(stop)
        kiosk.stop()
        launcher.stop()
        from .hardware import cleanup_gpio
        cleanup_gpio()


def _wait(stop):
    try:
        while not stop["flag"]:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
    sys.exit(0)
