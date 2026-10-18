"""Terminal kiosk - fallback when the touch display is not available (SSH or a USB keyboard)."""

from __future__ import annotations

import getpass
import time


def run_terminal_kiosk(launcher):
    print("\n=== SmartPark (terminal kiosk) ===")
    while launcher.locked:
        ok, msg = launcher.unlock(getpass.getpass("Admin password: "))
        print(msg)
        if not ok and "locked" in msg.lower():
            time.sleep(5)
    gw = launcher.gateway
    while True:
        st = gw.kiosk_state()
        bays = "  ".join(f"{b['label']}:{b['status']}" for b in st["bays"])
        print(f"\n{st['free']}/{st['total']} free | {bays}")
        choice = input("[1] Unlock my bay  [2] New user setup  [3] Refresh  [q] Quit > ").strip().lower()
        if choice == "1":
            r = gw.request_access(input("Username: "))
            if r["status"] == "full":
                print("Car park full.")
                continue
            print("If the username is registered, a code was sent to Telegram.")
            while True:
                code = input("Code from Telegram: ").strip()
                pin = getpass.getpass("PIN (hidden): ").strip()
                v = gw.verify_access(r["request_id"], code, pin)
                if v["ok"]:
                    print(f"ACCESS GRANTED - proceed to {v['bay_label']} (permit until {v['until']})")
                    break
                if v.get("locked") or v.get("expired") or v.get("attempts_left", 0) <= 0 or v.get("no_bay"):
                    print("ACCESS DENIED - start again.")
                    break
                print(f"Incorrect - {v['attempts_left']} attempt(s) left")
        elif choice == "2":
            u = input("Username: ")
            c = input("Enrolment code: ")
            p1 = getpass.getpass("Choose 6-digit PIN: ")
            if getpass.getpass("Repeat PIN: ") != p1:
                print("PINs do not match")
                continue
            print(gw.access.complete_setup(u, c, p1)[1])
        elif choice == "q":
            return
