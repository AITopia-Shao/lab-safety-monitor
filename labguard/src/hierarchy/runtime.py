"""运行时：视觉工作线程、独立温湿度源、母系统时钟与异步执行器。"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from .coordinator import BuildingCoordinator, Telemetry
from .vision import ModelPool, VisionWorker


def parse_telemetry(text):
    try:
        data = json.loads(text)
        temp = float(data.get('temp', data.get('temperature')))
        humi = float(data.get('humi', data.get('humidity', data.get('hum'))))
    except (ValueError, TypeError, AttributeError):
        values = re.findall(r'[-+]?\d+(?:\.\d+)?', text)
        if len(values) != 2:
            return None
        temp, humi = map(float, values)
    if not (math.isfinite(temp) and math.isfinite(humi) and -40 <= temp <= 150 and 0 <= humi <= 100):
        return None
    return temp, humi


class StateDirectoryLock:
    """同一运行目录只允许一个母系统，进程崩溃时由操作系统释放锁。"""
    def __init__(self, folder):
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        self.handle = (folder / 'runtime.lock').open('a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                self.handle.seek(0, 2)
                if self.handle.tell() == 0:
                    self.handle.write(b'0')
                    self.handle.flush()
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.handle.seek(0)
            self.handle.truncate()
            self.handle.write(str(os.getpid()).encode('ascii'))
            self.handle.flush()
        except OSError:
            self.handle.close()
            raise RuntimeError('已有母系统正在使用这个运行目录，请切换到已打开的窗口或使用不同的 --hierarchy-state-dir')

    def close(self):
        self.handle.close()


class HierarchyRuntime:
    def __init__(self, config, state_dir, event_sink=None):
        self.config = deepcopy(config)
        self.sim_lock = threading.Lock()
        self.simulation = {}
        self.sensor_threads = []
        self.stopping = threading.Event()
        self.state_lock = StateDirectoryLock(state_dir)
        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix='actuator')
        try:
            self.coordinator = BuildingCoordinator(config, self.dispatch, state_dir, event_sink=event_sink)
        except Exception:
            self.executor.shutdown(wait=False)
            self.state_lock.close()
            raise
        self.pool = ModelPool(os.environ.get('INFERENCE_DEVICE', 'cpu'))
        self.workers = []
        for lab in config['labs']:
            self.simulation[lab['macro']['id']] = dict(occupied=True, fire=False)
            self.workers.append(VisionWorker(lab['macro'], 'macro', self.pool,
                                             self.coordinator.ingest, self.get_simulation))
            for bench in lab['benches']:
                self.simulation[bench['camera']['id']] = dict(heating=False, fire=False)
                self.simulation[bench['id']] = dict(temperature=bench['telemetry']['temperature'],
                                                    humidity=bench['telemetry']['humidity'])
                self.workers.append(VisionWorker(bench['camera'], 'micro', self.pool,
                                                 self.coordinator.ingest, self.get_simulation))
                self.sensor_threads.append(threading.Thread(target=self.sensor_loop, args=(lab['id'], bench), daemon=True))
        self.clock_thread = threading.Thread(target=self.clock_loop, name='building-coordinator', daemon=True)
        self.error = ''
        self.started = False

    def get_simulation(self, node_id):
        with self.sim_lock:
            return dict(self.simulation[node_id])

    def set_simulation(self, node_id, **values):
        with self.sim_lock:
            self.simulation[node_id].update(values)

    def start(self):
        if self.started:
            return
        self.started = True
        for worker in [*self.workers, *self.sensor_threads, self.clock_thread]:
            worker.start()

    def clock_loop(self):
        while not self.stopping.wait(0.2):
            try:
                self.coordinator.tick()
                self.error = ''
            except Exception as exc:
                self.error = f'母系统处理异常：{type(exc).__name__}: {exc}'

    def sensor_loop(self, lab_id, bench):
        cfg, connection = bench['telemetry'], None
        ipc = Path(tempfile.gettempdir()) / 'lab_safety_env_telemetry.json'
        try:
            while not self.stopping.is_set():
                try:
                    if cfg['kind'] == 'serial':
                        import serial
                        if connection is None:
                            connection = serial.Serial(cfg['port'], int(cfg['baudrate']), timeout=0.5)
                        result = parse_telemetry(connection.readline().decode('utf-8', errors='ignore'))
                        if result:
                            self.coordinator.update_telemetry(lab_id, bench['id'], Telemetry(time.time(), *result, False))
                    else:
                        values = self.get_simulation(bench['id'])
                        if cfg['kind'] == 'legacy_ipc' and ipc.exists():
                            try:
                                data = json.loads(ipc.read_text(encoding='utf-8'))
                                if 0 <= time.time() - data['timestamp'] < 2.5:
                                    parsed = parse_telemetry(json.dumps(data))
                                    if parsed:
                                        values = dict(temperature=parsed[0], humidity=parsed[1])
                            except (OSError, ValueError, KeyError, TypeError):
                                pass
                        self.coordinator.update_telemetry(lab_id, bench['id'],
                            Telemetry(time.time(), values['temperature'], values['humidity'], True))
                    self.stopping.wait(0.2)
                except Exception:
                    # 实体传感器断开后保留旧时间戳，过期即 UNKNOWN，不偷偷伪造读数。
                    if connection:
                        connection.close()
                        connection = None
                    self.stopping.wait(2)
        finally:
            if connection:
                connection.close()

    def dispatch(self, command, callback):
        def execute():
            lab = next(l for l in self.config['labs'] if l['id'] == command['target_lab'])
            actuator = lab['actuator']
            # 演练事件永远不能驱动物理串口，即使目标实验室配置了真实执行器。
            if command['origin'] == 'simulation' or actuator['mode'] == 'simulation':
                return 'SIMULATED', '模拟执行完成，未操作硬件'
            if command['action'] == 'alarm':
                return 'SOFTWARE_ALARM', '软件报警已激活；未配置外置声光报警协议'
            from LabSafetyMonitor.serial_comm import SerialComm
            link = SerialComm(port=actuator['port'], baudrate=int(actuator['baudrate']))
            # 每间实验室必须使用显式端口，不能沿用全局自动扫描到另一间实验室。
            link.port_setting = actuator['port']
            link.baudrate = int(actuator['baudrate'])
            try:
                if not link.connect() or not link.send_power_off(reason=4):
                    raise IOError('目标实验室下位机未连接')
                deadline = time.monotonic() + 2.0
                while link.power_off_state == 'pending' and time.monotonic() < deadline:
                    time.sleep(0.02)
                if link.power_off_state == 'acked':
                    return 'ACKNOWLEDGED', '下位机已确认断电指令；恢复供电需现场操作，未作独立电压测量'
                raise IOError('下位机未确认断电：' + link.power_off_state)
            finally:
                link.disconnect()

        future = self.executor.submit(execute)

        def completed(result):
            try:
                status, detail = result.result()
            except Exception as exc:
                status, detail = 'FAILED', f'{type(exc).__name__}：执行失败，硬件状态未知，请人工核查'
            callback(command['id'], status, detail)
        future.add_done_callback(completed)

    def stop(self):
        self.stopping.set()
        for worker in self.workers:
            worker.stop()
        deadline = time.monotonic() + 5
        if self.started:
            for worker in [*self.workers, *self.sensor_threads, self.clock_thread]:
                worker.join(timeout=max(0, deadline - time.monotonic()))
        self.executor.shutdown(wait=True, cancel_futures=False)
        self.state_lock.close()
        # 未及时退出的视觉线程是 daemon；GUI 不销毁任何工作线程中的 Qt 对象。
        alive = [w.name for w in [*self.workers, *self.sensor_threads, self.clock_thread] if w.is_alive()]
        if alive:
            raise RuntimeError('视频源仍在退出，请稍后重试配置：' + ', '.join(alive))
