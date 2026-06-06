"""FH6Auto 一键抢车模块
拍卖行自动抢车（Auction House Sniper），与主流水线完全独立
基于 FrostyIsBored/FH6-Auction-House-Sniper 改编，支持中文识别

入口: AuctionSniperPanel(ctk.CTkFrame) — 嵌入主窗口的面板
"""

from __future__ import annotations
import json
import logging
import threading
import time
from pathlib import Path
import customtkinter as ctk

from fh6_sniper import capture, vision
from fh6_sniper.config import Config
from fh6_sniper.mini_overlay import MiniOverlay
from fh6_sniper.sniper import GameIO, Sniper

log = logging.getLogger("fh6.sniper_panel")


def _setup_sniper_logging(log_dir: Path):
    """Setup file logging for the sniper subsystem."""
    log_path = log_dir / "sniper.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)s %(message)s", "%H:%M:%S")
    file_handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    file_handler.setFormatter(fmt)
    root = logging.getLogger("fh6")
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(file_handler)



class AuctionSniperPanel(ctk.CTkFrame):
    """独立的一键抢车面板，不与其他功能融合"""

    def __init__(self, master, app):
        super().__init__(master, fg_color="transparent")
        self._app = app
        self._running = False
        self._sniper = None
        self._thread = None
        self._cfg = None
        self._io = None
        self._templates = None

        # 累计统计（跨启停）
        self._display = {"searches": 0, "bought": 0, "fails": 0}
        self._last_bot_stats = (0, 0, 0)

        # 模板路径
        self._template_dir = None

        # 迷你悬浮窗
        self._mini = None
        self._notify_sound = True
        self._notify_toast = True

        self._build_ui()

    def _build_ui(self):
        # ---------- 标题栏 ----------
        title_frame = ctk.CTkFrame(self, fg_color="#2B2B2B")
        title_frame.pack(fill="x", pady=(0, 8))

        ctk.CTkLabel(
            title_frame, text="🎯 一键抢车 — 拍卖行自动扫货",
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color="#FFFFFF",
        ).pack(side="left", padx=16, pady=10)

        self._status_dot = ctk.CTkLabel(
            title_frame, text="●", font=ctk.CTkFont(size=14),
            text_color="#888888",
        )
        self._status_dot.pack(side="left", padx=(0, 6))

        self._status_label = ctk.CTkLabel(
            title_frame, text="就绪",
            font=ctk.CTkFont(size=12), text_color="#AAAAAA",
        )
        self._status_label.pack(side="left")

        # ---------- 主内容区（两列） ----------
        content = ctk.CTkFrame(self, fg_color="transparent")
        content.pack(fill="both", expand=True, padx=6)

        # 左列：设置
        left_col = ctk.CTkFrame(content, fg_color="#2B2B2B")
        left_col.pack(side="left", fill="both", expand=True, padx=(0, 4))

        ctk.CTkLabel(
            left_col, text="⚙ 抢车设置",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(anchor="w", padx=14, pady=(10, 6))

        # 模板语言选择
        lang_row = ctk.CTkFrame(left_col, fg_color="transparent")
        lang_row.pack(fill="x", padx=14, pady=4)
        ctk.CTkLabel(lang_row, text="界面语言:", font=ctk.CTkFont(size=12)).pack(side="left")
        self._lang_var = ctk.StringVar(value="简体中文")
        self._lang_menu = ctk.CTkOptionMenu(
            lang_row, values=["简体中文", "English"],
            variable=self._lang_var, width=130,
            command=self._on_lang_changed,
        )
        self._lang_menu.pack(side="right")

        # 最大抢车数
        max_row = ctk.CTkFrame(left_col, fg_color="transparent")
        max_row.pack(fill="x", padx=14, pady=4)
        ctk.CTkLabel(max_row, text="最大抢车数:", font=ctk.CTkFont(size=12)).pack(side="left")
        self._entry_max_cars = ctk.CTkEntry(max_row, width=80, height=26)
        self._entry_max_cars.pack(side="right")
        self._entry_max_cars.insert(0, "1")

        # 最大运行时间
        time_row = ctk.CTkFrame(left_col, fg_color="transparent")
        time_row.pack(fill="x", padx=14, pady=4)
        ctk.CTkLabel(time_row, text="最大运行时间(分):", font=ctk.CTkFont(size=12)).pack(side="left")
        self._entry_max_minutes = ctk.CTkEntry(time_row, width=80, height=26)
        self._entry_max_minutes.pack(side="right")
        self._entry_max_minutes.insert(0, "180")

        # 匹配阈值
        thresh_row = ctk.CTkFrame(left_col, fg_color="transparent")
        thresh_row.pack(fill="x", padx=14, pady=4)
        ctk.CTkLabel(thresh_row, text="匹配阈值:", font=ctk.CTkFont(size=12)).pack(side="left")
        self._slider_threshold = ctk.CTkSlider(
            left_col, from_=0.00, to=1.00,
            width=200, height=18,
        )
        self._slider_threshold.pack(padx=14, pady=2)
        self._slider_threshold.set(0.80)
        self._lbl_threshold = ctk.CTkLabel(
            left_col, text="0.80", font=ctk.CTkFont(size=11), text_color="#AAAAAA",
        )
        self._lbl_threshold.pack(anchor="e", padx=14)
        self._slider_threshold.configure(
            command=lambda v: self._lbl_threshold.configure(text=f"{v:.2f}")
        )

        # 循环间隔
        pace_row = ctk.CTkFrame(left_col, fg_color="transparent")
        pace_row.pack(fill="x", padx=14, pady=4)
        ctk.CTkLabel(pace_row, text="循环间隔(秒):", font=ctk.CTkFont(size=12)).pack(side="left")
        self._entry_loop_pace = ctk.CTkEntry(pace_row, width=80, height=26)
        self._entry_loop_pace.pack(side="right")
        self._entry_loop_pace.insert(0, "0.15")

        # 测试模板按钮
        self._btn_test = ctk.CTkButton(
            left_col, text="测试模板",
            font=ctk.CTkFont(size=12),
            fg_color="#555555", hover_color="#666666",
            height=30, command=self._test_templates,
        )
        self._btn_test.pack(fill="x", padx=14, pady=(10, 10))

        # 右列：统计 + 快捷设置 + 操作
        right_col = ctk.CTkFrame(content, fg_color="#2B2B2B")
        right_col.pack(side="right", fill="both", expand=True, padx=(4, 0))

        # 统计卡片 — 4 格，与迷你窗口一致
        stats_frame = ctk.CTkFrame(right_col, fg_color="transparent")
        stats_frame.pack(fill="x", padx=14, pady=(10, 6))

        for i, (label, key, color) in enumerate([
            ("已抢到",   "bought",    "#2EA043"),
            ("搜索次数", "searches",  "#AAAAAA"),
            ("失败次数", "fails",     "#DA3633"),
            ("运行时间", "uptime",    "#AAAAAA"),
        ]):
            cell = ctk.CTkFrame(stats_frame, fg_color="#1E1E1E")
            cell.pack(side="left", fill="x", expand=True, padx=(0 if i < 3 else 0, 3 if i < 3 else 0))
            ctk.CTkLabel(
                cell, text=label, font=ctk.CTkFont(size=11), text_color="#888888",
            ).pack(pady=(8, 0))
            lbl = ctk.CTkLabel(
                cell, text="0" if key != "uptime" else "00:00",
                font=ctk.CTkFont(size=20, weight="bold"),
                text_color=color,
            )
            lbl.pack(pady=(0, 8))
            setattr(self, f"_stat_{key}", lbl)

        # 快捷设置复选框
        chk_frame = ctk.CTkFrame(right_col, fg_color="transparent")
        chk_frame.pack(fill="x", padx=14, pady=(8, 4))

        self._chk_collect = ctk.CTkCheckBox(
            chk_frame, text="自动领取车辆", font=ctk.CTkFont(size=12),
        )
        self._chk_collect.pack(anchor="w", pady=2)
        self._chk_collect.select()

        self._chk_sound = ctk.CTkCheckBox(
            chk_frame, text="声音提示", font=ctk.CTkFont(size=12),
        )
        self._chk_sound.pack(anchor="w", pady=2)
        self._chk_sound.select()

        self._chk_bg = ctk.CTkCheckBox(
            chk_frame, text="动态背景已开启", font=ctk.CTkFont(size=12),
        )
        self._chk_bg.pack(anchor="w", pady=2)
        self._chk_bg.select()

        self._chk_hdr = ctk.CTkCheckBox(
            chk_frame, text="HDR 模式", font=ctk.CTkFont(size=12),
        )
        self._chk_hdr.pack(anchor="w", pady=2)

        # 开始 / 停止按钮（大按钮）
        self._btn_start = ctk.CTkButton(
            right_col, text="🚗 开始抢车",
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color="#2EA043", hover_color="#238636",
            height=42, command=self._toggle,
        )
        self._btn_start.pack(fill="x", padx=14, pady=(10, 0))

        # 运行状态提示
        self._hint_label = ctk.CTkLabel(
            right_col, text="日志输出至下方主日志框",
            font=ctk.CTkFont(size=11), text_color="#888888",
        )
        self._hint_label.pack(pady=(8, 10))

    # ============================================================
    # 公共 API
    # ============================================================

    def force_stop(self):
        """强制停止（由主窗口 F8 调用）"""
        if self._running:
            self._log("收到强制停止信号")
            self._stop()

    # ============================================================
    # 内部逻辑
    # ============================================================

    def _log(self, msg: str):
        """输出到主窗口底部日志框"""
        try:
            self._app.log(msg)
        except Exception:
            pass

    def _resolve_template_dir(self) -> Path:
        """根据语言选择返回模板目录路径"""
        if self._lang_var.get() == "简体中文":
            return Path(__file__).parent / "images" / "sniper" / "cn"
        else:
            return Path(__file__).parent / "images" / "sniper" / "en"

    def _on_lang_changed(self, value):
        """语言切换回调"""
        if self._running:
            self._log("⚠ 运行中无法切换语言，请先停止")
            self._lang_var.set("简体中文" if value == "English" else "English")
            return
        self._log(f"已切换到: {value}")
        self._init_templates()

    def _init_templates(self):
        """加载模板"""
        self._template_dir = self._resolve_template_dir()
        try:
            moving_bg = self._chk_bg.get()
            self._templates = vision.load_templates(
                self._template_dir, moving_background=moving_bg)
            self._log(f"模板加载成功: {self._template_dir.name} ({len(self._templates)} 个)")
        except FileNotFoundError as e:
            self._log(f"⚠ 模板缺失: {e}")
            self._templates = None
        except Exception as e:
            self._log(f"⚠ 模板加载失败: {e}")
            self._templates = None

    def _build_config(self) -> Config:
        """从面板控件构建配置"""
        cfg = Config()
        cfg.max_cars = int(self._entry_max_cars.get() or "1")
        cfg.max_minutes = float(self._entry_max_minutes.get() or "180")
        cfg.match_threshold = self._slider_threshold.get()
        cfg.loop_pace_s = float(self._entry_loop_pace.get() or "0.15")
        cfg.collect_after_buyout = self._chk_collect.get()
        cfg.moving_background = self._chk_bg.get()
        cfg.hdr_mode = self._chk_hdr.get()
        cfg.notify_sound = self._chk_sound.get()
        cfg.notify_toast = self._notify_toast
        cfg.template_dir = str(self._template_dir)
        # 面板模式默认开启 Win32 API 输入，无需手动切焦点到 FH6
        cfg.win32_api_input = True
        # 禁用独立热键 — 由主程序 F8 管理
        cfg.hotkey_start_stop = "<f7>"
        cfg.hotkey_panic = "<f7>"
        return cfg

    def _set_status(self, text: str):
        """更新状态文字和指示灯"""
        self._status_label.configure(text=text)
        if "运行" in text or "搜索" in text or "抢到" in text or "领取" in text:
            self._status_dot.configure(text_color="#2EA043")
        elif "暂停" in text:
            self._status_dot.configure(text_color="#D97706")
        else:
            self._status_dot.configure(text_color="#888888")
        self._log(text)

    def _set_stats(self, searches, bought, fails):
        """更新统计显示"""
        self._stat_searches.configure(text=str(searches))
        self._stat_bought.configure(text=str(bought))
        self._stat_fails.configure(text=str(fails))

    def _update_uptime(self):
        """更新运行时间显示"""
        if not self._running:
            return
        if self._started_at:
            elapsed = int(time.monotonic() - self._started_at)
            h, m, s = elapsed // 3600, (elapsed % 3600) // 60, elapsed % 60
            self._stat_uptime.configure(text=f"{h:02d}:{m:02d}:{s:02d}")
        self.after(1000, self._update_uptime)

    def _toggle(self):
        """开始/停止切换"""
        if self._running:
            self._stop()
        else:
            self._start()

    def _start(self):
        """启动抢车"""
        if self._running:
            return

        self._init_templates()
        if self._templates is None:
            self._log("⚠ 无法启动：模板加载失败，请检查模板目录")
            return

        self._cfg = self._build_config()
        self._io = GameIO(self._cfg, self._templates)

        # 重置统计
        self._display = {"searches": 0, "bought": 0, "fails": 0}
        self._last_bot_stats = (0, 0, 0)
        self._set_stats(0, 0, 0)
        self._stat_uptime.configure(text="00:00")

        # 设置日志
        _setup_sniper_logging(Path(__file__).parent / "logs")

        def on_stats(searches, bought, fails):
            last_s, last_b, last_f = self._last_bot_stats
            d = self._display
            d["searches"] += max(0, searches - last_s)
            d["bought"]   += max(0, bought   - last_b)
            d["fails"]    += max(0, fails    - last_f)
            self._last_bot_stats = (searches, bought, fails)
            self.after(0, lambda: self._set_stats(
                d["searches"], d["bought"], d["fails"]))
            if self._mini:
                self._mini.set_stats(d["searches"], d["bought"], d["fails"])

        def on_purchase(loop_seconds, total):
            self.after(0, lambda: self._log(
                f"🎉 抢到一辆车！本次用时 {loop_seconds:.1f}s，累计 {total} 辆"))

        self._sniper = Sniper(
            self._io, self._cfg,
            on_purchase=on_purchase,
            on_status=lambda t: self.after(0, lambda: (
                self._set_status(t),
                self._mini and self._mini.set_status(t)
            )),
            on_stats=on_stats,
        )

        # 创建/显示迷你悬浮窗
        if self._mini is None:
            self._mini = MiniOverlay(
                on_stop=self._stop,
                on_start=self._start,
                on_close=self._close_overlay,
                on_setting_changed=self._on_mini_setting_changed,
                master=self.winfo_toplevel())
        # 同步当前设置到迷你窗口
        self._mini.sync_settings(
            self._chk_collect.get(),
            self._chk_sound.get(),
            self._cfg.notify_toast)
        self._mini.set_status("运行中...")
        self._mini.set_stats(0, 0, 0)
        self._mini.show()

        # 最小化主窗口（Windows 会隐藏 Toplevel 子窗口，force_show 补刀）
        try:
            self._app.iconify()
        except Exception:
            pass
        self._mini.force_show()

        def _run_safe():
            try:
                outcome = self._sniper.run()
            except Exception:
                logging.getLogger("fh6.main").exception("抢车线程异常")
                self.after(0, lambda: self._log("⚠ 抢车线程异常，查看 sniper.log"))
                outcome = "exception"
            # 线程退出时清理（auto-stop / recover_failed / exception）
            self.after(0, lambda: self._on_sniper_done(outcome))

        self._thread = threading.Thread(target=_run_safe, daemon=True)
        self._thread.start()

        self._running = True
        self._started_at = time.monotonic()
        self._btn_start.configure(text="⏹ 停止抢车", fg_color="#DA3633", hover_color="#C0352F")
        self._lang_menu.configure(state="disabled")
        self._set_status("运行中...")
        self._update_uptime()
        self._log("🚀 抢车已启动")

    def _stop(self):
        """停止抢车 — 不关迷你窗口，用户可重新开始"""
        if self._sniper and self._running:
            self._sniper.request_stop()

        self._running = False
        self._btn_start.configure(text="🚗 开始抢车", fg_color="#2EA043", hover_color="#238636")
        self._lang_menu.configure(state="normal")
        self._set_status("已停止")
        self._log("⏹ 抢车已停止")

        if self._mini:
            self._mini.set_status("已停止")

    def _close_overlay(self):
        """关闭迷你窗口，恢复主窗口"""
        if self._mini:
            self._mini.hide()
        try:
            self._app.deiconify()
            self._app.lift()
            self._app.focus_force()
        except Exception:
            pass

    def _on_sniper_done(self, outcome: str):
        """Sniper 线程退出后的清理（自动停止 / 恢复失败等）"""
        self._running = False
        self._btn_start.configure(text="🚗 开始抢车", fg_color="#2EA043", hover_color="#238636")
        self._lang_menu.configure(state="normal")
        self._log(f"抢车线程结束 ({outcome})")

    def _on_mini_setting_changed(self, key: str, value: bool):
        """迷你窗口快捷设置变动时同步到配置和面板控件"""
        try:
            if key == "collect_after_buyout":
                self._chk_collect.configure(
                    text="自动领取车辆" if value else "自动领取车辆 (已关闭)")
                if self._cfg:
                    self._cfg.collect_after_buyout = value
            elif key == "notify_sound":
                if value:
                    self._chk_sound.select()
                else:
                    self._chk_sound.deselect()
                if self._cfg:
                    self._cfg.notify_sound = value
            elif key == "notify_toast":
                self._notify_toast = value
                if self._cfg:
                    self._cfg.notify_toast = value
        except Exception:
            pass

    def _test_templates(self):
        """测试模板匹配"""
        self._init_templates()
        if self._templates is None:
            self._log("⚠ 模板加载失败")
            return

        try:
            frame = capture.grab_screen("Forza Horizon 6")
            scores = vision.screen_scores(frame, self._templates)
            self._log("--- 模板匹配测试 ---")
            for name in sorted(scores, key=lambda n: -scores[n]):
                score = scores[name]
                bar = "█" * int(score * 20) + "░" * (20 - int(score * 20))
                tag = " ✓" if score >= 0.80 else ""
                self._log(f"  {name:30s} {score:.3f} {bar}{tag}")
            best = max(scores, key=lambda n: scores[n])
            best_scr = vision.TEMPLATE_SCREENS.get(best)
            self._log(f"当前画面识别: {best_scr.name if best_scr else '未知'} "
                      f"(分数: {scores[best]:.3f})")
        except Exception as e:
            self._log(f"⚠ 测试失败: {e}")
