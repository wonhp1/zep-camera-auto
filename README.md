# ZEP 카메라 자동 클릭

ZEP 메타버스 스페이스의 하단 **카메라 버튼**을 스케줄에 맞춰 자동으로 켜고/끄는 프로그램.
Playwright를 실제 Chrome(디버그 모드)에 연결해 Google 로그인 차단 없이 동작한다.

## 기능

- 매시 **:00 켜기 / :50 끄기** (정각~50분 켜짐, 50분~정각 꺼짐)
- **점심시간 11:50~13:00** 동안 카메라 꺼둠 (12:00은 자동으로 안 켬)
- **18:50 마지막 동작 후 종료**
- 무조건 토글이 아니라 **현재 on/off 상태를 확인**하고 필요할 때만 클릭 (클릭 후 자가 검증)
- Tkinter GUI (링크 입력 → 브라우저 열기 → 스케줄 시작/중지)

> 규칙(종료 시각·점심시간 등)은 `app.py` 상단 상수에서 조정.

## 요구 사항

- macOS / Windows / Linux + Google Chrome 설치
- Python 3.x, `playwright` 패키지

```bash
pip install -r requirements.txt
playwright install chromium   # (CLI 버전용 — GUI는 시스템 Chrome 사용)
```

## 사용법

macOS / Windows / Linux 모두 지원한다.

### GUI — macOS

```bash
python3 app.py
```

또는 `ZEP카메라.app`(더블클릭용 런처)을 빌드해서 사용 — 터미널 없이 GUI만 뜬다:

```bash
osacompile -o "ZEP카메라.app" launch_zep.applescript
```

### GUI — Windows

**`ZEP카메라_실행.bat` 더블클릭** — 콘솔 창 없이 GUI만 뜬다.
(또는 명령창에서 `pythonw app.py`)

> Chrome은 기본 설치 경로와 레지스트리에서 자동으로 찾는다.
> 파이썬 설치 시 **"Add Python to PATH"** 를 체크해야 런처가 동작한다.

1. ZEP 링크 입력 → **브라우저 열기**
2. 뜬 Chrome에서 직접 **Google 로그인 · 스페이스 입장**
   (전용 프로필 `chrome_profile/`에 저장되어 다음 실행부터 로그인 유지)
3. **스케줄 시작** → 규칙대로 카메라 자동 관리, **중지**로 멈춤

### CLI

```bash
python3 click_button.py
```

## 동작 원리 (핵심)

- **실제 Chrome + CDP 연결**: `chromium.launch()`(자동화 브라우저)는 Google이 "안전하지 않은 브라우저"로
  차단한다. 대신 진짜 Chrome을 `--remote-debugging-port`로 띄우고 `connect_over_cdp`로 연결하면
  `navigator.webdriver=false`라 차단되지 않는다.
- **전용 프로필(`--user-data-dir`)**: 평소 쓰는 Chrome과 별개 인스턴스로 띄워 충돌 방지.
- **카메라 버튼 탐색**: `aria-label="카메라"`를 우선 사용하고, 예전 방식
  (`[data-sentry-element="MediaDeviceButton"]`의 2번째)을 폴백으로 함께 시도한다.
- **카메라 상태 판별**: 버튼의 `text-red` 클래스(+빗금 아이콘) 유무로 on/off를 구분하고,
  클릭 후 상태가 반영될 때까지 폴링해 자가 검증한다.
- **크로스플랫폼**: Chrome 경로를 OS별 기본 위치와 Windows 레지스트리에서 자동 탐색.

## 파일 구성

| 파일                     | 설명                                       |
| ------------------------ | ------------------------------------------ |
| `app.py`                 | GUI 버전 (메인)                            |
| `click_button.py`        | CLI 버전                                   |
| `launch_zep.applescript` | 터미널 없이 GUI를 실행하는 macOS 런처 소스 |
| `ZEP카메라_실행.bat` | 콘솔 없이 GUI를 실행하는 Windows 런처 |
| `requirements.txt`       | 의존성                                     |

> `chrome_profile/`, `browser_profile/`는 로그인 세션이 담겨 **git에 포함하지 않는다**(.gitignore).
