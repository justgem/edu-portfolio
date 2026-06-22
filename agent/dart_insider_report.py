#!/usr/bin/env python3
"""전일 임직원(임원·주요주주) 매수/매도 현황 Slack 보고 에이전트.

DART OpenAPI 의 「임원ㆍ주요주주 특정증권등 소유상황보고서」(elestock.json) 를 조회해
지정한 기업에 대해 전일 접수된 공시를 필터링하고, 매수/매도(소유 증감) 현황을 정리하여
Slack 으로 보고한다.

표준 라이브러리만 사용하므로 별도 의존성 설치 없이 `python3 dart_insider_report.py` 로 실행된다.

필요 환경변수
  DART_API_KEY     (필수) opendart.fss.or.kr 에서 발급받은 인증키
  CORP_CODE        (필수) 8자리 DART 고유번호. 쉼표로 여러 기업 지정 가능. 예) 00126380
  SLACK_BOT_TOKEN  (선택) xoxb- 봇 토큰. 지정 시 chat.postMessage 로 전송
  SLACK_CHANNEL    (선택) 전송 대상 채널/사용자 ID. 기본값은 본인 DM(U0AQ01K6GCB)
  SLACK_WEBHOOK_URL(선택) Incoming Webhook URL. 토큰 대신 사용 가능
  REPORT_DATE      (선택) 조회 기준일(YYYYMMDD). 기본값은 KST 기준 전일

  토큰/웹훅 둘 다 없으면 보고 내용을 표준출력으로만 인쇄한다(dry-run).

기업 고유번호(CORP_CODE) 조회
  python3 dart_insider_report.py --resolve "삼성전자"

오늘(또는 지정일) 임원·주요주주 매매 공시가 접수된 기업 전체 목록
  python3 dart_insider_report.py --today-list           # KST 기준 오늘
  python3 dart_insider_report.py --today-list 20260622  # 특정일 지정
"""

import io
import json
import os
import sys
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree

KST = timezone(timedelta(hours=9))
DART_BASE = "https://opendart.fss.or.kr/api"
DEFAULT_SLACK_CHANNEL = "U0AQ01K6GCB"  # 본인 DM


def _http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "dart-insider-report/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def yesterday_kst():
    return (datetime.now(KST) - timedelta(days=1)).strftime("%Y%m%d")


def today_kst():
    return datetime.now(KST).strftime("%Y%m%d")


# 임원ㆍ주요주주 특정증권등 소유상황보고서 (지분공시 상세유형)
INSIDER_DETAIL_TY = "D002"


def fetch_disclosure_list(api_key, date_str):
    """list.json 으로 해당 일자에 접수된 임원·주요주주 매매 공시를 모두 조회.

    페이지네이션을 끝까지 따라가며 전체 공시 목록(list[dict])을 반환한다.
    """
    items = []
    page_no = 1
    while True:
        params = urllib.parse.urlencode(
            {
                "crtfc_key": api_key,
                "bgn_de": date_str,
                "end_de": date_str,
                "pblntf_detail_ty": INSIDER_DETAIL_TY,
                "page_no": page_no,
                "page_count": 100,
            }
        )
        raw = _http_get(f"{DART_BASE}/list.json?{params}")
        data = json.loads(raw.decode("utf-8"))
        status = data.get("status")
        if status == "013":  # 조회된 데이터가 없습니다
            break
        if status != "000":
            raise RuntimeError(f"DART API 오류 [{status}] {data.get('message')}")
        items.extend(data.get("list", []))
        if page_no >= int(data.get("total_page", 1)):
            break
        page_no += 1
    return items


def summarize_companies(disclosures):
    """공시 목록을 기업 단위로 묶어 (기업명, 고유번호, 종목코드, 건수) 리스트 반환."""
    by_corp = {}
    for d in disclosures:
        code = (d.get("corp_code") or "").strip()
        entry = by_corp.setdefault(
            code,
            {
                "corp_name": (d.get("corp_name") or "").strip(),
                "stock_code": (d.get("stock_code") or "").strip(),
                "count": 0,
            },
        )
        entry["count"] += 1
    rows = [
        (v["corp_name"], code, v["stock_code"], v["count"])
        for code, v in by_corp.items()
    ]
    # 공시 건수 많은 순 → 기업명 순
    rows.sort(key=lambda r: (-r[3], r[0]))
    return rows


def fetch_insider_reports(api_key, corp_code):
    """elestock.json 호출 → 보고 목록(list[dict]) 반환.

    데이터 없음(status 013)은 빈 리스트로, 그 외 오류는 예외로 처리한다.
    """
    params = urllib.parse.urlencode({"crtfc_key": api_key, "corp_code": corp_code})
    raw = _http_get(f"{DART_BASE}/elestock.json?{params}")
    data = json.loads(raw.decode("utf-8"))
    status = data.get("status")
    if status == "013":  # 조회된 데이터가 없습니다
        return []
    if status != "000":
        raise RuntimeError(f"DART API 오류 [{status}] {data.get('message')}")
    return data.get("list", [])


def _to_int(value):
    """'1,234' / '-' / '' 같은 DART 숫자 문자열을 int 로 변환."""
    if value is None:
        return 0
    text = str(value).replace(",", "").strip()
    if text in ("", "-"):
        return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def filter_by_date(reports, yyyymmdd):
    """접수일자(rcept_dt) 가 기준일과 일치하는 보고만 남긴다."""
    return [r for r in reports if str(r.get("rcept_dt", "")).strip() == yyyymmdd]


def _actor_label(item):
    """보고자의 직위/구분 라벨 생성."""
    parts = []
    ofcps = (item.get("isu_exctv_ofcps") or "").strip()
    if ofcps:
        parts.append(ofcps)
    if (item.get("isu_exctv_rgist_at") or "").strip() == "등기임원":
        parts.append("등기임원")
    main = (item.get("isu_main_shrholdr") or "").strip()
    if main and main != "-":
        parts.append(main)
    return ", ".join(parts) if parts else "-"


def build_report(corp_name, date_str, items):
    """Slack 마크다운 보고 문자열 생성."""
    pretty_date = f"{date_str[0:4]}-{date_str[4:6]}-{date_str[6:8]}"
    header = f"*📊 임직원 매매 현황 보고* — *{corp_name}* (전일 {pretty_date} 접수 공시)"

    if not items:
        return f"{header}\n\n해당 일자에 접수된 임원·주요주주 소유상황보고 공시가 없습니다."

    buy_cnt = sum(1 for it in items if _to_int(it.get("sp_stock_lmp_irds_cnt")) > 0)
    sell_cnt = sum(1 for it in items if _to_int(it.get("sp_stock_lmp_irds_cnt")) < 0)
    net = sum(_to_int(it.get("sp_stock_lmp_irds_cnt")) for it in items)

    lines = [
        header,
        "",
        f"보고 건수: *{len(items)}건*  |  매수(증가) {buy_cnt}건  |  매도(감소) {sell_cnt}건"
        f"  |  순증감 *{net:+,}주*",
        "",
        "| 보고자 | 구분 | 증감(주) | 매매 | 보유수(주) | 보유비율 |",
        "| --- | --- | ---: | :---: | ---: | ---: |",
    ]
    for it in items:
        irds = _to_int(it.get("sp_stock_lmp_irds_cnt"))
        if irds > 0:
            trade = "🟢 매수"
        elif irds < 0:
            trade = "🔴 매도"
        else:
            trade = "⚪ 변동없음"
        repror = (it.get("repror") or "-").strip()
        rate = (it.get("sp_stock_lmp_rate") or "-").strip()
        held = _to_int(it.get("sp_stock_lmp_cnt"))
        lines.append(
            f"| {repror} | {_actor_label(it)} | {irds:+,} | {trade} | {held:,} | {rate}% |"
        )

    lines.append("")
    lines.append("_출처: 금융감독원 DART 임원·주요주주 특정증권등 소유상황보고서_")
    return "\n".join(lines)


def send_to_slack(text):
    """봇 토큰 또는 웹훅으로 전송. 둘 다 없으면 표준출력(dry-run)."""
    bot_token = os.environ.get("SLACK_BOT_TOKEN")
    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    channel = os.environ.get("SLACK_CHANNEL", DEFAULT_SLACK_CHANNEL)

    if bot_token:
        payload = json.dumps({"channel": channel, "text": text, "mrkdwn": True}).encode("utf-8")
        req = urllib.request.Request(
            "https://slack.com/api/chat.postMessage",
            data=payload,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Authorization": f"Bearer {bot_token}",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        if not result.get("ok"):
            raise RuntimeError(f"Slack chat.postMessage 실패: {result.get('error')}")
        print(f"[slack] 전송 완료 → {channel}")
        return

    if webhook:
        payload = json.dumps({"text": text}).encode("utf-8")
        req = urllib.request.Request(
            webhook, data=payload, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
        print("[slack] 웹훅 전송 완료")
        return

    print("[dry-run] SLACK_BOT_TOKEN/SLACK_WEBHOOK_URL 미설정 — 아래 내용 출력만 합니다:\n")
    print(text)


def resolve_corp_code(api_key, name):
    """corpCode.zip 을 받아 기업명으로 8자리 고유번호를 조회한다."""
    params = urllib.parse.urlencode({"crtfc_key": api_key})
    raw = _http_get(f"{DART_BASE}/corpCode.xml?{params}")
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        xml_bytes = zf.read(zf.namelist()[0])
    root = ElementTree.fromstring(xml_bytes)
    matches = []
    for el in root.iter("list"):
        corp_name = (el.findtext("corp_name") or "").strip()
        if name in corp_name:
            matches.append(
                (
                    corp_name,
                    (el.findtext("corp_code") or "").strip(),
                    (el.findtext("stock_code") or "").strip(),
                )
            )
    # 상장사(종목코드 보유)를 우선 노출
    matches.sort(key=lambda m: (m[2] == "", m[0]))
    return matches


def main(argv):
    api_key = os.environ.get("DART_API_KEY")

    if len(argv) >= 2 and argv[1] == "--resolve":
        if not api_key:
            sys.exit("DART_API_KEY 환경변수가 필요합니다.")
        if len(argv) < 3:
            sys.exit('사용법: python3 dart_insider_report.py --resolve "기업명"')
        for corp_name, corp_code, stock in resolve_corp_code(api_key, argv[2]):
            tag = f"종목 {stock}" if stock else "비상장"
            print(f"{corp_code}  {corp_name}  ({tag})")
        return

    if len(argv) >= 2 and argv[1] == "--today-list":
        if not api_key:
            sys.exit("DART_API_KEY 환경변수가 필요합니다.")
        date_str = argv[2] if len(argv) >= 3 else today_kst()
        disclosures = fetch_disclosure_list(api_key, date_str)
        rows = summarize_companies(disclosures)
        pretty = f"{date_str[0:4]}-{date_str[4:6]}-{date_str[6:8]}"
        if not rows:
            print(f"{pretty} 접수된 임원·주요주주 매매 공시가 없습니다.")
            return
        print(f"=== {pretty} 임원·주요주주 매매 공시 기업 ({len(rows)}개사 / 공시 {len(disclosures)}건) ===")
        print(f"{'고유번호':>8}  {'종목코드':>6}  공시  기업명")
        for corp_name, corp_code, stock, count in rows:
            print(f"{corp_code:>8}  {stock or '-':>6}  {count:>3}  {corp_name}")
        return

    if not api_key:
        sys.exit("DART_API_KEY 환경변수가 필요합니다.")
    corp_codes = [c.strip() for c in os.environ.get("CORP_CODE", "").split(",") if c.strip()]
    if not corp_codes:
        sys.exit("CORP_CODE 환경변수가 필요합니다. (--resolve 로 조회 가능)")

    date_str = os.environ.get("REPORT_DATE") or yesterday_kst()

    for corp_code in corp_codes:
        reports = fetch_insider_reports(api_key, corp_code)
        todays = filter_by_date(reports, date_str)
        corp_name = todays[0].get("corp_name") if todays else (
            reports[0].get("corp_name") if reports else corp_code
        )
        text = build_report(corp_name, date_str, todays)
        send_to_slack(text)


if __name__ == "__main__":
    main(sys.argv)
