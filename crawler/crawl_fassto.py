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


def explore_pages(page, out_dir, raw_dir, menus, date_str, start=None, end=None):
    """지정 (상위,하위) 메뉴를 열어 북청라(IC02) 선택·조회 → 목록 API 응답을 유도한다."""
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
        if child == "택배 출고 신청" and start and end:
            # 요청일자 범위 설정(7일전~오늘). 날짜 input 에 값 세팅 후 change 이벤트 발생.
            try:
                page.evaluate(
                    """([s, e]) => {
                        const set = (sel, val) => {
                            const el = document.querySelector(sel);
                            if (el) { el.value = val;
                                el.dispatchEvent(new Event('change', {bubbles:true})); }
                        };
                        set('#ordDt1_pic12', s); set('#ordDt2_pic12', e);
                    }""", [start, end])
                log(f"  요청일자 {start}~{end} 설정")
            except Exception as e:
                log(f"  요청일자 설정 실패: {e}")
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


def _init_module(page, form_url):
    """메뉴 탭 진입 시 호출되는 Form.do 를 먼저 쳐서 모듈 세션을 초기화한다.
    (초기화 없이 mainList 를 직접 부르면 빈 응답이 온다)"""
    try:
        page.evaluate(
            """async (url) => {
                try { await fetch(url, {method:'POST',
                    headers:{'X-Requested-With':'XMLHttpRequest'}, credentials:'include'}); }
                catch(e) {}
            }""", form_url)
        page.wait_for_timeout(800)
    except Exception:
        pass


def fetch_outbound(page, center, start, end):
    """택배출고신청 목록(pic12/mainList). 요청일자 start~end, 전체 배송유형/작업상태."""
    _init_module(page, f"{API_BASE}/pic/pic12/pic12Form.do")
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
    _init_module(page, f"{API_BASE}/stk/stk06/stk06Form.do")
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


# 리포트 배송유형 컬럼 (코드, 표시명) — ordDiv 값 기준
ORD_DIV_COLS = [("O", "토스도착"), ("P", "도착보장"), ("S", "당일도착"), ("N", "일반배송")]


def _pick(d, *names):
    for n in names:
        v = d.get(n)
        if v not in (None, ""):
            return v
    return ""


def build_report_model(report_data):
    """LOC재고 + 출고 데이터를 리포트 표 모델로 집계한다.
    - 냉동/냉장: 주문 상품코드를 LOC재고(취급온도)에서 찾아 판별(냉동 포함 시 냉동, 그 외 냉장)
    - 셀 값: 출고요청(wrkStat=1) / 출고작업중(wrkStat=2) 건수
    반환: {"clients":[...], "cells":{(cst,temp,ordDiv):{"req","work"}}, "meta":{...}}
    """
    loc_rows = _rows_of(report_data.get("loc"))
    out_rows = _rows_of(report_data.get("outbound"))

    # 상품코드 -> 취급온도명(냉장/냉동/상온)
    temp_map = {}
    for r in loc_rows:
        g = _pick(r, "godCd", "cstGodCd")
        if not g:
            continue
        temp_map[g] = _pick(r, "godDealTempNm", "zoneDealTempNm")

    cells = {}
    clients = {}   # cstNm -> True (등장 순)
    order_keys = sorted(out_rows[0].keys()) if out_rows else []
    for o in out_rows:
        cst = _pick(o, "cstNm", "custNm", "cstNmValue") or "(미상)"
        clients[cst] = True
        god = _pick(o, "godCd", "cstGodCd", "godCd1")
        tname = temp_map.get(god, "")
        temp = "냉동" if "냉동" in tname else "냉장"   # 상온/미상 → 냉장(사은품 포함)
        odiv = _pick(o, "ordDiv", "ordDivCd")
        ws = str(_pick(o, "wrkStat", "wrkStatCd"))
        cell = cells.setdefault((cst, temp, odiv), {"req": 0, "work": 0})
        if ws == "1":
            cell["req"] += 1
        elif ws == "2":
            cell["work"] += 1

    return {
        "clients": list(clients.keys()),
        "cells": cells,
        "meta": {
            "loc_count": len(loc_rows),
            "outbound_count": len(out_rows),
            "order_keys": order_keys,
        },
    }


def _cell_txt(cells, cst, temp, odiv):
    c = cells.get((cst, temp, odiv))
    if not c:
        return "0 / 0"
    return f"{c['req']} / {c['work']}"


def _sum_cell(cells, cst, temp):
    req = work = 0
    for (_c, _t, _d), v in cells.items():
        if _c == cst and _t == temp:
            req += v["req"]; work += v["work"]
    return req, work


def render_report_html(model, center_name, start, end, collected_at):
    """리포트 표를 HTML 문자열로 렌더링(거래처 × 냉동/냉장/합계 × 배송유형)."""
    clients = model["clients"]
    cells = model["cells"]
    meta = model["meta"]
    divs = ORD_DIV_COLS

    def _row(temp_cells):
        """temp_cells: dict code->(req,work). 반환: (총계txt, [칸txt...])"""
        per = []
        tr = tw = 0
        for code, _ in divs:
            r, w = temp_cells.get(code, (0, 0))
            per.append(f"{r} / {w}")
            tr += r; tw += w
        return f"{tr} / {tw}", per

    body_rows = []
    for cst in clients:
        # 냉동/냉장 행
        for i, temp in enumerate(("냉동", "냉장")):
            tc = {}
            for code, _ in divs:
                c = cells.get((cst, temp, code))
                tc[code] = (c["req"], c["work"]) if c else (0, 0)
            tot, per = _row(tc)
            name = cst if i == 0 else ""
            tds = "".join(f"<td>{x}</td>" for x in per)
            body_rows.append(
                f'<tr><td class="cst">{name}</td><td>{temp}</td>'
                f'<td class="subtot">{tot}</td>{tds}<td></td><td></td></tr>')
        # 합계 행
        tc = {}
        for code, _ in divs:
            r = sum(v["req"] for (c, t, d), v in cells.items() if c == cst and d == code)
            w = sum(v["work"] for (c, t, d), v in cells.items() if c == cst and d == code)
            tc[code] = (r, w)
        tot, per = _row(tc)
        tds = "".join(f'<td class="tot">{x}</td>' for x in per)
        body_rows.append(
            f'<tr class="totrow"><td class="cst"></td><td>합계</td>'
            f'<td class="tot">{tot}</td>{tds}<td></td><td></td></tr>')

    if not clients:
        body_rows.append(
            f'<tr><td colspan="9" class="empty">현재 조회된 출고요청/출고작업중 주문이 '
            f'없습니다. (요청일자 {start} ~ {end})</td></tr>')

    div_th = "".join(f'<th class="crawl">{name}</th>' for _, name in divs)
    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>북청라센터 주문처리현황</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: 'Pretendard', system-ui, sans-serif; margin: 24px; background:#f7f8fa; color:#1a1a1a; }}
  @media (prefers-color-scheme: dark) {{ body {{ background:#12141a; color:#e8e8ea; }} }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  .meta {{ color:#777; font-size: 13px; margin-bottom:16px; }}
  .wrap {{ overflow-x:auto; }}
  table {{ border-collapse: collapse; width: 100%; min-width: 820px; background:#fff; }}
  @media (prefers-color-scheme: dark) {{ table {{ background:#1b1e26; }} }}
  th, td {{ border: 1px solid #d8dbe0; padding: 7px 10px; text-align:center; font-size:13px; }}
  @media (prefers-color-scheme: dark) {{ th,td {{ border-color:#333842; }} }}
  thead th {{ background:#eef1f5; font-weight:600; }}
  @media (prefers-color-scheme: dark) {{ thead th {{ background:#242833; }} }}
  th.crawl {{ background:#e3f0ff; }} @media (prefers-color-scheme: dark) {{ th.crawl {{ background:#1e3350; }} }}
  td.subtot, td.tot {{ font-weight:600; background:#f4f6f9; }}
  @media (prefers-color-scheme: dark) {{ td.subtot, td.tot {{ background:#232734; }} }}
  tr.totrow td {{ background:#eef1f5; font-weight:600; }}
  @media (prefers-color-scheme: dark) {{ tr.totrow td {{ background:#242833; }} }}
  td.empty {{ color:#888; padding:24px; }}
  .legend {{ font-size:12px; color:#888; margin-top:10px; }}
</style></head><body>
  <h1>주문처리현황 — {center_name}</h1>
  <div class="meta">요청일자 {start} ~ {end} · 수집시각 {collected_at} · 셀 값 = 출고요청 / 출고작업중
    · LOC재고 {meta['loc_count']}건 · 출고 {meta['outbound_count']}건</div>
  <div class="wrap"><table>
    <thead>
      <tr><th>거래처</th><th>구분</th><th>총계</th>{div_th}<th>CS처리건</th><th>비고</th></tr>
    </thead>
    <tbody>
      {''.join(body_rows)}
    </tbody>
  </table></div>
  <div class="legend">※ 냉동/냉장은 주문 상품코드를 LOC재고현황(피킹) 취급온도로 판별 —
     냉동 포함 시 냉동, 그 외 냉장(상온·사은품 포함). CS처리건·비고는 수기 입력란.</div>
</body></html>"""


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
    report_data = {"loc": None, "outbound": None}  # mainList 전체 응답

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
                # 관심 API 는 JSON 파싱 여부와 무관하게 원본(머리)을 남긴다
                # (빈 응답/HTML 도 진단해야 하므로)
                if _is_interesting(url) and len(discovery) < 40:
                    try:
                        post = resp.request.post_data
                    except Exception:
                        post = None
                    discovery.append({
                        "url": url, "status": resp.status,
                        "method": getattr(resp.request, "method", ""),
                        "post_data": (post or "")[:1500],
                        "len": len(body or ""), "body_head": (body or "")[:6000],
                    })
                    log(f"  ◆ 관심 API 응답: {url} (len={len(body or '')})")
                if not body or len(body) < 2:
                    return
                # JSON 으로 파싱 가능한 것만 (아래 raw 저장/캡처용)
                try:
                    data = json.loads(body)
                except Exception:
                    return
                entry = {"url": url, "status": resp.status, "content_type": ct}
                captured.append(entry)
                fname = raw_dir / f"{len(captured):03d}_{safe_name(url)}.json"
                fname.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                # 리포트용: 출고/재고 mainList 전체 응답을 보관(가장 큰 것 우선)
                if "pic12/mainList" in url:
                    report_data["outbound"] = data
                elif "stk06/mainList" in url:
                    if _rows_of(data) or report_data.get("loc") is None:
                        report_data["loc"] = data
                if CFG["target_keyword"] and CFG["target_keyword"] in url:
                    target_payloads.append({"url": url, "data": data})
                    log(f"  ★ 타겟 API 캡처: {url}")
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

            # 리포트 소스: UI 조회로 출고/재고 mainList 전체 응답 캡처 (북청라=IC02)
            start, end = business_date_range(now)
            log(f"요청일자 범위: {start} ~ {end} (센터 IC02)")
            explored = explore_pages(
                page, out_dir, raw_dir,
                [("출고관리", "택배 출고 신청"), ("재고관리", "LOC재고현황")],
                date_str, start=start, end=end,
            )
            out_rows = _rows_of(report_data.get("outbound"))
            loc_rows = _rows_of(report_data.get("loc"))
            log(f"출고 {len(out_rows)}건, LOC재고 {len(loc_rows)}건")
            report_source = {
                "date_range": [start, end],
                "outbound_count": len(out_rows),
                "outbound_keys": sorted(out_rows[0].keys()) if out_rows else [],
                "loc_count": len(loc_rows),
                "loc_keys": sorted(loc_rows[0].keys()) if loc_rows else [],
            }

        finally:
            context.close()
            browser.close()

    # ----- 리포트(HTML/CSV) 생성 -----
    try:
        model = build_report_model(report_data)
        collected = now.strftime("%Y-%m-%d %H:%M KST")
        html = render_report_html(model, "북청라센터(IC02)",
                                  report_source.get("date_range", ["", ""])[0],
                                  report_source.get("date_range", ["", ""])[1], collected)
        Path("report.html").write_text(html, encoding="utf-8")
        log("✅ report.html 생성")
        # CSV(엑셀용) — 거래처/구분/총계/배송유형/CS/비고
        csv_path = out_dir / f"report_{date_str}.csv"
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["거래처", "구분", "총계"] + [n for _, n in ORD_DIV_COLS] + ["CS처리건", "비고"])
            for cst in model["clients"]:
                for temp in ("냉동", "냉장"):
                    row = [cst, temp]
                    tr = tw = 0
                    cellvals = []
                    for code, _ in ORD_DIV_COLS:
                        c = model["cells"].get((cst, temp, code))
                        r, wk = (c["req"], c["work"]) if c else (0, 0)
                        cellvals.append(f"{r} / {wk}"); tr += r; tw += wk
                    row += [f"{tr} / {tw}"] + cellvals + ["", ""]
                    w.writerow(row)
        log(f"✅ report CSV 저장: {csv_path}")
    except Exception as e:
        log(f"⚠️ 리포트 생성 실패: {e}")

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
    log(f"✅ 진단 JSON 저장: {json_path}")
    log("완료. raw 응답: " + str(raw_dir))


if __name__ == "__main__":
    main()
