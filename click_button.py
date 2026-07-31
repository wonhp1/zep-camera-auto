"""
ZEP 스페이스 하단 카메라 버튼을 매시 정각(:00)과 50분(:50)마다 자동으로 클릭하는 프로그램.

흐름:
  1. 프로그램 실행 → ZEP 링크 입력
  2. 열린 브라우저 창에서 직접 로그인 → 자동으로 스페이스 입장
  3. 준비되면 콘솔에서 Enter → 스케줄 루프 시작
  4. 매 :00 / :50 에 카메라 버튼 1회 클릭(토글), 오후 7시(19:00) 넘으면 종료

종료하려면 언제든 Ctrl+C.
"""

from datetime import datetime, timedelta
import sys
import time

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

# ─────────────────────────── 설정값 ───────────────────────────
# 카메라 버튼 셀렉터 (앞에서부터 순서대로 시도)
#  1) aria-label="카메라"  — 현재 ZEP 방식
#  2) MediaDeviceButton 의 2번째 — 예전 방식 폴백(1번=마이크)
CAMERA_SELECTORS = [
    '[aria-label="카메라"]',
    '[data-sentry-element="MediaDeviceButton"] >> nth=1',
]
CLICK_MINUTES = [0, 50]  # 매시 정각·50분
END_HOUR = 19  # 오후 7시(19:00)까지만 클릭
PROFILE_DIR = "browser_profile"  # 로그인 세션이 저장되는 폴더 (자동 생성)
# ──────────────────────────────────────────────────────────────


def next_target(now: datetime) -> datetime:
    """now 이후의 가장 가까운 클릭 목표 시각(오늘의 매시 :00/:50 중)을 반환."""
    candidates = []
    for hour in range(0, 24):
        for minute in CLICK_MINUTES:
            t = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if t > now:
                candidates.append(t)
    return min(candidates) if candidates else None


def sleep_until(target: datetime):
    """target 시각까지 잘게 쪼개 대기 (Ctrl+C 반응성 확보)."""
    while True:
        remaining = (target - datetime.now()).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 10))


def click_camera(page) -> bool:
    """카메라 버튼을 클릭. 성공하면 True, 버튼을 못 찾으면 False."""
    deadline = time.time() + 15
    while True:
        for sel in CAMERA_SELECTORS:
            try:
                camera = page.locator(sel).first
                if camera.count() > 0 and camera.is_visible():
                    camera.click()
                    return True
            except Exception:
                continue
        if time.time() >= deadline:
            return False
        time.sleep(0.3)


def main():
    url = input("ZEP 링크를 붙여넣고 Enter: ").strip()
    if not url:
        print("링크가 입력되지 않았습니다. 종료합니다.")
        sys.exit(1)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            PROFILE_DIR,
            headless=False,
            args=["--use-fake-ui-for-media-stream"],  # 카메라·마이크 권한 자동 허용
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(url)

        print()
        print("브라우저에서 로그인하고 스페이스에 입장하세요.")
        input("준비가 되면 여기서 Enter를 누르세요: ")

        end_time = datetime.now().replace(
            hour=END_HOUR, minute=0, second=0, microsecond=0
        )
        print(f"\n스케줄 시작. 매시 {CLICK_MINUTES} 분에 카메라 버튼을 클릭합니다.")
        print(f"오후 {END_HOUR}:00({end_time:%H:%M}) 이후로는 종료합니다.\n")

        try:
            while True:
                target = next_target(datetime.now())
                if target is None or target > end_time:
                    print(
                        f"종료 시각({end_time:%H:%M})을 지나 더 이상 클릭하지 않습니다. 종료합니다."
                    )
                    break

                print(f"다음 클릭 예정: {target:%H:%M} (대기 중...)")
                sleep_until(target)

                if click_camera(page):
                    print(f"[{datetime.now():%H:%M:%S}] 카메라 버튼 클릭 완료.")
                else:
                    print(
                        f"[{datetime.now():%H:%M:%S}] 카메라 버튼을 찾지 못했습니다. "
                        "입장이 풀렸을 수 있습니다. 브라우저에서 다시 입장한 뒤 잠시 기다려 주세요."
                    )

                # 같은 분에 중복 클릭하지 않도록 다음 분으로 살짝 넘김
                time.sleep(60 - datetime.now().second + 1)
        except KeyboardInterrupt:
            print("\n사용자가 중단했습니다(Ctrl+C). 종료합니다.")
        finally:
            context.close()


if __name__ == "__main__":
    main()
