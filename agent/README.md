# 임직원 매수/매도 현황 일일 보고 에이전트

매일 아침 8시(KST), 지정한 기업에 대해 **전일 접수된 임원·주요주주의 특정증권 매수/매도(소유 증감) 공시**를
금융감독원 **DART** 에서 조회하여 **Slack DM** 으로 보고합니다.

- 데이터 출처: DART OpenAPI 「임원ㆍ주요주주 특정증권등 소유상황보고서」(`elestock.json`)
- 의존성: 없음 (Python 3 표준 라이브러리만 사용)
- 실행: cron 등 스케줄러로 매일 08:00 KST 실행

## 1. 사전 준비

### DART 인증키 발급
1. https://opendart.fss.or.kr 회원가입 → 인증키 신청
2. 발급받은 키를 `DART_API_KEY` 로 사용

### 대상 기업 고유번호(CORP_CODE) 확인
DART 는 종목코드가 아닌 8자리 고유번호를 사용합니다. 다음 명령으로 조회하세요.

```bash
DART_API_KEY=발급키 python3 dart_insider_report.py --resolve "삼성전자"
# 00126380  삼성전자  (종목 005930)
```

### 오늘 매매 공시가 있는 기업 전체 목록 (대상 후보 탐색)
오늘(또는 지정일) 임원·주요주주 매매 공시가 접수된 **모든 기업**을 한눈에 봅니다.
여기서 관심 기업의 고유번호를 골라 `CORP_CODE` 에 등록하세요.

```bash
DART_API_KEY=발급키 python3 dart_insider_report.py --today-list           # KST 기준 오늘
DART_API_KEY=발급키 python3 dart_insider_report.py --today-list 20260622  # 특정일 지정
# === 2026-06-22 임원·주요주주 매매 공시 기업 (12개사 / 공시 27건) ===
# 고유번호    종목코드  공시  기업명
# 00126380  005930    3  삼성전자
# ...
```

### Slack 전송 설정 (둘 중 하나)
- **봇 토큰**: Slack 앱 생성 → `chat:write` 권한 → `xoxb-` 토큰을 `SLACK_BOT_TOKEN` 으로 사용.
  본인 DM 으로 받으려면 `SLACK_CHANNEL=U0AQ01K6GCB` (기본값). 봇이 DM 을 보낼 수 있도록
  앱과 한 번 대화를 시작해 두세요.
- **Incoming Webhook**: `SLACK_WEBHOOK_URL` 설정 (특정 채널 고정 전송).

## 2. 설정

```bash
cp .env.example .env   # 값 채우기
```

`.env` 는 커밋하지 마세요 (`.gitignore` 에 등록되어 있습니다).

## 3. 실행

```bash
# 환경변수 로드 후 실행
set -a; . ./.env; set +a
python3 dart_insider_report.py
```

토큰/웹훅을 설정하지 않으면 보고 내용을 **표준출력으로만** 출력합니다(dry-run). 먼저 dry-run 으로
형식을 확인한 뒤 Slack 설정을 추가하는 것을 권장합니다.

## 4. 매일 8시 스케줄 등록 (cron)

`crontab -e` 에 다음을 추가합니다. (서버 타임존이 KST 가 아니면 `CRON_TZ` 로 보정)

```cron
CRON_TZ=Asia/Seoul
0 8 * * * cd /path/to/agent && set -a && . ./.env && set +a && /usr/bin/python3 dart_insider_report.py >> report.log 2>&1
```

## 동작 방식 / 참고

- **기준일**: `REPORT_DATE` 미설정 시 KST 기준 전일. 공시 **접수일자(rcept_dt)** 기준으로 필터링합니다.
  (실제 매매 체결일과 접수일은 다를 수 있습니다. 주말·공휴일 다음 영업일에는 누적 공시가 함께 잡힐 수 있어
  접수일 기준이 보고 누락을 방지하는 데 유리합니다.)
- **매수/매도 판단**: `sp_stock_lmp_irds_cnt`(특정증권 소유 증감수)가 양수면 매수(증가), 음수면 매도(감소).
- 해당 일자에 공시가 없으면 "공시 없음" 으로 보고합니다.
- `CORP_CODE` 에 쉼표로 여러 기업을 지정하면 기업별로 각각 보고합니다.
