"""FH6Auto 自动驾驶模块
送外卖 / 刷劲敌 / 线上挂机 三种自动驾驶模式
与主流水线（跑图/买车/抽奖/卖车/开抽）完全独立，互不耦合

入口: AutoDrivePanel(ctk.CTkFrame) — 嵌入主窗口的面板
"""

import time
import socket
import struct
import threading
import customtkinter as ctk


# ============================================================
# 遥测接收器（Forza Horizon Data Out — UDP 324 字节数据包）
# ============================================================

class TelemetryReceiver:
    """监听 Forza Horizon 6 UDP 数据输出，解析速度/比赛状态/手刹等"""

    def __init__(self, ip="127.0.0.1", port=1000):
        self.ip = ip
        self.port = port
        self.latest = {"is_race_on": False, "speed_kmh": 0.0, "handbrake": 0, "car_class": -1}
        self._running = False
        self._sock = None
        self._thread = None
        self._lock = threading.Lock()

    def start(self):
        self._running = True
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind((self.ip, self.port))
            self._sock.settimeout(0.1)
            self._thread = threading.Thread(target=self._listen, daemon=True)
            self._thread.start()
            return True
        except OSError as e:
            self._running = False
            self._sock = None
            return False

    def stop(self):
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        if self._thread:
            self._thread.join(timeout=1)
            self._thread = None

    def get_latest(self):
        with self._lock:
            return self.latest.copy()

    def _listen(self):
        while self._running:
            try:
                data, _ = self._sock.recvfrom(1024)
                if len(data) != 324:
                    continue
                is_race_on = struct.unpack_from('<i', data, 0)[0] == 1
                speed_ms = struct.unpack_from('<f', data, 256)[0]
                car_class = struct.unpack_from('<i', data, 216)[0]
                speed_kmh = speed_ms * 3.6
                handbrake = data[318]
                with self._lock:
                    self.latest["is_race_on"] = is_race_on
                    self.latest["speed_kmh"] = speed_kmh
                    self.latest["handbrake"] = handbrake
                    self.latest["car_class"] = car_class
            except socket.timeout:
                continue
            except Exception:
                pass


# ============================================================
# 核心工具
# ============================================================

class StoppableSleep:
    """可中断的 sleep，响应停止和暂停信号"""

    def __init__(self, stop_check, pause_check=None):
        self._stop = stop_check
        self._pause = pause_check

    def sleep(self, seconds, interval=0.1):
        """返回 True = 被中断，False = 正常超时"""
        if seconds <= 0.2:
            if self._pause:
                self._pause()
            if self._stop():
                return True
            time.sleep(seconds)
            return self._stop()

        elapsed = 0.0
        while elapsed < seconds:
            if self._pause:
                self._pause()
            if self._stop():
                return True
            chunk = min(interval, seconds - elapsed)
            time.sleep(chunk)
            elapsed += chunk
        return self._stop()


# ============================================================
# 工作线程
# ============================================================

class _BaseWorker(threading.Thread):
    """自动驾驶工作线程基类"""

    def __init__(self, app, stop_check, pause_check=None, on_failed=None):
        super().__init__(daemon=True)
        self._app = app
        self._stop = stop_check
        self._pause = pause_check
        self._ss = StoppableSleep(stop_check, pause_check)
        self.completed = 0
        self._on_failed = on_failed

    # -- 按键快捷方法，直接调主 App 的硬件接口 --
    def _kd(self, key):
        self._app.hw_key_down(key)

    def _ku(self, key):
        self._app.hw_key_up(key)

    def _kp(self, key, delay=0.08):
        self._app.hw_press(key, delay)

    def _release_all(self):
        for k in ['w', 'a', 's', 'd', 'enter', 'esc', 'space', 'c', '2']:
            self._app.hw_key_up(k)

    def _log(self, msg):
        self._app.log(msg)

    # -- 图像识别快捷方法 --
    def _find_any(self, images, threshold=0.70, timeout=1.0, interval=0.2):
        """单次扫描多张图，返回 (name, pos) 或 None"""
        region = self._app.regions.get("全界面")
        for img in images:
            pos = self._app.find_image(img, region=region, threshold=threshold, fast_mode=True)
            if pos:
                return (img, pos)
        return None

    def _wait_any(self, images, threshold=0.70, timeout=5.0, interval=0.3):
        """循环等待多张图中的任意一张，返回 (name, pos) 或 None"""
        region = self._app.regions.get("全界面")
        start = time.time()
        while not self._stop() and time.time() - start < timeout:
            for img in images:
                pos = self._app.find_image(img, region=region, threshold=threshold, fast_mode=True)
                if pos:
                    return (img, pos)
            sleep_end = time.time() + interval
            while not self._stop() and time.time() < sleep_end:
                time.sleep(0.05)
        return None


class _RivalWorker(_BaseWorker):
    """刷劲敌：按住 W，每 90 秒 Enter"""

    def run(self):
        self._log("[自动驾驶-刷劲敌] 开始：按住 W，每 90 秒 Enter")
        self._kd('w')
        try:
            while not self._stop():
                if self._ss.sleep(90):
                    break
                if self._stop():
                    break
                self._kp('enter')
                self.completed += 1
                self._log(f"[自动驾驶-刷劲敌] Enter 第 {self.completed} 次")
        finally:
            self._ku('w')
            self._release_all()
        self._log(f"[自动驾驶-刷劲敌] 已停止，共按 Enter {self.completed} 次")


class _OnlineWorker(_BaseWorker):
    """线上挂机：按住 W，每 20 秒 D，每 90 秒 Enter"""

    def run(self):
        self._log("[自动驾驶-线上挂机] 开始：按住 W，每 20s D，每 90s Enter")
        self._kd('w')
        last_enter = time.time()
        last_d = time.time()
        try:
            while not self._stop():
                now = time.time()
                if now - last_enter >= 90:
                    if self._stop():
                        break
                    self._kp('enter')
                    last_enter = now
                    self.completed += 1
                    self._log(f"[自动驾驶-线上挂机] Enter 第 {self.completed} 次")
                if now - last_d >= 20:
                    if self._stop():
                        break
                    self._kp('d')
                    last_d = now
                if self._ss.sleep(1):
                    break
        finally:
            self._ku('w')
            self._release_all()
        self._log(f"[自动驾驶-线上挂机] 已停止，Enter {self.completed} 次")


class _DeliveryWorker(_BaseWorker):
    """送外卖：纯按键 / 图像识别 两种模式"""

    def __init__(self, app, stop_check, pause_check, vision_mode=False):
        super().__init__(app, stop_check, pause_check)
        self.vision_mode = vision_mode

    def run(self):
        if self.vision_mode:
            self._run_vision()
        else:
            self._run_key_only()

    # ---- 纯按键 ----
    def _run_key_only(self):
        self._log("[自动驾驶-送外卖-纯按键] 开始：按住 W，每 60s Enter")
        self._kd('w')
        try:
            while not self._stop():
                if self._ss.sleep(60):
                    break
                if self._stop():
                    break
                self._kp('enter')
                self.completed += 1
                self._log(f"[自动驾驶-送外卖] 完成第 {self.completed} 单")
        finally:
            self._ku('w')
            self._release_all()
        self._log(f"[自动驾驶-送外卖] 已停止，共完成 {self.completed} 单")

    # ---- 图像识别 ----
    def _run_vision(self):
        self._log("[自动驾驶-送外卖-识图] 开始")
        while not self._stop():
            self._log("[送外卖-识图] 等待新订单...")
            anna_ready = self._check_anna()
            if self._stop():
                break

            if anna_ready:
                self._log("[送外卖-识图] Anna 可用，C+2 启动自动驾驶...")
                self._kp('c', delay=0.3)
                time.sleep(2.0)
                if self._stop():
                    break
                self._kp('2', delay=0.1)
                time.sleep(1.0)
            else:
                self._log("[送外卖-识图] Anna 不可用，纯按键兜底...")

            self._drive_wait_loop()
            if self._stop():
                break

        self._log(f"[自动驾驶-送外卖] 已停止，共完成 {self.completed} 单")

    def _check_anna(self):
        for i in range(10):
            if self._stop():
                return False
            result = self._find_any(["anna_on.png", "anna_off.png"], threshold=0.70)
            if result:
                name, _ = result
                self._log(f"[送外卖-识图] 检测到 Anna: {name}")
                return True
            time.sleep(0.5)
        self._log("[送外卖-识图] 10 次未检测到 Anna，视为不可用")
        return False

    def _drive_wait_loop(self):
        self._kd('w')
        start = time.time()
        timeout = 600
        manual_mode = False
        last_enter = time.time()

        try:
            while not self._stop():
                elapsed = time.time() - start

                if elapsed > timeout:
                    self._log("[送外卖-识图] 超时 10 分钟，按 Enter 跳过")
                    self._kp('enter')
                    self.completed += 1
                    return

                settle = self._find_any(["settle_menu.png"], threshold=0.70, timeout=1.5)
                if settle:
                    self._log("[送外卖-识图] 检测到结算画面，到达！")
                    self._kp('enter', delay=0.3)
                    time.sleep(0.5)
                    self.completed += 1
                    self._log(f"[送外卖-识图] 完成第 {self.completed} 单")
                    return

                if not manual_mode:
                    disabled = self._find_any(["anna_disabled.png"], threshold=0.75, timeout=1.0)
                    if disabled:
                        self._log("[送外卖-识图] Anna 被禁用，切换手动 W 模式")
                        manual_mode = True

                if manual_mode and time.time() - last_enter >= 60:
                    self._kp('enter')
                    last_enter = time.time()

                if self._ss.sleep(1):
                    break
        finally:
            self._ku('w')


class _DeliveryTelemetryWorker(_BaseWorker):
    """送外卖-遥测：读取 Forza UDP 数据，根据实时车速辅助驾驶"""

    def __init__(self, app, stop_check, pause_check, telemetry_config, on_failed=None):
        super().__init__(app, stop_check, pause_check, on_failed=on_failed)
        self._telem_ip = telemetry_config.get("ip", "127.0.0.1")
        self._telem_port = telemetry_config.get("port", 1000)
        self._telem = None

    def _signal_failed(self, msg):
        """通知面板启动失败"""
        self._log(msg)
        self._release_all()
        if self._on_failed:
            self._on_failed()

    def run(self):
        self._log(f"[送外卖-遥测] 启动 UDP {self._telem_ip}:{self._telem_port}")

        # 1. 启动遥测监听
        self._telem = TelemetryReceiver(self._telem_ip, self._telem_port)
        if not self._telem.start():
            self._signal_failed(
                "[送外卖-遥测] 端口绑定失败！"
                "请确认：① 游戏内 Data Out 已开启 ② IP/端口设置正确 ③ 端口未被占用")
            return

        self._log("[送外卖-遥测] 监听已就绪，等待游戏数据帧 (最多 5 秒)...")

        # 2. 等待第一帧真实数据（不是只 bind，要真收到包）
        telemetry_active = False
        wait_deadline = time.time() + 5.0
        while time.time() < wait_deadline:
            if self._stop():
                self._telem.stop()
                self._release_all()
                return
            data = self._telem.get_latest()
            if data.get("speed_kmh", 0.0) > 0.01 or data.get("is_race_on", False):
                telemetry_active = True
                break
            time.sleep(0.1)

        if telemetry_active:
            data = self._telem.get_latest()
            self._log(f"[送外卖-遥测] ✓ 数据连接成功！"
                      f"速度 {data.get('speed_kmh', 0):.1f} km/h，"
                      f"车辆等级 {data.get('car_class', -1)}")
        else:
            self._log("[送外卖-遥测] ⚠ 5 秒内未收到遥测数据！"
                      "将以纯按键兜底模式驾驶（W + 每 60s Enter）")

        # 3. 开始驾驶
        self._log("[送外卖-遥测] 按住 W，开始驾驶...")
        self._kd('w')
        stuck_start = 0.0
        last_enter = time.time()
        last_speed = 0.0
        enter_interval = 60

        try:
            while not self._stop():
                data = self._telem.get_latest()
                speed = data.get("speed_kmh", 0.0)

                now = time.time()

                # ---- 卡住检测：仅遥测活跃时生效 ----
                if telemetry_active and speed <= 2.0:
                    if stuck_start == 0:
                        stuck_start = now
                    elif now - stuck_start >= 8:
                        self._log(f"[送外卖-遥测] 静止 {now - stuck_start:.0f}s，按 Enter")
                        self._kp('enter')
                        self.completed += 1
                        self._log(f"[送外卖-遥测] 完成第 {self.completed} 单")
                        stuck_start = 0
                        last_enter = now
                        self._ss.sleep(3)
                        continue
                else:
                    stuck_start = 0

                # ---- 速度骤降检测 ----
                if last_speed >= 30 and speed <= 1 and telemetry_active:
                    self._log(f"[送外卖-遥测] 速度骤降 {last_speed:.0f}→{speed:.0f} km/h，疑似到达")
                    self._ss.sleep(2)
                    if not self._stop():
                        data2 = self._telem.get_latest()
                        if data2.get("speed_kmh", 0) <= 2:
                            self._kp('enter')
                            self.completed += 1
                            self._log(f"[送外卖-遥测] 完成第 {self.completed} 单")
                            stuck_start = 0
                            last_enter = time.time()
                            self._ss.sleep(3)
                            last_speed = 0
                            continue

                # ---- 定时 Enter 兜底 ----
                if now - last_enter >= enter_interval:
                    self._kp('enter')
                    last_enter = now
                    self._log(f"[送外卖-遥测] 定时 Enter")

                last_speed = speed

                if self._ss.sleep(1):
                    break

        finally:
            self._ku('w')
            self._release_all()
            if self._telem:
                self._telem.stop()
        self._log(f"[送外卖-遥测] 已停止，共完成 {self.completed} 单")


# ============================================================
# UI 面板（自包含，嵌入主窗口）
# ============================================================

MODES = ["送外卖-纯按键", "送外卖-图像识别", "送外卖-遥测模式", "刷劲敌", "线上挂机"]


class AutoDrivePanel(ctk.CTkFrame):
    """自动驾驶控制面板 — 完全独立于流水线系统"""

    def __init__(self, master, app):
        super().__init__(master, fg_color="#1A1A2E", corner_radius=12)
        self._app = app
        self._worker = None
        self._running = False
        self._mini_frame = None
        self._mini_timer_id = None
        self._start_time = 0

        # ---- 顶部标题栏 ----
        top_bar = ctk.CTkFrame(self, fg_color="transparent")
        top_bar.pack(fill="x", padx=20, pady=(18, 8))

        ctk.CTkLabel(
            top_bar, text="自动驾驶系统",
            font=ctk.CTkFont(size=22, weight="bold"), text_color="#F1C40F"
        ).pack(side="left")

        ctk.CTkLabel(
            top_bar, text="独立于流水线 · 使用独立逻辑",
            font=ctk.CTkFont(size=12), text_color="#666666"
        ).pack(side="left", padx=(12, 0))

        # ---- 模式选择卡片 ----
        card = ctk.CTkFrame(self, fg_color="#16213E", corner_radius=10)
        card.pack(fill="x", padx=20, pady=(0, 10))

        ctk.CTkLabel(
            card, text="驾驶模式", font=ctk.CTkFont(size=14, weight="bold"), text_color="#E0E0E0"
        ).pack(pady=(15, 5))

        self._mode_var = ctk.StringVar(value="送外卖-纯按键")
        self._mode_menu = ctk.CTkOptionMenu(
            card, values=MODES, variable=self._mode_var,
            width=200, height=32, corner_radius=8,
            font=ctk.CTkFont(size=13),
            fg_color="#0E7490", button_color="#0C6470", button_hover_color="#0A5560"
        )
        self._mode_menu.pack(pady=(0, 5))

        # ---- 控制按钮 ----
        btn_frame = ctk.CTkFrame(card, fg_color="transparent")
        btn_frame.pack(pady=(10, 15))

        self._btn_start = ctk.CTkButton(
            btn_frame, text="启动自动驾驶", width=160, height=40,
            corner_radius=10, font=ctk.CTkFont(size=14, weight="bold"),
            fg_color="#27AE60", hover_color="#1E8449",
            command=self._toggle
        )
        self._btn_start.pack(side="left", padx=5)

        self._btn_stop = ctk.CTkButton(
            btn_frame, text="停止 (F8)", width=120, height=40,
            corner_radius=10, font=ctk.CTkFont(size=14, weight="bold"),
            fg_color="#DA3633", hover_color="#B02A37",
            command=self._stop, state="disabled"
        )
        self._btn_stop.pack(side="left", padx=5)

        # ---- 模式切换回调 ----
        self._mode_var.trace_add("write", self._on_mode_changed)

        # ---- 状态栏 ----
        self._lbl_status = ctk.CTkLabel(
            card, text="就绪 · 等待启动", font=ctk.CTkFont(size=13), text_color="#888888"
        )
        self._lbl_status.pack(pady=(0, 15))

        # ---- 遥测设置卡片（仅遥测模式显示） ----
        self._telem_card = ctk.CTkFrame(self, fg_color="#16213E", corner_radius=10)

        ctk.CTkLabel(
            self._telem_card, text="遥测设置 (Forza Data Out)",
            font=ctk.CTkFont(size=14, weight="bold"), text_color="#F1C40F"
        ).pack(pady=(15, 8))

        telem_row = ctk.CTkFrame(self._telem_card, fg_color="transparent")
        telem_row.pack(pady=(0, 5))

        ctk.CTkLabel(
            telem_row, text="IP:", font=ctk.CTkFont(size=13), text_color="#E0E0E0"
        ).pack(side="left", padx=(0, 5))
        self._telem_ip_var = ctk.StringVar(value="127.0.0.1")
        self._entry_telem_ip = ctk.CTkEntry(
            telem_row, textvariable=self._telem_ip_var, width=130, height=28,
            corner_radius=6, font=ctk.CTkFont(size=13)
        )
        self._entry_telem_ip.pack(side="left", padx=(0, 15))

        ctk.CTkLabel(
            telem_row, text="端口:", font=ctk.CTkFont(size=13), text_color="#E0E0E0"
        ).pack(side="left", padx=(0, 5))
        self._telem_port_var = ctk.StringVar(value="1000")
        self._entry_telem_port = ctk.CTkEntry(
            telem_row, textvariable=self._telem_port_var, width=80, height=28,
            corner_radius=6, font=ctk.CTkFont(size=13)
        )
        self._entry_telem_port.pack(side="left", padx=(0, 15))

        self._btn_telem_test = ctk.CTkButton(
            telem_row, text="测试连接", width=90, height=28,
            corner_radius=6, font=ctk.CTkFont(size=12),
            fg_color="#0E7490", hover_color="#0C6470",
            command=self._test_telemetry
        )
        self._btn_telem_test.pack(side="left")

        self._lbl_telem_status = ctk.CTkLabel(
            self._telem_card, text="", font=ctk.CTkFont(size=12), text_color="#888888"
        )
        self._lbl_telem_status.pack(pady=(5, 12))

        # ---- 说明文字 ----
        info_frame = ctk.CTkFrame(self, fg_color="#16213E", corner_radius=10)
        info_frame.pack(fill="both", expand=True, padx=20, pady=(0, 10))
        self._info_frame = info_frame

        info_text = (
            "模式说明：\n\n"
            "送外卖-纯按键：按住 W 前进，每 60 秒按 Enter 交单，无限循环\n"
            "送外卖-图像识别：检测 Anna 导航状态 + 结算画面，智能交单\n"
            "送外卖-遥测模式：读取 Forza UDP 数据，根据车速判断到达/卡住\n"
            "刷劲敌：按住 W + 每 90 秒 Enter 防掉线，适合劲敌挂机\n"
            "线上挂机：按住 W + 每 20 秒 D + 每 90 秒 Enter，适合线上漫游"
        )
        ctk.CTkLabel(
            info_frame, text=info_text, font=ctk.CTkFont(size=12),
            text_color="#A0A0A0", justify="left", wraplength=500
        ).pack(padx=20, pady=15, anchor="w")

    # ================================================================
    # 遥测设置
    # ================================================================

    def _on_mode_changed(self, *args):
        """模式切换时显示/隐藏遥测设置"""
        if self._mode_var.get() == "送外卖-遥测模式":
            self._telem_card.pack(fill="x", padx=20, pady=(0, 10), before=self._info_frame)
        else:
            self._telem_card.pack_forget()

    def _test_telemetry(self):
        """测试遥测 UDP 连接（3 秒内收到有效包即成功）"""
        ip = self._telem_ip_var.get().strip() or "127.0.0.1"
        try:
            port = int(self._telem_port_var.get().strip() or "1000")
        except ValueError:
            port = 1000

        self._lbl_telem_status.configure(text="正在测试...", text_color="#F1C40F")
        self._btn_telem_test.configure(state="disabled")

        result = {"ok": False}

        def do_test():
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((ip, port))
                sock.settimeout(3)
                data, _ = sock.recvfrom(1024)
                if len(data) == 324:
                    result["ok"] = True
                sock.close()
            except Exception:
                pass

        t = threading.Thread(target=do_test, daemon=True)
        t.start()

        def check_result():
            t.join(timeout=0.1)
            if not t.is_alive():
                if result["ok"]:
                    self._lbl_telem_status.configure(text="已接收数据 — 连接正常", text_color="#27AE60")
                else:
                    self._lbl_telem_status.configure(text="连接失败 — 请确认游戏 Data Out 已开启", text_color="#DA3633")
                self._btn_telem_test.configure(state="normal")
            else:
                self._app.after(500, check_result)

        self._app.after(600, check_result)

    # ================================================================
    # 迷你模式 UI
    # ================================================================

    def _build_mini_frame(self):
        """创建迷你模式框架（作为 root 窗口子控件）"""
        if self._mini_frame is not None:
            return
        root = self._app
        self._mini_frame = ctk.CTkFrame(root, fg_color="#1E1E1E", corner_radius=10)

        # 日志区
        self._mini_log_box = ctk.CTkTextbox(
            self._mini_frame, state="disabled", wrap="word",
            font=ctk.CTkFont(size=13), fg_color="#2B2B2B"
        )
        self._mini_log_box.pack(side="left", fill="both", expand=True, padx=(10, 5), pady=10)

        # 信息区
        mini_info = ctk.CTkFrame(self._mini_frame, fg_color="transparent")
        mini_info.pack(side="left", fill="y", padx=5, pady=10)

        self._lbl_mini_mode = ctk.CTkLabel(
            mini_info, text="自动驾驶", font=ctk.CTkFont(size=14, weight="bold"), text_color="#F1C40F"
        )
        self._lbl_mini_mode.pack(pady=(5, 2), anchor="w")

        self._lbl_mini_status = ctk.CTkLabel(
            mini_info, text="运行中...", font=ctk.CTkFont(size=13), text_color="#27AE60"
        )
        self._lbl_mini_status.pack(pady=2, anchor="w")

        self._lbl_mini_count = ctk.CTkLabel(
            mini_info, text="完成: 0 次", font=ctk.CTkFont(size=13)
        )
        self._lbl_mini_count.pack(pady=2, anchor="w")

        self._lbl_mini_time = ctk.CTkLabel(
            mini_info, text="总耗时: 00:00:00", font=ctk.CTkFont(size=13)
        )
        self._lbl_mini_time.pack(pady=2, anchor="w")

        # 停止按钮
        self._btn_mini_stop = ctk.CTkButton(
            self._mini_frame, text="停止 (F8)", width=90,
            fg_color="#DA3633", hover_color="#B02A37",
            font=ctk.CTkFont(weight="bold"), command=self._stop
        )
        self._btn_mini_stop.pack(side="left", fill="y", padx=(5, 10), pady=10)

    def _update_mini_timer(self):
        if not self._running:
            self._mini_timer_id = None
            return
        elapsed = int(time.time() - self._start_time)
        hrs, rem = divmod(elapsed, 3600)
        mins, secs = divmod(rem, 60)
        try:
            self._lbl_mini_time.configure(text=f"总耗时: {hrs:02d}:{mins:02d}:{secs:02d}")
            if self._worker is not None:
                self._lbl_mini_count.configure(text=f"完成: {self._worker.completed} 次")
        except Exception:
            pass
        self._mini_timer_id = self._app.after(1000, self._update_mini_timer)

    # ================================================================
    # 公开 API
    # ================================================================

    def _toggle(self):
        if self._running:
            self._stop()
        else:
            self._start()

    def _start(self):
        if self._app.is_running:
            self._app.log("[自动驾驶] 主流水线正在运行，请先停止流水线！")
            return

        mode = self._mode_var.get()
        self._app.is_running = True
        self._running = True

        stop_check = lambda: not self._running
        pause_check = self._app.check_pause if hasattr(self._app, 'check_pause') else None

        if mode == "刷劲敌":
            self._worker = _RivalWorker(self._app, stop_check, pause_check)
        elif mode == "线上挂机":
            self._worker = _OnlineWorker(self._app, stop_check, pause_check)
        elif mode == "送外卖-图像识别":
            self._worker = _DeliveryWorker(self._app, stop_check, pause_check, vision_mode=True)
        elif mode == "送外卖-遥测模式":
            telem_config = {
                "ip": self._telem_ip_var.get().strip() or "127.0.0.1",
                "port": int(self._telem_port_var.get().strip() or "1000"),
            }
            self._worker = _DeliveryTelemetryWorker(
                self._app, stop_check, pause_check, telem_config,
                on_failed=lambda: self._app.after(0, self._on_worker_start_failed))
        else:
            self._worker = _DeliveryWorker(self._app, stop_check, pause_check, vision_mode=False)

        self._worker.start()
        self._app.log(f"[自动驾驶] 启动模式: {mode}")

        self._btn_start.configure(text="暂停 (F9)", fg_color="#F1C40F", hover_color="#D4AC0D", state="disabled")
        self._btn_stop.configure(state="normal")
        self._mode_menu.configure(state="disabled")
        self._lbl_status.configure(text=f"运行中: {mode}", text_color="#27AE60")

        # 进入迷你模式
        self._build_mini_frame()
        self._lbl_mini_mode.configure(text=f"自动驾驶 · {mode}")
        self._app._enter_auto_drive_mini(self._mini_frame, self._mini_log_box)
        self._start_time = time.time()
        self._update_mini_timer()

    def _on_worker_start_failed(self):
        """遥测 Worker 启动失败的回调（主线程）"""
        self._app.log("[自动驾驶] 启动失败，请检查遥测设置")
        self._running = False
        self._app.is_running = False
        self._worker = None
        self._btn_start.configure(text="启动自动驾驶", fg_color="#27AE60",
                                  hover_color="#1E8449", state="normal")
        self._btn_stop.configure(state="disabled")
        self._mode_menu.configure(state="normal")
        self._lbl_status.configure(text="启动失败 · 检查遥测设置", text_color="#DA3633")

    def _stop(self):
        self._app.log("[自动驾驶] 正在停止...")
        self._running = False
        self._app.is_running = False

        # 停掉迷你模式定时器
        if self._mini_timer_id is not None:
            try:
                self._app.after_cancel(self._mini_timer_id)
            except Exception:
                pass
            self._mini_timer_id = None

        if self._worker and self._worker.is_alive():
            self._worker.join(timeout=3)
        self._worker = None

        # 释放所有按键
        for k in ['w', 'a', 's', 'd', 'enter', 'esc', 'space', 'c', '2']:
            self._app.hw_key_up(k)

        self._app.is_running = False

        # 退出迷你模式
        if self._mini_frame is not None:
            self._app._exit_auto_drive_mini(self._mini_frame)

        self._btn_start.configure(text="启动自动驾驶", fg_color="#27AE60", hover_color="#1E8449", state="normal")
        self._btn_stop.configure(state="disabled")
        self._mode_menu.configure(state="normal")
        self._lbl_status.configure(text="就绪 · 等待启动", text_color="#888888")
        self._app.log("[自动驾驶] 已停止")

    def force_stop(self):
        """由主 App 调用（F8 全局停止时）"""
        if self._running:
            self._stop()
