"""
ZEP 카메라 버튼 자동 클릭 — GUI 버전 (Tkinter).

구조:
  - GUI는 메인 스레드에서 동작 (Tkinter 요구사항).
  - 실제 Google Chrome을 디버그 모드(--remote-debugging-port)로 직접 실행하고,
    Playwright가 CDP(connect_over_cdp)로 거기에 "연결"한다.
    → Playwright가 만든 자동화 브라우저가 아니라 평범한 Chrome이라,
      Google 로그인의 "이 브라우저는 안전하지 않을 수 있습니다" 차단을 받지 않는다.
  - 스케줄 루프는 별도 워커 스레드에서 동작. 워커 → GUI 통신은 thread-safe 큐로 처리.

사용 순서:
  1. ZEP 링크 입력 → "브라우저 열기" → 뜬 Chrome에서 직접 Google 로그인·입장
  2. 준비되면 "스케줄 시작" → 정해진 규칙대로 카메라 상태를 자동 관리
     (매시 :00 켜기 / :50 끄기, 점심 11:50~13:00 꺼둠, 18:50 종료 — 상단 상수로 조정)
     · 무조건 토글이 아니라, 현재 on/off를 확인해 필요할 때만 클릭
  3. 언제든 "중지"로 멈춤 (다시 시작 가능). 창을 닫으면 Chrome도 정리됨.
"""

import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, scrolledtext, ttk

from playwright.sync_api import sync_playwright

# ─────────────────────────── 설정값 ───────────────────────────
# 카메라 버튼 셀렉터 (앞에서부터 순서대로 시도)
#  1) aria-label="카메라"  — 현재 ZEP이 쓰는 방식 (가장 안정적)
#  2) data-sentry-element="MediaDeviceButton" 의 2번째 — 예전 방식 폴백(1번=마이크)
CAMERA_SELECTORS = [
    '[aria-label="카메라"]',
    '[data-sentry-element="MediaDeviceButton"] >> nth=1',
]

# ── 스케줄 규칙 (필요하면 여기 숫자만 바꾸세요) ──
CLICK_MINUTES = [0, 50]  # 동작 시각: 매시 정각(:00)과 50분(:50)
END_TIME = (18, 50)  # 마지막 동작 시각 = 18:50 (카메라 끄기)
QUIT_TIME = (19, 0)  # 프로그램 완전 종료 시각 = 19:00
#   ↑ 종료 감시는 마지막 동작(18:50)을 마친 뒤에야 시작한다.
#     그 전까지는 19:00을 신경 쓰지 않고, 이후 19:00까지 한 번만 대기한다.
LUNCH_START = (11, 50)  # 점심 시작 — 이때부터 카메라 꺼둠
LUNCH_END = (13, 0)  # 점심 끝 — 이때 다시 켬 (구간 [11:50, 13:00) 동안 OFF)
# 카메라 꺼짐(OFF) 판별 신호: 실측 확인됨
#   OFF = 버튼에 'text-red' 클래스 + 빗금 아이콘(path가 아래 값으로 시작)
#   ON  = 'text-red' 없음 (초록 text-[#61986A]) + 일반 카메라 아이콘
OFF_ICON_PREFIX = "M2.707 2.293"

# ── 카메라 장치 자동 선택 ──
# ZEP은 카메라를 다시 켤 때 기본 카메라(MacBook 카메라)로 돌아가는 경우가 있다.
# 카메라를 켠 직후 장치 선택 드롭다운에서 아래 이름이 들어간 장치를 자동 선택한다.
# (부분 일치, 빈 문자열 "" 로 두면 이 기능을 끔)
PREFERRED_CAMERA = "OBS Virtual Camera"
# 카메라 버튼 오른쪽의 장치 선택 드롭다운 버튼 (카메라 버튼과 같은 MediaDeviceControl 그룹 안)
CAMERA_DROPDOWN_SEL = (
    'div[data-sentry-component="MediaDeviceControl"]:has([aria-label="카메라"]) '
    '[aria-haspopup="menu"]'
)

DEBUG_PORT = 9222  # Chrome 원격 디버깅 포트
# 이 앱 전용 Chrome 프로필 폴더 (자동 생성). 평소 쓰는 Chrome과 충돌하지 않도록 분리.
# 여기서 한 번 Google 로그인하면 이 폴더에 세션이 남아 다음 실행 때 유지됨.
# 실행 위치(cwd)가 아니라 이 스크립트가 있는 폴더 기준 — 더블클릭/런처 실행에도 안전
CHROME_PROFILE_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "chrome_profile"
)

IS_MAC = sys.platform == "darwin"
IS_WINDOWS = sys.platform.startswith("win")


def _chrome_candidates():
    """OS별 Google Chrome 실행 파일 후보 경로 목록."""
    if IS_MAC:
        return [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            os.path.expanduser(
                "~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
            ),
        ]
    if IS_WINDOWS:
        # 환경변수는 시스템 언어/드라이브 구성에 따라 달라지므로 그대로 조합해 쓴다
        bases = [
            os.environ.get("PROGRAMFILES", r"C:\Program Files"),
            os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
            os.environ.get("LOCALAPPDATA", ""),
        ]
        return [
            os.path.join(b, "Google", "Chrome", "Application", "chrome.exe")
            for b in bases
            if b
        ]
    # Linux 등
    return [
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/opt/google/chrome/chrome",
    ]


def _chrome_from_registry():
    """Windows 레지스트리에서 Chrome 설치 경로를 조회 (실패하면 None)."""
    if not IS_WINDOWS:
        return None
    try:
        import winreg  # Windows 전용 표준 모듈
    except ImportError:
        return None
    key_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(root, key_path) as k:
                path = winreg.QueryValue(k, None)
                if path and os.path.exists(path):
                    return path
        except OSError:
            continue
    return None


def find_chrome():
    """설치된 Google Chrome 실행 파일 경로를 반환 (없으면 None). macOS/Windows/Linux 지원."""
    for path in _chrome_candidates():
        if os.path.exists(path):
            return path
    from_reg = _chrome_from_registry()  # Windows: 비표준 위치 설치 대응
    if from_reg:
        return from_reg
    # 마지막 폴백: PATH에서 탐색
    for name in ("google-chrome", "google-chrome-stable", "chrome", "chrome.exe"):
        found = shutil.which(name)
        if found:
            return found
    return None


# ──────────────────────────────────────────────────────────────


def next_target(now: datetime, minutes):
    """now 이후의 가장 가까운 클릭 목표 시각(오늘의 매시 minutes 중)을 반환."""
    candidates = []
    for hour in range(0, 24):
        for minute in minutes:
            t = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if t > now:
                candidates.append(t)
    return min(candidates) if candidates else None


def desired_camera_on(t: datetime) -> bool:
    """시각 t에서 카메라가 켜져 있어야 하면 True.
    규칙: :00~:50 켜짐, :50~:00 꺼짐. 단, 점심 구간 [11:50, 13:00)은 꺼둠."""
    hm = (t.hour, t.minute)
    if LUNCH_START <= hm < LUNCH_END:  # 점심 → 꺼둠 (12:00 자동 건너뜀)
        return False
    return t.minute < 50


def camera_button(page, timeout=15000):
    """현재 페이지에서 카메라 버튼을 찾아 locator 반환 (못 찾으면 None).

    ZEP이 프론트엔드를 바꿔도 견디도록 여러 셀렉터를 순서대로 시도한다.
    """
    deadline = time.time() + timeout / 1000
    while True:
        for sel in CAMERA_SELECTORS:
            try:
                btn = page.locator(sel).first
                if btn.count() > 0 and btn.is_visible():
                    return btn
            except Exception:
                continue
        if time.time() >= deadline:
            return None
        time.sleep(0.3)


def read_camera_state(page):
    """카메라 버튼 상태를 읽어 True(켜짐)/False(꺼짐)/None(판별불가) 반환."""
    try:
        btn = camera_button(page)
        if btn is None:
            return None
        pressed = btn.get_attribute("aria-pressed")
        if pressed in ("true", "false"):  # 신호 0: aria-pressed (가장 정확)
            return pressed == "true"
        # aria-pressed가 없으면 예전 신호로 폴백
        tokens = (btn.get_attribute("class") or "").split()
        has_red = "text-red" in tokens  # 신호 1: 빨강 토큰 = 꺼짐
        d = btn.locator("path").first.get_attribute("d") or ""
        has_off_icon = d.startswith(OFF_ICON_PREFIX)  # 신호 2: 빗금 아이콘 = 꺼짐
        is_off = has_red or has_off_icon
        return not is_off
    except Exception:
        return None


class Worker:
    """별도 스레드에서 Playwright 브라우저와 스케줄 루프를 담당."""

    def __init__(self, msg_queue: queue.Queue):
        self.q = msg_queue
        self.start_event = threading.Event()  # "스케줄 시작"
        self.stop_event = threading.Event()  # "중지" (루프만 멈춤)
        self.shutdown_event = threading.Event()  # 앱 종료 (워커+Chrome 정리)
        self.thread = None
        self.chrome_proc = None
        self.url = ""

    # ── GUI → 워커 명령 ──
    def open_browser(self, url):
        self.url = url
        self.start_event.clear()
        self.stop_event.clear()
        self.shutdown_event.clear()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def begin_schedule(self):
        self.start_event.set()

    def stop(self):
        """스케줄 루프만 멈춤 (브라우저 연결은 유지 → 다시 시작 가능)."""
        self.stop_event.set()

    def shutdown(self):
        """앱 종료: 워커를 끝내고 우리가 띄운 Chrome도 정리."""
        self.shutdown_event.set()
        self.stop_event.set()
        self.start_event.set()
        self._kill_chrome()

    def _kill_chrome(self):
        """띄운 Chrome을 정리한다.

        먼저 정상 종료(terminate)를 시도하고 잠시 기다린다. Windows에서 곧바로
        강제 종료하면 다음 실행 때 "Chrome이 올바르게 종료되지 않았습니다"
        복원 프롬프트가 떠서 탭이 중복될 수 있기 때문이다.
        """
        if self.chrome_proc is None:
            return
        proc = self.chrome_proc
        self.chrome_proc = None
        try:
            proc.terminate()
        except Exception:
            return
        try:
            proc.wait(timeout=5)  # 정상 종료 대기
        except Exception:
            try:
                proc.kill()  # 그래도 안 죽으면 강제 종료
            except Exception:
                pass

    def is_alive(self):
        return self.thread is not None and self.thread.is_alive()

    # ── 워커 → GUI 메시지 ──
    def _log(self, text):
        self.q.put(("log", text))

    def _status(self, text):
        self.q.put(("status", text))

    def _state(self, name):
        self.q.put(("state", name))

    # ── 실제 Chrome 실행 + CDP 연결 ──
    def _launch_chrome(self):
        chrome = find_chrome()
        if not chrome:
            self._log("Google Chrome을 찾지 못했습니다. Chrome을 설치해 주세요.")
            return False
        os.makedirs(CHROME_PROFILE_DIR, exist_ok=True)
        self._log("Chrome을 디버그 모드로 실행합니다...")
        self.chrome_proc = subprocess.Popen(
            [
                chrome,
                f"--remote-debugging-port={DEBUG_PORT}",
                f"--user-data-dir={CHROME_PROFILE_DIR}",
                "--no-first-run",
                "--no-default-browser-check",
                "--use-fake-ui-for-media-stream",  # 카메라·마이크 권한 자동 허용
                self.url,
            ]
        )
        # 디버그 엔드포인트가 열릴 때까지 대기 (최대 ~30초)
        for _ in range(60):
            if self.shutdown_event.is_set():
                return False
            try:
                with urllib.request.urlopen(
                    f"http://localhost:{DEBUG_PORT}/json/version", timeout=1
                ) as r:
                    if r.status == 200:
                        return True
            except Exception:
                pass
            self.shutdown_event.wait(0.5)
        self._log("Chrome 디버그 포트에 연결하지 못했습니다.")
        return False

    def _pick_zep_page(self, context):
        """ZEP 탭을 찾고, 없으면 새 탭에서 URL을 연다."""
        for pg in context.pages:
            try:
                if "zep.us" in (pg.url or ""):
                    return pg
            except Exception:
                continue
        page = context.pages[0] if context.pages else context.new_page()
        try:
            if "zep.us" not in (page.url or ""):
                page.goto(self.url)
        except Exception as e:
            self._log(f"페이지 접속 오류: {e}")
        return page

    # ── 워커 본체 ──
    def _run(self):
        try:
            if not self._launch_chrome():
                self._state("stopped")
                self._status("종료됨")
                return

            with sync_playwright() as p:
                browser = p.chromium.connect_over_cdp(f"http://localhost:{DEBUG_PORT}")
                context = (
                    browser.contexts[0] if browser.contexts else browser.new_context()
                )
                try:
                    context.grant_permissions(
                        ["camera", "microphone"], origin="https://zep.us"
                    )
                except Exception:
                    pass

                page = self._pick_zep_page(context)
                self._log("Chrome에 연결되었습니다. 로그인·입장을 진행하세요.")

                # 앱이 종료될 때까지: 시작 대기 → 루프 → (중지/종료시각) → 다시 시작 대기
                while not self.shutdown_event.is_set():
                    self.stop_event.clear()
                    self.start_event.clear()
                    self._status("로그인·입장 후 '스케줄 시작'을 누르세요.")
                    self._state("ready_to_start")

                    # 시작 신호 대기
                    while not self.start_event.is_set():
                        if self.shutdown_event.wait(0.2):
                            break
                    if self.shutdown_event.is_set():
                        break

                    self._log("스케줄을 시작합니다.")
                    self._status("스케줄 동작 중")
                    self._state("running")
                    result = self._loop(page)
                    if result == "quit":
                        self._log("프로그램을 종료합니다.")
                        self.shutdown_event.set()
                        self.q.put(("quit", None))  # GUI에 종료 요청
                        break
                    self._log("스케줄을 멈췄습니다.")
        except Exception as e:
            self._log(f"오류로 종료되었습니다: {e}")
        finally:
            self._kill_chrome()
            self._status("종료됨")
            self._state("stopped")

    def _loop(self, page):
        """스케줄 루프. 종료 시각 도달로 끝나면 "quit", 그 외(중지 등)는 None 반환."""
        now = datetime.now()
        end_time = now.replace(
            hour=END_TIME[0], minute=END_TIME[1], second=0, microsecond=0
        )
        quit_time = now.replace(
            hour=QUIT_TIME[0], minute=QUIT_TIME[1], second=0, microsecond=0
        )

        # 이미 종료 시각(19:00)이 지난 뒤 시작한 경우 → 아무 동작 없이 대기 상태로
        if now >= quit_time:
            self._log(
                f"이미 종료 시각({quit_time:%H:%M})이 지났습니다 — 아무 동작도 하지 않습니다."
            )
            return None

        # 초기 동기화: 시작 시각이 종료 전이면 현재 시간대에 맞는 상태로 즉시 맞춤
        if now <= end_time:
            want = desired_camera_on(now)
            self._log(
                f"[초기 동기화] 현재 시간대 카메라는 {'켜짐' if want else '꺼짐'}이어야 합니다."
            )
            self._set_camera(page, want)

        while not self.stop_event.is_set():
            target = next_target(datetime.now(), CLICK_MINUTES)
            if target is None or target > end_time:
                self._log(f"마지막 동작({end_time:%H:%M})을 마쳤습니다.")
                break

            want = desired_camera_on(target)
            self._status(f"다음 동작 {target:%H:%M} → {'켜기' if want else '끄기'}")
            self._log(
                f"다음 동작 예정: {target:%H:%M} → 카메라 {'켜기' if want else '끄기'} (대기 중...)"
            )

            # target 까지 잘게 쪼개 대기 (중지 반응성 확보)
            while not self.stop_event.is_set():
                remaining = (target - datetime.now()).total_seconds()
                if remaining <= 0:
                    break
                self.stop_event.wait(min(remaining, 1))
            if self.stop_event.is_set():
                break

            self._set_camera(page, want)

            # 같은 분 중복 동작 방지: 다음 분으로 넘어갈 때까지 대기
            self.stop_event.wait(max(0, 60 - datetime.now().second) + 1)

        if self.stop_event.is_set():
            return None  # 사용자가 중지 → 대기 상태로

        # ── 여기부터 종료 감시 (마지막 동작을 마친 뒤에만 도달) ──
        return self._wait_until_quit(quit_time)

    def _wait_until_quit(self, quit_time):
        """종료 시각까지 대기. 도달하면 "quit", 중지되면 None.

        반복 확인(폴링) 없이 남은 시간만큼 한 번에 대기한다.
        중지/종료 시 stop_event가 즉시 깨우고, 절전 등으로 일찍 깨면 남은 만큼 다시 대기한다.
        """
        self._log(f"{quit_time:%H:%M}에 프로그램을 종료합니다. (대기 중...)")
        self._status(f"{quit_time:%H:%M} 종료 대기 중")
        while not self.stop_event.is_set():
            remaining = (quit_time - datetime.now()).total_seconds()
            if remaining <= 0:
                break
            self.stop_event.wait(remaining)  # 단일 대기 — 도달 시각에 깨어남
        if self.stop_event.is_set():
            self._log("종료 대기를 취소했습니다.")
            return None
        return "quit"

    def _set_camera(self, page, desired):
        """카메라를 desired(True=켜짐) 상태로 만든다. 이미 맞으면 클릭하지 않음."""
        label = "켜짐(ON)" if desired else "꺼짐(OFF)"
        state = read_camera_state(page)
        if state is None:
            self._log(
                "카메라 상태를 읽지 못해 이번 동작은 건너뜁니다 (입장 확인 필요)."
            )
            return
        if state == desired:
            self._log(f"이미 {label} 상태 — 클릭하지 않습니다.")
            return
        try:
            btn = camera_button(page)
            if btn is None:
                self._log("카메라 버튼을 찾지 못했습니다 — 이번 동작을 건너뜁니다.")
                return
            btn.click()
        except Exception as e:
            self._log(f"클릭 중 오류: {e}")
            return
        # 자가 검증: 상태가 반영될 때까지 최대 ~6초 폴링
        # (카메라 켜짐/꺼짐이 DOM에 반영되는 데 시간이 걸리므로 단발 확인 X)
        after = self._wait_camera_state(page, desired)
        if after == desired:
            self._log(
                f"[{datetime.now():%H:%M:%S}] 카메라를 {label}(으)로 변경했습니다."
            )
            if desired:  # 켰을 때만 — ZEP이 기본 카메라로 되돌리는 문제 보정
                self._ensure_preferred_camera(page)
        else:
            now_txt = "ON" if after else "OFF" if after is not None else "불명"
            self._log(
                f"⚠ 카메라를 {label}(으)로 바꿨지만 약 6초 안에 확인되지 않았습니다 "
                f"(현재={now_txt}). 실제로는 바뀌었을 수 있으니 다음 동작에서 보정됩니다."
            )

    def _ensure_preferred_camera(self, page):
        """카메라가 켜진 뒤, 장치 선택 드롭다운에서 PREFERRED_CAMERA를 선택한다.

        이미 선택돼 있으면 메뉴만 열었다 닫고, 다르면 클릭한 뒤 다시 열어 체크 상태로 검증한다.
        """
        if not PREFERRED_CAMERA:
            return
        try:
            trigger = page.locator(CAMERA_DROPDOWN_SEL).first
            trigger.wait_for(state="visible", timeout=10000)
        except Exception:
            self._log("카메라 장치 선택 버튼을 찾지 못해 장치 확인을 건너뜁니다.")
            return

        item = self._open_device_item(page, trigger)
        if item is None:
            return
        if item.get_attribute("aria-checked") == "true":
            self._close_menu(page)
            self._log(f"카메라 장치: 이미 '{PREFERRED_CAMERA}' 선택됨.")
            return

        try:
            item.click()  # 선택하면 메뉴는 자동으로 닫힘
        except Exception as e:
            self._close_menu(page)
            self._log(f"카메라 장치 선택 중 오류: {e}")
            return
        page.wait_for_timeout(1000)  # 장치 전환 반영 대기

        # 검증: 다시 열어 체크 상태 확인
        item = self._open_device_item(page, trigger)
        if item is None:
            return
        ok = item.get_attribute("aria-checked") == "true"
        self._close_menu(page)
        if ok:
            self._log(f"카메라 장치를 '{PREFERRED_CAMERA}'(으)로 전환했습니다.")
        else:
            self._log(f"⚠ '{PREFERRED_CAMERA}' 선택이 확인되지 않았습니다. 직접 확인해 주세요.")

    def _open_device_item(self, page, trigger):
        """드롭다운을 열고 선호 장치 메뉴 항목 locator 반환. 없으면 메뉴를 닫고 None."""
        try:
            trigger.click()
            menu = page.locator('[role="menu"]').first
            menu.wait_for(state="visible", timeout=3000)
            radios = menu.locator('[role="menuitemradio"]')
            item = radios.filter(has_text=PREFERRED_CAMERA).first
            if item.count() == 0:
                names = [t.strip() for t in radios.all_inner_texts()]
                self._close_menu(page)
                self._log(
                    f"⚠ 카메라 장치 목록에 '{PREFERRED_CAMERA}'가 없습니다 "
                    f"(현재 목록: {', '.join(names) or '없음'}). OBS 가상 카메라가 시작돼 있는지 확인하세요."
                )
                return None
            return item
        except Exception as e:
            self._close_menu(page)
            self._log(f"카메라 장치 메뉴를 여는 중 오류: {e}")
            return None

    def _close_menu(self, page):
        """열려 있는 드롭다운 메뉴가 있을 때만 Escape로 닫는다."""
        try:
            if page.locator('[role="menu"]').count() > 0:
                page.keyboard.press("Escape")
                page.wait_for_timeout(200)
        except Exception:
            pass

    def _wait_camera_state(self, page, desired, attempts=20, interval=0.3):
        """클릭 후 카메라 상태가 desired로 반영될 때까지 폴링. 마지막으로 읽은 상태 반환."""
        last = read_camera_state(page)
        for _ in range(attempts):
            if last == desired or self.stop_event.is_set():
                return last
            self.stop_event.wait(interval)  # 중지에도 반응하는 대기
            last = read_camera_state(page)
        return last


class App:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.worker = Worker(self.q)

        root.title("ZEP 카메라 자동 클릭")
        root.geometry("520x520")
        root.minsize(460, 460)

        pad = {"padx": 10, "pady": 6}
        frm = ttk.Frame(root, padding=12)
        frm.pack(fill="both", expand=True)

        # URL
        ttk.Label(frm, text="ZEP 링크").grid(row=0, column=0, sticky="w")
        self.url_var = tk.StringVar()
        self.url_entry = ttk.Entry(frm, textvariable=self.url_var, width=48)
        self.url_entry.grid(row=1, column=0, columnspan=3, sticky="we", pady=(0, 8))

        # 동작 규칙 안내 (코드 상수 기반 — 바꾸려면 app.py 상단 상수 수정)
        rule_text = (
            f"규칙: 매시 :{CLICK_MINUTES[0]:02d} 켜기 / :{CLICK_MINUTES[-1]:02d} 끄기"
            f"   ·   점심 {LUNCH_START[0]:02d}:{LUNCH_START[1]:02d}~"
            f"{LUNCH_END[0]:02d}:{LUNCH_END[1]:02d} 꺼둠"
            f"   ·   {END_TIME[0]:02d}:{END_TIME[1]:02d} 마지막 동작"
            f"   ·   {QUIT_TIME[0]:02d}:{QUIT_TIME[1]:02d} 프로그램 종료"
        )
        ttk.Label(frm, text=rule_text, foreground="#555").grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(0, 8)
        )

        # 버튼들
        btns = ttk.Frame(frm)
        btns.grid(row=4, column=0, columnspan=3, sticky="we", pady=(2, 8))
        self.open_btn = ttk.Button(btns, text="브라우저 열기", command=self.on_open)
        self.open_btn.pack(side="left")
        self.start_btn = ttk.Button(
            btns, text="스케줄 시작", command=self.on_start, state="disabled"
        )
        self.start_btn.pack(side="left", padx=6)
        self.stop_btn = ttk.Button(
            btns, text="중지", command=self.on_stop, state="disabled"
        )
        self.stop_btn.pack(side="left")

        # 상태
        self.status_var = tk.StringVar(
            value="대기 중 — 링크를 입력하고 '브라우저 열기'를 누르세요."
        )
        ttk.Label(frm, textvariable=self.status_var, foreground="#3355bb").grid(
            row=5, column=0, columnspan=3, sticky="w", pady=(0, 6)
        )

        # 로그
        ttk.Label(frm, text="로그").grid(row=6, column=0, sticky="w")
        self.log_box = scrolledtext.ScrolledText(
            frm, height=12, state="disabled", wrap="word"
        )
        self.log_box.grid(row=7, column=0, columnspan=3, sticky="nsew")

        frm.columnconfigure(0, weight=1)
        frm.columnconfigure(1, weight=1)
        frm.rowconfigure(7, weight=1)

        self._enable_clipboard()

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.poll_queue()

    # ── 클립보드(복사/붙여넣기) ──
    def _enable_clipboard(self):
        """입력 위젯에 복사/붙여넣기 단축키 + 우클릭 메뉴 추가.

        macOS는 ttk.Entry가 Cmd+V를 기본 인식하지 않아 직접 바인딩이 필요하고,
        Windows/Linux는 Ctrl 조합을 쓰므로 OS에 맞는 수식키를 사용한다.
        """
        # macOS는 Command, 그 외는 Control (양쪽 다 걸어도 무해하므로 함께 바인딩)
        modifiers = ("Command", "Control") if IS_MAC else ("Control",)
        actions = {
            "v": "<<Paste>>",
            "c": "<<Copy>>",
            "x": "<<Cut>>",
        }
        for cls in ("TEntry", "Entry", "TSpinbox", "Spinbox", "Text"):
            for mod in modifiers:
                for key, event in actions.items():
                    for k in (key, key.upper()):
                        self.root.bind_class(
                            cls,
                            f"<{mod}-{k}>",
                            lambda e, ev=event: (
                                e.widget.event_generate(ev),
                                "break",
                            )[1],
                        )
                for k in ("a", "A"):  # 전체 선택
                    self.root.bind_class(cls, f"<{mod}-{k}>", self._select_all)

        # 우클릭(또는 Ctrl-클릭) 붙여넣기 메뉴 — URL 입력란
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(
            label="붙여넣기",
            command=lambda: self.url_entry.event_generate("<<Paste>>"),
        )
        menu.add_command(
            label="복사", command=lambda: self.url_entry.event_generate("<<Copy>>")
        )
        menu.add_command(
            label="잘라내기", command=lambda: self.url_entry.event_generate("<<Cut>>")
        )
        menu.add_separator()
        menu.add_command(
            label="전체 선택",
            command=lambda: (
                self.url_entry.select_range(0, "end"),
                self.url_entry.icursor("end"),
            ),
        )

        def show_menu(event):
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()

        for seq in ("<Button-2>", "<Button-3>", "<Control-Button-1>"):
            self.url_entry.bind(seq, show_menu)

    def _select_all(self, event):
        w = event.widget
        try:
            w.select_range(0, "end")
            w.icursor("end")
        except Exception:
            pass
        return "break"

    # ── 버튼 핸들러 ──
    def on_open(self):
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("링크 필요", "ZEP 링크를 입력해 주세요.")
            return
        self.open_btn.config(state="disabled")
        self.url_entry.config(state="disabled")
        self.log("브라우저를 여는 중...")
        self.worker.open_browser(url)

    def on_start(self):
        self.start_btn.config(state="disabled")
        self.worker.begin_schedule()

    def on_stop(self):
        self.worker.stop()
        self.log("중지 요청됨...")

    def on_close(self):
        if self.worker.is_alive():
            self.worker.shutdown()
            self.root.after(800, self.root.destroy)
        else:
            self.worker.shutdown()
            self.root.destroy()

    # ── 유틸 ──
    def log(self, text):
        self.log_box.config(state="normal")
        self.log_box.insert("end", f"{datetime.now():%H:%M:%S}  {text}\n")
        self.log_box.see("end")
        self.log_box.config(state="disabled")

    def poll_queue(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self.log(payload)
                elif kind == "status":
                    self.status_var.set(payload)
                elif kind == "state":
                    self._apply_state(payload)
                elif kind == "quit":  # 종료 시각 도달 → 앱 완전 종료
                    self.on_close()
                    return
        except queue.Empty:
            pass
        self.root.after(100, self.poll_queue)

    def _apply_state(self, name):
        if name == "ready_to_start":
            self.start_btn.config(state="normal")
            self.stop_btn.config(state="disabled")
        elif name == "running":
            self.start_btn.config(state="disabled")
            self.stop_btn.config(state="normal")
        elif name == "stopped":
            self.start_btn.config(state="disabled")
            self.stop_btn.config(state="disabled")
            self.open_btn.config(state="normal")
            self.url_entry.config(state="normal")


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
