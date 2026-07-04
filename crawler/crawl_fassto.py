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
import urllib.parse
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
    """비밀번호 input 이 있는 프레임에서 아이디/비번을 채우고 제출을 시도한다.
    파스토 로그인 폼: 아이디=#loginId, 비번=#psWd, 제출='로그인'(submit).
    #psWd 앞에 화면에 안 보이는 더미 password 칸이 있어 :visible 로 걸러낸다."""
    # 비밀번호: 지정 셀렉터 → 보이는 password (숨은 더미 제외)
    pw_sel = CFG["pw_selector"] or "#psWd, input[type='password']:visible"
    pw = fr.locator(pw_sel).first
    if pw.count() == 0:
        return False
    # 아이디: 지정 셀렉터 → #loginId → 보이는 text/email
    id_sel = CFG["id_selector"] or (
        "#loginId, input[type='text']:visible, input[type='email']:visible"
    )
    id_input = fr.locator(id_sel).first
    try:
        if id_input.count() > 0:
            id_input.fill(CFG["id"], timeout=5000)
        pw.fill(CFG["pw"], timeout=5000)
        log(f"  로그인 폼 입력 완료 (frame: {fr.url})")
    except Exception as e:
        log(f"  입력 실패: {e}")
        return False
    # 제출: '로그인' submit 버튼 우선 → 일반 후보 → Enter
    submit_sels = ([CFG["submit_selector"]] if CFG["submit_selector"] else []) + [
        "button[type='submit']:has-text('로그인')",
        "button[type='submit']",
        "button:has-text('로그인')",
    ]
    for sel in submit_sels:
        if not sel:
            continue
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
    resp = page.goto(CFG["login_url"], wait_until="domcontentloaded", timeout=60000)
    if resp is not None:
        log(f"  HTTP 상태: {resp.status} {resp.status_text}")
        if resp.status == 403:
            log("  ⛔ 서버가 접속을 차단(403)했습니다. IP 차단(해외/데이터센터 IP)일 "
                "가능성이 높습니다. 이 경우 한국 내 네트워크에서 실행해야 합니다.")
    page.wait_for_timeout(3000)

    # 먼저 구조를 로그로 남긴다 (셀렉터 확정용)
    enumerate_fields(page)

    # 비번 input 이 있는 프레임을 찾아 로그인 (CFG 셀렉터가 있으면 그것을 사용)
    done = False
    for fr in page.frames:
        if _login_in_frame(fr):
            done = True
            break

    if not done:
        log("⚠️  로그인 폼을 어떤 프레임에서도 못 찾음. 위 '요소 진단' 로그를 보고 "
            "FASSTO_ID_SELECTOR / FASSTO_PW_SELECTOR 를 지정해야 함.")
    else:
        # 로그인 성공 시 보통 메인(main.do)으로 리다이렉트된다. 확인.
        try:
            page.wait_for_url("**/main/main.do", timeout=15000)
            log("  ✅ 로그인 성공 → 메인 화면 이동 확인")
        except PWTimeout:
            log("  ⚠️  로그인 후 메인 이동이 확인되지 않음(자격증명 오류이거나 다른 랜딩일 수 있음).")

    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except PWTimeout:
        pass
    page.wait_for_timeout(2000)
    log(f"로그인 후 현재 URL: {page.url}")
    # 로그인 성공 여부 판단용: 로그인 후 다시 한 번 진단
    enumerate_fields(page)


def dump_frames_html(page, raw_dir, prefix=""):
    """각 프레임의 HTML 을 raw 에 저장 (아티팩트로 받아 구조 분석용)."""
    for idx, fr in enumerate(page.frames):
        try:
            html = fr.content()
        except Exception:
            continue
        (raw_dir / f"{prefix}frame_{idx:02d}_{safe_name(fr.url)}.html").write_text(
            html, encoding="utf-8"
        )


def _click_text(page, name):
    """텍스트로 요소를 찾아 클릭(프레임 순회, exact→부분, 마지막엔 force)."""
    for exact in (True, False):
        for fr in page.frames:
            try:
                loc = fr.get_by_text(name, exact=exact).first
                if loc.count() > 0:
                    try:
                        loc.scroll_into_view_if_needed(timeout=2000)
                    except Exception:
                        pass
                    try:
                        loc.click(timeout=3000)
                    except Exception:
                        loc.click(timeout=3000, force=True)  # 숨김/겹침 대비
                    log(f"  클릭: '{name}' (frame {fr.url})")
                    return True
            except Exception:
                continue
    return False


def open_menu(page, parent, child):
    """상위 메뉴(parent)를 펼친 뒤 하위 메뉴(child)를 클릭한다.
    LNB 하위 항목은 상위가 접혀 있으면 숨겨져 클릭이 안 되므로 먼저 펼친다."""
    # 하위를 바로 시도 → 실패 시 상위 펼치고 재시도
    if _click_text(page, child):
        return True
    if parent and _click_text(page, parent):
        page.wait_for_timeout(1200)
        if _click_text(page, child):
            return True
    log(f"  ⚠️  메뉴 '{parent} > {child}' 를 못 찾음")
    return False


def explore_pages(page, out_dir, raw_dir, menus, date_str):
    """지정 (상위,하위) 메뉴들을 차례로 열어, 각 페이지의 폼/드롭다운/프레임 HTML 을 수집한다.
    (북청라센터 선택칸·요청일자·조회 버튼·그리드 API 구조 파악용)"""
    explored = {}
    for parent, child in menus:
        key = safe_name(child)
        if not open_menu(page, parent, child):
            explored[child] = {"opened": False}
            continue
        page.wait_for_timeout(5000)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except PWTimeout:
            pass
        log(f"── '{child}' 페이지 구조 ──")
        nav = collect_navigation(page)   # select 옵션(센터 목록 등) 로그 포함
        enumerate_fields(page)           # input/버튼 로그

        # 활성 탭에서 북청라센터(IC02) 선택 + (LOC는 피킹) 후 조회 → 목록 API 캡처
        try:
            page.locator("#whCd:visible").first.select_option("IC02", timeout=4000)
            log("  whCd=IC02(북청라) 선택")
        except Exception as e:
            log(f"  whCd 선택 실패: {e}")
        if child == "LOC재고현황":
            try:
                page.locator("#locDiv:visible").first.select_option("01", timeout=4000)
                log("  locDiv=01(피킹) 선택")
            except Exception as e:
                log(f"  locDiv 선택 실패: {e}")
        page.wait_for_timeout(1500)  # 센터 변경(onchange) 반영 대기 후 조회
        try:
            page.locator("#btnSearch:visible").first.click(timeout=4000)
            log("  조회(btnSearch) 클릭")
            page.wait_for_timeout(6000)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except PWTimeout:
                pass
        except Exception as e:
            log(f"  조회 클릭 실패: {e}")

        explored[child] = {"opened": True, "navigation": nav}
        dump_frames_html(page, raw_dir, prefix=f"{key}__")
        try:
            page.screenshot(
                path=str(out_dir / f"screenshot_{key}_{date_str}.png"), full_page=True
            )
        except Exception:
            pass
    return explored


_NAV_JS = """() => {
    const links = [...document.querySelectorAll('a, [onclick]')].map(a => ({
        text: (a.innerText || a.getAttribute('title') || '').trim().slice(0, 40),
        href: a.getAttribute('href') || '',
        onclick: (a.getAttribute('onclick') || '').slice(0, 120),
        id: a.id || '',
    })).filter(l => (l.text || l.onclick) && l.href !== 'javascript:;');
    const selects = [...document.querySelectorAll('select')].map(s => ({
        name: s.getAttribute('name') || '', id: s.id || '',
        options: [...s.options].map(o => ({
            value: o.value, text: (o.text || '').trim().slice(0, 40)
        })).slice(0, 80),
    }));
    return { links, selects };
}"""


def collect_navigation(page):
    """LNB 메뉴 링크와 select 드롭다운(센터 선택 등)을 프레임별로 수집한다.
    이 결과로 '북청라센터' 선택 방법과 출고/재고/상품 메뉴 위치를 파악한다."""
    nav = {"links": [], "selects": []}
    seen = set()
    for fr in page.frames:
        try:
            d = fr.evaluate(_NAV_JS)
        except Exception:
            continue
        for l in d.get("links", []):
            key = (l["text"], l["href"], l["onclick"])
            if key in seen:
                continue
            seen.add(key)
            nav["links"].append({**l, "frame": fr.url})
        for s in d.get("selects", []):
            nav["selects"].append({**s, "frame": fr.url})
    # 센터 관련 키워드가 든 링크/옵션은 눈에 띄게 로그로도 남긴다
    for s in nav["selects"]:
        for o in s["options"]:
            if any(k in o["text"] for k in ("센터", "청라", "북청")):
                log(f"  [center?] select#{s['id'] or s['name']} option "
                    f"value={o['value']!r} text={o['text']!r}")
    log(f"내비게이션 수집: 링크 {len(nav['links'])}개, select {len(nav['selects'])}개")
    return nav


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


# ---------------------------------------------------------------------------
# 리포트 데이터: 출고(pic12) + 재고LOC(stk06) API 직접 호출
# ---------------------------------------------------------------------------
API_BASE = "https://fms.fassto.ai/classic"

# LOC재고현황(stk06) 검색 파라미터 기본값 (whCd/locDiv 만 채워 사용)
STK06_SCHPARAM = {
    "cateCd": "", "whCd": "", "cstCd": "", "cstNm": "", "groupcstCd": None,
    "godCd": "", "cstGodCd": "", "godNm": "", "inBoxBarcd": "", "lCate": "",
    "mCate": "", "sCate": "", "productsBeyondExpirationDate": "", "seasonCd": "",
    "genderCd": "", "makeYr": "", "boxInCntYn": "N", "zone": "", "locX": "",
    "locY": "", "locZ": "", "locD": "", "locDiv": "", "boxCountYn": "N",
    "equipType": "", "keepWay": "", "dealTemp": "",
}


def business_date_range(now):
    """요청일자 범위 [7일전, 종료일]. 새벽 02:00 이전이면 종료일=어제(업무일 기준)."""
    end = now.date() if now.hour >= 2 else (now.date() - datetime.timedelta(days=1))
    start = end - datetime.timedelta(days=7)
    return start.isoformat(), end.isoformat()


def api_post(page, url, form):
    """페이지 내부에서 fetch 로 폼 POST (앱과 동일한 쿠키·오리진·세션).
    반환: {status, parsed, json, head}"""
    body = urllib.parse.urlencode(form)
    js = """async ([url, body]) => {
        try {
            const r = await fetch(url, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                    'X-Requested-With': 'XMLHttpRequest'
                },
                body, credentials: 'include'
            });
            const t = await r.text();
            let j = null, parsed = false;
            try { j = JSON.parse(t); parsed = true; } catch (e) {}
            return { status: r.status, parsed, json: j, head: parsed ? null : t.slice(0, 800) };
        } catch (e) {
            return { status: -1, parsed: false, json: null, head: String(e) };
        }
    }"""
    return page.evaluate(js, [url, body])


def fetch_outbound(page, center, start, end):
    """택배출고신청 목록(pic12/mainList). 요청일자 start~end, 전체 배송유형/작업상태."""
    return api_post(page, f"{API_BASE}/pic/pic12/mainList.json", {
        "custNmValue": "", "whCd": center,
        "ordDt1_pic12": start, "ordDt2_pic12": end,
        "cstCd": "", "cstNm": "", "groupcstCd": "", "ordNo": "",
        "ordDiv": "", "wrkStat": "", "salChanel": "", "custNm": "",
        "custTelNo": "", "fileDownloadYn": "0", "searchType": "slipNo",
        "slipNo": "", "labelNo": "",
    })


def fetch_loc_stock(page, center):
    """LOC재고현황(stk06/mainList) — 해당 센터 피킹(locDiv=01) 재고."""
    sch = dict(STK06_SCHPARAM)
    sch["whCd"] = center
    sch["locDiv"] = "01"
    return api_post(page, f"{API_BASE}/stk/stk06/mainList.json",
                    {"schParam": json.dumps(sch, ensure_ascii=False)})


def _rows_of(payload):
    """API 응답에서 목록 배열을 꺼낸다(list/rows/data 등 키 대응)."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for k in ("list", "rows", "data", "resultList", "items"):
            v = payload.get(k)
            if isinstance(v, list):
                return v
    return []


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
    discovery = []  # 관심 API(출고/재고 목록 등)의 요청 파라미터+응답 샘플
    requests_log = []  # 모든 요청(응답 무관) — 엔드포인트 식별용
    tables = []
    nav = {"links": [], "selects": []}
    explored = {}
    report_source = {}

    # 관심 API 판별: classic 경로의 목록/조회성 엔드포인트 (노이즈 제외)
    def _is_interesting(url):
        if "channel.io" in url or "/cmn/" in url:
            return False
        return "/classic/" in url and any(
            k in url for k in ("otb", "stk", "pic", "dlv", "out", "loc", "List", "list", "Sch")
        )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=CFG["headless"])
        context = browser.new_context(
            viewport={"width": 1600, "height": 1000},
            locale="ko-KR",
            timezone_id="Asia/Seoul",
            # 헤드리스 크롬(HeadlessChrome UA)이 WAF 에 막히는 경우가 있어
            # 일반 크롬처럼 보이도록 UA/언어 헤더를 지정한다.
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            extra_http_headers={
                "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
            },
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
                # 관심 API 는 요청 파라미터 + 응답 샘플을 결과에 담는다(엔드포인트 확정용)
                if _is_interesting(url) and len(discovery) < 25:
                    try:
                        req = resp.request
                        post = req.post_data
                    except Exception:
                        post = None
                    body_head = json.dumps(data, ensure_ascii=False)[:6000]
                    discovery.append({
                        "url": url, "method": getattr(resp.request, "method", ""),
                        "post_data": (post or "")[:1500], "body_head": body_head,
                    })
                    log(f"  ◆ 관심 API 캡처: {url}")
            except Exception:
                pass

        page.on("response", on_response)

        # --- 요청 로깅: 응답이 JSON 이 아니거나 실패해도 엔드포인트를 남긴다 ---
        def on_request(req):
            try:
                u = req.url
                if "fms.fassto.ai" not in u:
                    return
                if "/cmn/" in u:
                    return
                path = u.split("?")[0]
                if not (path.endswith(".do") or path.endswith(".json") or req.method == "POST"):
                    return
                if len(requests_log) < 120:
                    requests_log.append({
                        "method": req.method, "url": path,
                        "post": (req.post_data or "")[:800],
                    })
            except Exception:
                pass

        page.on("request", on_request)

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

            # 리포트 소스: 출고/재고 API 직접 호출 (북청라센터=IC02)
            start, end = business_date_range(now)
            log(f"요청일자 범위: {start} ~ {end} (센터 IC02)")
            out_res = fetch_outbound(page, "IC02", start, end)
            loc_res = fetch_loc_stock(page, "IC02")
            outbound = out_res.get("json")
            loc_stock = loc_res.get("json")
            (raw_dir / "outbound_pic12.json").write_text(
                json.dumps(out_res, ensure_ascii=False, indent=2), encoding="utf-8")
            (raw_dir / "loc_stk06.json").write_text(
                json.dumps(loc_res, ensure_ascii=False, indent=2), encoding="utf-8")
            out_rows = _rows_of(outbound)
            loc_rows = _rows_of(loc_stock)
            log(f"출고 {len(out_rows)}건(status {out_res.get('status')}), "
                f"LOC재고 {len(loc_rows)}건(status {loc_res.get('status')})")
            report_source = {
                "date_range": [start, end],
                "outbound_status": out_res.get("status"),
                "outbound_head": out_res.get("head"),
                "outbound_top_keys": (sorted(outbound.keys()) if isinstance(outbound, dict) else None),
                "outbound_count": len(out_rows),
                "outbound_keys": sorted(out_rows[0].keys()) if out_rows else [],
                "outbound_sample": out_rows[:3],
                "loc_status": loc_res.get("status"),
                "loc_head": loc_res.get("head"),
                "loc_top_keys": (sorted(loc_stock.keys()) if isinstance(loc_stock, dict) else None),
                "loc_count": len(loc_rows),
                "loc_keys": sorted(loc_rows[0].keys()) if loc_rows else [],
                "loc_sample": loc_rows[:2],
            }

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
        "report_source": report_source,         # 출고/재고 API 응답 샘플(필드 확인용)
        "discovery": discovery,                 # 목록 API 요청 파라미터 + 응답 샘플
        "requests_log": requests_log,           # 모든 요청(엔드포인트 식별용)
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
