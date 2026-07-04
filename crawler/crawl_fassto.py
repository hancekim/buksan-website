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

# 참고: GitHub Actions 의 `${{ vars.X }}` 는 변수 미설정 시 빈 문자열("")을 넘긴다.
# os.environ.get(k, default) 는 "빈 문자열이라도 값이 있으면" 그대로 반환하므로
# 기본값이 무시된다. 그래서 URL/폴더처럼 기본값이 중요한 항목은 `or` 로 처리해
# 빈 문자열도 기본값으로 대체되게 한다.
def env(key, default=""):
    return (os.environ.get(key) or "").strip() or default


CFG = {
    "id": env("FASSTO_ID"),
    "pw": env("FASSTO_PW"),
    "login_url": env("FASSTO_LOGIN_URL", DEFAULT_URL),
    "main_url": env("FASSTO_MAIN_URL", DEFAULT_URL),
    "id_selector": env("FASSTO_ID_SELECTOR"),
    "pw_selector": env("FASSTO_PW_SELECTOR"),
    "submit_selector": env("FASSTO_SUBMIT_SELECTOR"),
    "target_keyword": env("TARGET_API_KEYWORD"),
    "output_dir": env("OUTPUT_DIR", "data"),
    "headless": env("HEADLESS", "true").lower() != "false",
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


_ENUM_JS = """() => {
    const pick = (e) => ({
        tag: e.tagName.toLowerCase(),
        type: (e.getAttribute('type') || '').toLowerCase(),
        name: e.getAttribute('name') || '',
        id: e.id || '',
        placeholder: e.getAttribute('placeholder') || '',
        text: (e.innerText || e.value || '').trim().slice(0, 30),
    });
    return {
        title: document.title,
        inputs: [...document.querySelectorAll('input, select, textarea')].map(pick),
        buttons: [...document.querySelectorAll(
            "button, a[onclick], input[type=submit], input[type=button], [role=button]")].map(pick),
    };
}"""


def enumerate_fields(page):
    """모든 프레임(iframe 포함)의 input/button 요소를 로그로 찍고 구조를 반환한다.
    파스토 classic 화면이 프레임 기반일 수 있어, 프레임별로 훑는다.
    이 로그만 보면 로그인 폼의 정확한 name/id 를 알 수 있다."""
    out = []
    log("=== 페이지 요소 진단 (프레임별) ===")
    for fr in page.frames:
        try:
            data = fr.evaluate(_ENUM_JS)
        except Exception as e:
            data = {"error": str(e)}
        out.append({"frame_url": fr.url, "data": data})
        log(f"[frame] url={fr.url}  title={data.get('title', '')!r}")
        for i in data.get("inputs", []):
            log(f"    input  type={i['type']!r} name={i['name']!r} "
                f"id={i['id']!r} ph={i['placeholder']!r}")
        for b in data.get("buttons", []):
            log(f"    button text={b['text']!r} id={b['id']!r} type={b['type']!r}")
    return out


def _login_in_frame(fr):
    """비밀번호 input 이 있는 프레임에서 아이디/비번을 채우고 제출을 시도한다."""
    pw = fr.locator("input[type='password']").first
    if pw.count() == 0:
        return False
    # 아이디: 같은 프레임의 첫 text/무타입 input
    id_input = fr.locator(
        "input[type='text'], input[type='email'], input:not([type])"
    ).first
    try:
        if id_input.count() > 0:
            id_input.fill(CFG["id"], timeout=3000)
        pw.fill(CFG["pw"], timeout=3000)
        log(f"  로그인 폼 입력 완료 (frame: {fr.url})")
    except Exception as e:
        log(f"  입력 실패: {e}")
        return False
    # 제출: 프레임 내 로그인 버튼 → 없으면 Enter
    for sel in SUBMIT_CANDIDATES:
        try:
            b = fr.locator(sel).first
            if b.count() > 0:
                b.click(timeout=3000)
                log(f"  버튼 클릭: {sel}")
                return True
        except Exception:
            continue
    try:
        pw.press("Enter")
        log("  Enter 로 제출")
        return True
    except Exception:
        return False


def do_login(page):
    log(f"로그인 페이지 접속: {CFG['login_url']}")
    page.goto(CFG["login_url"], wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3000)

    # 먼저 구조를 로그로 남긴다 (셀렉터 확정용)
    enumerate_fields(page)

    # 1) 셀렉터가 명시됐으면 메인 페이지 우선 시도
    done = False
    if CFG["id_selector"] or CFG["pw_selector"]:
        ok_id = try_fill(page, ID_CANDIDATES, CFG["id"], CFG["id_selector"])
        ok_pw = try_fill(page, PW_CANDIDATES, CFG["pw"], CFG["pw_selector"])
        if ok_id or ok_pw:
            try_click(page, SUBMIT_CANDIDATES, CFG["submit_selector"]) or page.keyboard.press("Enter")
            done = True

    # 2) 자동: 비번 input 이 있는 프레임을 찾아 로그인
    if not done:
        for fr in page.frames:
            if _login_in_frame(fr):
                done = True
                break

    if not done:
        log("⚠️  로그인 폼을 어떤 프레임에서도 못 찾음. 위 '요소 진단' 로그를 보고 "
            "FASSTO_ID_SELECTOR / FASSTO_PW_SELECTOR 를 지정해야 함.")

    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except PWTimeout:
        pass
    page.wait_for_timeout(2000)
    log(f"로그인 후 현재 URL: {page.url}")
    # 로그인 성공 여부 판단용: 로그인 후 다시 한 번 진단
    enumerate_fields(page)


def dump_frames_html(page, raw_dir):
    """각 프레임의 HTML 을 raw 에 저장 (아티팩트로 받아 구조 분석용)."""
    for idx, fr in enumerate(page.frames):
        try:
            html = fr.content()
        except Exception:
            continue
        (raw_dir / f"frame_{idx:02d}_{safe_name(fr.url)}.html").write_text(
            html, encoding="utf-8"
        )


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

            # 프레임 HTML 덤프 + 스크린샷(구조 분석/확인용)
            dump_frames_html(page, raw_dir)
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
