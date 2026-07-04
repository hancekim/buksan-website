#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fassto FMS 크롤러
=================

https://fms.fassto.ai/classic/cmn/main/main.do 에 로그인한 뒤,
메인 화면 하단의 스프레드시트(그리드) 데이터를 수집해 CSV/JSON 으로 저장한다.

설계 메모
---------
파스토 FMS 같은 국내 기업용 화면의 "스프레드시트"는 대부분 RealGrid / AUIGrid
같은 canvas 기반 그리드라서 화면에 보이는 표를 그대로 HTML 로 긁기 어렵다.
대신 그리드에 데이터를 채워주는 내부 API(보통 .do / .json 으로 끝나는 XHR/fetch)가
JSON 을 내려준다. 그래서 이 스크립트는:

  1) 로그인한다.
  2) 메인 페이지를 연다.
  3) 페이지가 주고받는 모든 JSON 응답(network)을 가로채서 raw 로 저장한다.
     -> 이걸 보고 "어떤 엔드포인트가 그 스프레드시트 데이터인지" 확정한다.
  4) 화면에 일반 <table> 이 있으면 그것도 CSV 로 뽑아 둔다(보너스).

정확한 엔드포인트/셀렉터가 확정되면 TARGET_API_KEYWORD / 셀렉터 환경변수로
좁혀서 깔끔한 CSV 한 장만 떨어지도록 마무리하면 된다.

환경변수
--------
  필수:
    FASSTO_ID            로그인 아이디
    FASSTO_PW            로그인 비밀번호
  선택:
    FASSTO_LOGIN_URL     로그인 페이지 URL (기본: 메인 URL)
    FASSTO_MAIN_URL      데이터가 있는 메인 URL
    FASSTO_ID_SELECTOR   아이디 입력칸 CSS 셀렉터(자동탐지 실패 시 지정)
    FASSTO_PW_SELECTOR   비번 입력칸 CSS 셀렉터
    FASSTO_SUBMIT_SELECTOR 로그인 버튼 CSS 셀렉터
    TARGET_API_KEYWORD   이 문자열이 URL 에 포함된 응답만 "메인 데이터"로 저장
    OUTPUT_DIR           출력 폴더 (기본: data)
    HEADLESS             "false" 면 브라우저 창을 띄움(디버깅)
    TZ                   기준 시간대 (기본: Asia/Seoul)
"""

import os
import re
import sys
import csv
import json
import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout


# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------
DEFAULT_URL = "https://fms.fassto.ai/classic/cmn/main/main.do"

CFG = {
    "id": os.environ.get("FASSTO_ID", ""),
    "pw": os.environ.get("FASSTO_PW", ""),
    "login_url": os.environ.get("FASSTO_LOGIN_URL", DEFAULT_URL),
    "main_url": os.environ.get("FASSTO_MAIN_URL", DEFAULT_URL),
    "id_selector": os.environ.get("FASSTO_ID_SELECTOR", ""),
    "pw_selector": os.environ.get("FASSTO_PW_SELECTOR", ""),
    "submit_selector": os.environ.get("FASSTO_SUBMIT_SELECTOR", ""),
    "target_keyword": os.environ.get("TARGET_API_KEYWORD", ""),
    "output_dir": os.environ.get("OUTPUT_DIR", "data"),
    "headless": os.environ.get("HEADLESS", "true").lower() != "false",
}

# 로그인 입력칸 자동탐지 후보 (사이트마다 name/id 가 달라서 흔한 패턴을 순서대로 시도)
ID_CANDIDATES = [
    "input#userId", "input[name='userId']", "input#loginId", "input[name='loginId']",
    "input#id", "input[name='id']", "input#mberId", "input[name='mberId']",
    "input#empId", "input[name='empId']", "input[type='text']:visible",
]
PW_CANDIDATES = [
    "input#userPw", "input[name='userPw']", "input#password", "input[name='password']",
    "input#pwd", "input[name='pwd']", "input#passwd", "input[name='passwd']",
    "input[type='password']:visible",
]
SUBMIT_CANDIDATES = [
    "button[type='submit']", "input[type='submit']",
    "button:has-text('로그인')", "a:has-text('로그인')",
    "#loginBtn", ".btn-login", ".login-btn",
]


def kst_today():
    """기준 시간대(Asia/Seoul) 기준의 '오늘' 날짜. 01:30 실행 기준 업무일."""
    tz = datetime.timezone(datetime.timedelta(hours=9))  # KST = UTC+9
    return datetime.datetime.now(tz)


def log(msg):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def try_fill(page, candidates, value, override):
    """후보 셀렉터들을 순서대로 시도해 첫 번째로 보이는 input 에 값을 채운다."""
    selectors = [override] if override else candidates
    for sel in selectors:
        if not sel:
            continue
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                loc.fill(value, timeout=3000)
                log(f"  입력 성공: {sel}")
                return True
        except Exception:
            continue
    return False


def try_click(page, candidates, override):
    selectors = [override] if override else candidates
    for sel in selectors:
        if not sel:
            continue
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                loc.click(timeout=3000)
                log(f"  버튼 클릭: {sel}")
                return True
        except Exception:
            continue
    return False


def do_login(page):
    log(f"로그인 페이지 접속: {CFG['login_url']}")
    page.goto(CFG["login_url"], wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(2000)

    if not try_fill(page, ID_CANDIDATES, CFG["id"], CFG["id_selector"]):
        log("⚠️  아이디 입력칸을 못 찾음. FASSTO_ID_SELECTOR 로 지정 필요할 수 있음.")
    if not try_fill(page, PW_CANDIDATES, CFG["pw"], CFG["pw_selector"]):
        log("⚠️  비밀번호 입력칸을 못 찾음. FASSTO_PW_SELECTOR 로 지정 필요할 수 있음.")

    clicked = try_click(page, SUBMIT_CANDIDATES, CFG["submit_selector"])
    if not clicked:
        log("로그인 버튼을 못 찾아 Enter 로 제출 시도")
        try:
            page.keyboard.press("Enter")
        except Exception:
            pass

    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except PWTimeout:
        pass
    page.wait_for_timeout(2000)
    log(f"로그인 후 현재 URL: {page.url}")


def extract_html_tables(page):
    """화면의 일반 <table> 들을 [{headers, rows}] 형태로 추출 (canvas 그리드는 못 잡음)."""
    return page.evaluate(
        """() => {
            const out = [];
            document.querySelectorAll('table').forEach((t, i) => {
                const rows = [...t.querySelectorAll('tr')].map(tr =>
                    [...tr.querySelectorAll('th,td')].map(c => c.innerText.trim())
                ).filter(r => r.length && r.some(x => x !== ''));
                if (rows.length) out.push({ index: i, rows });
            });
            return out;
        }"""
    )


def safe_name(url):
    name = re.sub(r"^https?://", "", url)
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    return name[:120]


def main():
    if not CFG["id"] or not CFG["pw"]:
        log("❌ FASSTO_ID / FASSTO_PW 환경변수가 필요합니다.")
        sys.exit(1)

    now = kst_today()
    date_str = now.strftime("%Y-%m-%d")
    out_dir = Path(CFG["output_dir"])
    raw_dir = out_dir / "raw" / date_str
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)

    captured = []   # 모든 JSON 응답 메타
    target_payloads = []  # target_keyword 매칭된 응답 본문

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=CFG["headless"])
        context = browser.new_context(
            viewport={"width": 1600, "height": 1000},
            locale="ko-KR",
        )
        page = context.new_page()

        # --- network 캡처: JSON 응답을 전부 저장 ---
        def on_response(resp):
            try:
                ct = (resp.headers.get("content-type") or "").lower()
                url = resp.url
                is_jsonish = "json" in ct or url.endswith(".do") or url.endswith(".json")
                if not is_jsonish:
                    return
                body = resp.text()
                if not body or len(body) < 2:
                    return
                # JSON 으로 파싱 가능한 것만
                try:
                    data = json.loads(body)
                except Exception:
                    return
                entry = {"url": url, "status": resp.status, "content_type": ct}
                captured.append(entry)
                fname = raw_dir / f"{len(captured):03d}_{safe_name(url)}.json"
                fname.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                if CFG["target_keyword"] and CFG["target_keyword"] in url:
                    target_payloads.append({"url": url, "data": data})
                    log(f"  ★ 타겟 API 캡처: {url}")
            except Exception:
                pass

        page.on("response", on_response)

        try:
            do_login(page)

            if page.url.rstrip("#/") != CFG["main_url"].rstrip("#/"):
                log(f"메인 페이지로 이동: {CFG['main_url']}")
                page.goto(CFG["main_url"], wait_until="domcontentloaded", timeout=60000)

            # 그리드/스프레드시트가 렌더되도록 충분히 대기 + 하단까지 스크롤
            try:
                page.wait_for_load_state("networkidle", timeout=30000)
            except PWTimeout:
                pass
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(4000)

            tables = extract_html_tables(page)
            log(f"HTML <table> {len(tables)}개 발견, JSON 응답 {len(captured)}개 캡처")

            # 스크린샷(디버깅/확인용)
            page.screenshot(path=str(out_dir / f"screenshot_{date_str}.png"), full_page=True)

        finally:
            context.close()
            browser.close()

    # ----- 결과 저장 -----
    result = {
        "collected_at": now.isoformat(),
        "date": date_str,
        "main_url": CFG["main_url"],
        "captured_api_count": len(captured),
        "captured_api_index": captured,        # url 목록 (어떤 게 데이터인지 식별용)
        "target_payloads": target_payloads,    # TARGET_API_KEYWORD 지정 시 채워짐
        "html_tables": tables,
    }
    json_path = out_dir / f"fassto_{date_str}.json"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"✅ JSON 저장: {json_path}")

    # HTML 테이블이 있으면 첫 번째를 CSV 로도 저장
    if tables:
        csv_path = out_dir / f"fassto_{date_str}.csv"
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            for row in tables[0]["rows"]:
                w.writerow(row)
        log(f"✅ CSV 저장: {csv_path}")
    else:
        log("ℹ️  일반 HTML 테이블이 없음(= canvas 그리드일 가능성 높음). "
            "data/raw 의 JSON 응답을 확인해 TARGET_API_KEYWORD 를 지정하세요.")

    log("완료. raw 응답: " + str(raw_dir))


if __name__ == "__main__":
    main()
