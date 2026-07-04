-- ZEP 카메라 자동 클릭 GUI 런처
-- 더블클릭하면 터미널 없이 GUI(app.py)만 실행한다.
-- 파이썬은 백그라운드(&)로 띄우고 로그는 /tmp/zep_app.log 로 보낸다.

set pyBin to "/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
set projDir to "/Users/gimgyeong-won/Desktop/code/zep 버튼 클릭하기 파이선"

do shell script "cd " & quoted form of projDir & " && " & quoted form of pyBin & " app.py > /tmp/zep_app.log 2>&1 &"
