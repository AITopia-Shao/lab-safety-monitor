"""
下位机联调工具：列出串口、实时打印遥测、手动发送断电 / 语音指令。
    python tools/serial_probe.py                 # 自动识别端口，打印遥测
    python tools/serial_probe.py --port /dev/ttyACM0
    python tools/serial_probe.py --cut           # 连接后发送一次断电指令，检查 ACK
    python tools/serial_probe.py --voice 1       # 播放 TF 卡 /mp3/0001.mp3
"""
import os
import sys
import time
import argparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [ROOT, os.path.join(ROOT, "LabSafetyMonitor")]

from serial.tools import list_ports
from serial_comm import SerialComm, KNOWN_VIDS, CUT_REASONS


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=None)
    ap.add_argument("--cut", action="store_true")
    ap.add_argument("--voice", type=int, default=0)
    ap.add_argument("--level", type=int, default=0, help="心跳携带的告警等级 0/1/2，用于测试三色灯")
    args = ap.parse_args()

    print("可用串口：")
    for p in list_ports.comports():
        tag = KNOWN_VIDS.get(p.vid, "")
        print(f"  {p.device:20s} VID={p.vid and hex(p.vid)} {p.description} {tag}")

    link = SerialComm(port=args.port or "/dev/ttyUSB0")
    if args.port:
        link.port_setting = args.port
    link.add_listener(lambda kind, info: kind != "telemetry" and print(f"  <事件> {kind}: {info}"))
    link.connect()
    link.set_alert_level(args.level)
    print("等待下位机数据（Ctrl+C 退出）...")

    sent = False
    try:
        while True:
            time.sleep(1.0)
            if not link.is_alive:
                print(" ", link.status_text())
                continue
            d = link.get_sensor_data()
            print(f"  环境 {d['temp']}℃ {d['humi']}% | 热源 {d['obj_temp']}℃ | 烟雾 {d['smoke']} | "
                  f"继电器 {'闭合' if d['relay_on'] else '断开'} | 锁定 {d['latched']} | "
                  f"原因 {CUT_REASONS.get(d['cut_reason'])} | 心跳 {'正常' if d['pc_link_ok'] else '丢失'}")
            if not sent:
                sent = True
                if args.voice:
                    print("  -> 播放语音", args.voice, link.play_voice(args.voice))
                if args.cut:
                    print("  -> 发送断电指令", link.send_power_off())
            if args.cut:
                print("     断电指令状态:", link.power_off_state)
    except KeyboardInterrupt:
        pass
    finally:
        link.disconnect()


if __name__ == "__main__":
    main()
