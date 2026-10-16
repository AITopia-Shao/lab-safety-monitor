"""网页和配置回归测试；不启动摄像头、串口、Qt 或大模型。"""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient
from src import config
from src.core.jepa_link import JepaLink
from src.web import server


class IsolatedConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="labsafety_web_")
        self.addCleanup(self.tmp.cleanup)
        self.override = Path(self.tmp.name) / "override.yaml"
        for name, value in (
            ("_config", copy.deepcopy(config.DEFAULTS)),
            ("_config_path", None),
            ("_candidate_paths", lambda: []),
            ("_override_path", lambda: str(self.override)),
        ):
            p = patch.object(config, name, value)
            p.start()
            self.addCleanup(p.stop)
        config.set_value("web.token", "MixedCase123", persist=False)


class ConfigRegressionTests(IsolatedConfig):
    def test_non_mapping_override_is_ignored(self):
        for text in ("- wrong\n- shape\n", "42\n"):
            with self.subTest(text=text):
                self.override.write_text(text, encoding="utf-8")
                self.assertEqual(config.load(force=True)["rules"]["dry_burn_c"], 120)
                config.set_value("rules.dry_burn_c", 125)
                self.assertEqual(config._read_overrides()["rules"]["dry_burn_c"], 125)

    def test_concurrent_changes_survive_reload(self):
        # 第一项读完旧文件后暂停；第二项同时保存也不能覆盖掉第一项。
        first_read, second_read, release = (threading.Event() for _ in range(3))
        read = config._read_overrides

        def delayed_read():
            result = read()
            if threading.current_thread().name == "first":
                first_read.set()
                release.wait(2)
            else:
                second_read.set()
            return result

        errors = []

        def save(key, value):
            try:
                config.set_value(key, value)
            except Exception as exc:
                errors.append(exc)

        with patch.object(config, "_read_overrides", delayed_read):
            first = threading.Thread(target=save, args=("env.t_set_c", 25), name="first")
            second = threading.Thread(target=save, args=("env.h_low", 35), name="second")
            first.start()
            self.assertTrue(first_read.wait(1))
            second.start()
            second_read.wait(0.1)
            release.set()
            first.join(3)
            second.join(3)
            self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(errors, [])
        saved = config.load(force=True)["env"]
        self.assertEqual((saved["t_set_c"], saved["h_low"]), (25, 35))

    def test_failed_save_preserves_last_valid_file(self):
        config.set_value("env.t_set_c", 25)
        before = self.override.read_bytes()
        with patch("yaml.safe_dump", side_effect=OSError("模拟写入失败")):
            config.set_value("env.h_low", 35)
        self.assertEqual(self.override.read_bytes(), before)
        self.assertEqual(config.cfg("env.h_low"), 35)


class WebRegressionTests(IsolatedConfig):
    def setUp(self):
        super().setUp()
        self.engine = Mock()
        self.engine.extensions = {"env": Mock(), "agent": Mock(), "jepa": JepaLink()}
        self.engine.extensions["env"].set_manual.return_value = "已更新"
        self.engine.extensions["env"].status.return_value = {}
        self.engine.extensions["agent"].configure.return_value = {"ok": True}
        self.engine.latest_jpeg.return_value = b"jpeg"
        self.engine.dual_system.is_paused = False
        self.engine.media_label = "摄像头"
        self.client = TestClient(server.create_app(self.engine), raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        self.headers = {"X-Token": "MixedCase123"}
        events_patch = patch.object(server, "events")
        self.events = events_patch.start()
        self.events.get_event.return_value = {"uid": "existing"}
        self.addCleanup(events_patch.stop)

    def post(self, path, data):
        return self.client.post(path, headers=self.headers, json=data)

    def test_invalid_manual_actuation_does_not_change_outputs(self):
        for data in ({"fan_pwm": "bad"}, {"fan_pwm": 1.5}, {"fan_pwm": 101},
                     {"fan_pwm": True}, {"fan_pwm": 30, "humid": "false"}):
            with self.subTest(data=data):
                self.assertEqual(self.post("/api/env/manual", data).status_code, 422)
        self.engine.extensions["env"].set_manual.assert_not_called()

    def test_manual_and_auto_control_remain_available(self):
        self.assertEqual(self.post("/api/env/manual", {"fan_pwm": 30, "humid": False}).status_code, 200)
        self.engine.extensions["env"].set_manual.assert_called_with(30, False)
        self.assertEqual(self.post("/api/env/manual", {"fan_pwm": None}).status_code, 200)
        self.engine.extensions["env"].set_manual.assert_called_with(None, False)

    def test_nonfinite_jepa_score_does_not_poison_json_snapshot(self):
        for value in ("NaN", "Infinity", "-Infinity", True):
            with self.subTest(value=value):
                self.assertEqual(self.post("/api/jepa/score", {"score": value}).status_code, 400)
        self.assertEqual(self.engine.extensions["jepa"].status()["history"], [])
        self.assertEqual(self.post("/api/jepa/score", {"score": -0.4}).status_code, 200)
        json.dumps(self.engine.extensions["jepa"].status(), allow_nan=False)

    def test_invalid_mute_returns_validation_error(self):
        for value in ("bad", "NaN", "Infinity", -1, True):
            with self.subTest(value=value):
                self.assertEqual(self.post("/api/mute", {"seconds": value}).status_code, 422)
        self.engine.mute.assert_not_called()

    def test_integer_threshold_is_not_silently_truncated(self):
        for value in (1800.5, "1800.5", True, "Infinity"):
            with self.subTest(value=value):
                response = self.client.patch("/api/config", headers=self.headers,
                                             json={"rules.smoke_raw": value})
                self.assertEqual(response.status_code, 422)
        self.assertEqual(config.cfg("rules.smoke_raw"), 1800)

    def test_review_requires_explicit_boolean(self):
        for data in ({}, {"correct": "false"}, {"correct": 1}):
            with self.subTest(data=data):
                self.assertEqual(self.post("/api/events/existing/review", data).status_code, 422)
        self.events.set_review.assert_not_called()
        self.assertEqual(self.post("/api/events/existing/review", {"correct": False}).status_code, 200)
        self.events.set_review.assert_called_once_with("existing", False)

    def test_agent_nested_config_requires_objects(self):
        for data in ({"remote": "bad"}, {"local": ["bad"]}):
            with self.subTest(data=data):
                self.assertEqual(self.post("/api/agent/config", data).status_code, 422)
        self.engine.extensions["agent"].configure.assert_not_called()

    def test_non_ascii_wrong_token_is_an_auth_error(self):
        response = self.client.get("/api/config", params={"token": "错误口令"})
        self.assertEqual(response.status_code, 401)

    def test_invalid_frame_name_is_not_another_camera(self):
        self.assertEqual(self.client.get("/api/frame/typo.jpg", headers=self.headers).status_code, 404)
        self.engine.latest_jpeg.assert_not_called()


class WebPortRegressionTests(unittest.TestCase):
    def test_last_port_busy_does_not_probe_outside_tcp_range(self):
        with patch.object(server, "_port_free", return_value=False) as free:
            self.assertIsNone(server._pick_port("127.0.0.1", 65535))
        free.assert_called_once_with("127.0.0.1", 65535)

    def test_invalid_configured_port_is_rejected_without_bind(self):
        with patch.object(server, "_port_free") as free:
            for port in (-1, 0, 65536):
                self.assertIsNone(server._pick_port("127.0.0.1", port))
        free.assert_not_called()


class BrowserRegressionTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "浏览器脚本回归需要 Node.js")
    def test_token_and_relogin_keep_single_live_connection(self):
        # 执行实际前端脚本，用最小 DOM 验证登录交互，无浏览器或网络依赖。
        script = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const els = new Map(), intervals = new Map(), timers = new Map(), sockets = [];
let next = 0;
function el(id) {
  if (!els.has(id)) els.set(id, {value:'', textContent:'', style:{}, dataset:{},
    classList:{add(){}, remove(){}, toggle(){}, contains(){return false;}}});
  return els.get(id);
}
class Socket {
  constructor(url) {this.url=url; this.closed=false; sockets.push(this);}
  close() {this.closed=true; if(this.onclose) this.onclose({code:1000});}
}
const context = {console, URLSearchParams, WebSocket:Socket,
  location:{search:'?token=MixedCase123&still=1', pathname:'/', hash:'', protocol:'http:', host:'localhost'},
  history:{replaceState(){}}, localStorage:{getItem(){return '';}, setItem(){}},
  document:{getElementById:el, querySelectorAll(){return [];}, querySelector(){return null;}, addEventListener(){}},
  setInterval(fn){const id=++next; intervals.set(id,fn); return id;}, clearInterval(id){intervals.delete(id);},
  setTimeout(fn){const id=++next; timers.set(id,fn); return id;}, clearTimeout(id){timers.delete(id);}
};
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),context);
assert(sockets[0].url.endsWith('token=MixedCase123'), 'URL 口令需要保留大小写');
const staleClose = sockets[0].onclose;
el('tokenInput').value='OtherCase456'; el('loginBtn').onclick();
assert.equal(sockets.filter(s=>!s.closed).length,1, '重新登录后只能保留一个连接');
assert(sockets.at(-1).url.endsWith('token=OtherCase456'), '手动输入的口令需要保留大小写');
assert.equal(intervals.size,1,'重新登录后只能保留一个画面轮询');
staleClose({code:1006});
assert.equal(timers.size,0,'旧连接断开不能再触发重连');
sockets.at(-1).onclose({code:1006});
assert.equal(timers.size,1,'当前连接断开应安排一次重连');
el('tokenInput').value='ThirdCase789'; el('loginBtn').onclick();
assert.equal(timers.size,0,'重新登录应取消旧重连');
assert.equal(intervals.size,1);
"""
        result = subprocess.run([shutil.which("node"), "-e", script,
                                 str(ROOT / "src/web/static/app.js")], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
