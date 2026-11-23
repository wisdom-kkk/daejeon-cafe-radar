"""대전 카페 레이더 - 매일 데이터 수집.

실행:
  python collector/collect.py          # 실제 API 호출 (환경변수에 키 필요)
  python collector/collect.py --demo   # 키 없이 샘플 데이터로 대시보드 확인

결과:
  docs/data/*.json   대시보드가 읽는 파일 (GitHub Pages)
  data/store/cafes.json  지금까지 본 카페 기록 (다음 실행 때 비교용)
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import random
import re
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from pyproj import Transformer

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config as C  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "data"
STORE = ROOT / "data" / "store" / "cafes.json"
KST = timezone(timedelta(hours=9))

TO_WGS = Transformer.from_crs(C.TM_PROJ, "EPSG:4326", always_xy=True)
TO_TM = Transformer.from_crs("EPSG:4326", C.TM_PROJ, always_xy=True)

log_lines: list[str] = []


def log(msg: str) -> None:
    print(msg, flush=True)
    log_lines.append(msg)


# ------------------------------------------------------------------ helpers
def pick(d: dict, names: list[str], default=None):
    """여러 후보 필드명 중 처음 값이 있는 것을 돌려줌 (대소문자 무시)."""
    lower = {str(k).lower(): v for k, v in d.items()}
    for n in names:
        v = lower.get(n.lower())
        if v not in (None, "", " "):
            return v
    return default


def ymd(v) -> str | None:
    if not v:
        return None
    digits = re.sub(r"\D", "", str(v))
    if len(digits) < 8:
        return None
    d = digits[:8]
    try:
        datetime.strptime(d, "%Y%m%d")
    except ValueError:
        return None
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}"


def find_records(obj, must=("BPLC_NM",)):
    """응답 JSON 안에서 레코드 리스트를 찾아냄 (응답 껍데기 구조가 달라도 동작)."""
    must_l = {m.lower() for m in must}
    if isinstance(obj, list):
        if obj and all(isinstance(x, dict) for x in obj) and any(
            must_l & {str(k).lower() for k in x} for x in obj
        ):
            return obj
        for x in obj:
            r = find_records(x, must)
            if r is not None:
                return r
    elif isinstance(obj, dict):
        if must_l & {str(k).lower() for k in obj}:
            return [obj]
        for v in obj.values():
            r = find_records(v, must)
            if r is not None:
                return r
    return None


def find_key(obj, key: str):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() == key.lower():
                return v
            r = find_key(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for x in obj:
            r = find_key(x, key)
            if r is not None:
                return r
    return None


def api_error(js) -> str | None:
    code = find_key(js, "resultCode")
    if code is not None and str(code) not in ("00", "0", "INFO-000", "200"):
        return f"resultCode={code} {find_key(js, 'resultMsg') or ''}".strip()
    return None


# ------------------------------------------------------------------ 1. 인허가
def fetch_all(url: str, params: dict, key: str, max_pages: int = 400) -> list[dict]:
    rows: list[dict] = []
    for page in range(1, max_pages + 1):
        p = {"serviceKey": key, "pageNo": page, "numOfRows": 100, "returnType": "json", **params}
        js = None
        for attempt in range(3):
            body = ""
            try:
                r = requests.get(url, params=p, timeout=30)
                body = r.text[:200]
                r.raise_for_status()
                js = r.json()
                break
            except Exception as e:  # noqa: BLE001
                if attempt == 2:
                    raise RuntimeError(f"{url} page {page}: {e} / {body}") from e
                time.sleep(2 * (attempt + 1))
        err = api_error(js)
        if err:
            raise RuntimeError(f"{url}: {err}")
        items = find_records(js) or []
        rows.extend(items)
        total = find_key(js, "totalCount")
        if len(items) < 100 or (total is not None and len(rows) >= int(total)):
            break
        time.sleep(0.15)
    return rows


def gu_dong(road: str | None, lot: str | None) -> tuple[str, str]:
    gu = ""
    for a in (road, lot):
        if a:
            m = re.search(r"대전광역시\s+(\S+구)", a)
            if m:
                gu = m.group(1)
                break
    dong = ""
    if lot:
        m = re.search(r"구\s+(\S+?(?:동\d*가?|동))(?:\s|$|\d)", lot)
        if m:
            dong = m.group(1)
    if not dong and road:
        m = re.search(r"\(([^,()]*?동\d*가?)[,)]", road)
        if m:
            dong = m.group(1).strip()
    return gu, dong


def to_latlng(x, y):
    try:
        x, y = float(x), float(y)
    except (TypeError, ValueError):
        return None, None
    if x == 0 or y == 0:
        return None, None
    lng, lat = TO_WGS.transform(x, y)
    lat_min, lng_min, lat_max, lng_max = C.DAEJEON_BBOX
    if not (lat_min <= lat <= lat_max and lng_min <= lng <= lng_max):
        return None, None
    return round(lat, 6), round(lng, 6)


def normalize(raw: dict, src: dict) -> dict:
    name = str(pick(raw, ["BPLC_NM", "bplcNm"], "")).strip()
    road = pick(raw, ["ROAD_NM_ADDR", "RDNWHLADDR"])
    lot = pick(raw, ["LOTNO_ADDR", "SITE_ADDR", "SITEWHLADDR"])
    rid = pick(raw, ["MNG_NO", "OPN_SVC_MNG_NO", "mngNo"])
    if not rid:
        rid = hashlib.md5(f"{name}|{road}|{lot}".encode()).hexdigest()[:16]
    status_nm = str(pick(raw, ["SALS_STTS_NM", "DTL_SALS_STTS_NM", "TRDSTATENM"], "") or "")
    status_cd = str(pick(raw, ["SALS_STTS_CD", "TRDSTATEGBN"], "") or "")
    closed = ymd(pick(raw, ["CLSBIZ_YMD", "DCBYMD"]))
    is_closed = ("폐업" in status_nm) or status_cd == "03" or bool(closed)
    lat, lng = to_latlng(pick(raw, ["XCRD", "CRD_INFO_X", "X"]), pick(raw, ["YCRD", "CRD_INFO_Y", "Y"]))
    gu, dong = gu_dong(road, lot)
    return {
        "id": f"{src['key']}-{rid}",
        "name": name,
        "source": src["label"],
        "biz_type": str(pick(raw, ["BZSTAT_SE_NM", "SNTTN_BZSTAT_NM", "UPTAENM", "uptaeNm"], "") or "").strip(),
        "permit_date": ymd(pick(raw, ["LCPMT_YMD", "APVPERMYMD"])),
        "status": "폐업" if is_closed else (status_nm or "영업"),
        "closed": is_closed,
        "closed_date": closed,
        "road_addr": road,
        "lot_addr": lot,
        "gu": gu,
        "dong": dong,
        "lat": lat,
        "lng": lng,
        "updated": str(pick(raw, ["DAT_UPDT_PNT", "LASTMODTS"], "") or ""),
    }


CATEGORIES = ["카페", "베이커리", "식당", "주점", "기타"]


def classify(rec: dict) -> tuple[str, str]:
    """(종류, 세부종류). 규칙은 config.py 2번 참고."""
    name_l = rec["name"].lower()
    bt = rec["biz_type"].replace(" ", "")
    has = lambda keys: any(k.replace(" ", "") in bt for k in keys)  # noqa: E731
    if any(k in name_l for k in C.EXCLUDE_NAME_KEYWORDS):
        return "기타", rec["biz_type"] or "기타"
    if rec["source"] == "제과점":
        return "베이커리", "제과점"
    if any(k in name_l for k in C.BAKERY_NAME_KEYWORDS) and not has(C.BAKERY_EXCLUDE_BIZ):
        return "베이커리", "빵집"
    if has(C.PUB_BIZ_TYPES):
        return "주점", rec["biz_type"]
    if has(C.OTHER_BIZ_TYPES):
        return "기타", rec["biz_type"]
    if has(C.CAFE_BIZ_TYPES):
        return "카페", rec["biz_type"]
    if any(k in name_l for k in C.CAFE_NAME_KEYWORDS) and (bt in C.CAFE_NAME_OK_BIZ or bt.startswith("기타")):
        return "카페", rec["biz_type"] or "기타"
    for sub, keys in C.RESTAURANT_SUBTYPES:
        if has(keys):
            return "식당", sub
    if rec["source"] == "일반음식점":
        return "식당", "기타 식당"
    return "기타", rec["biz_type"] or "기타"


def is_cafe(rec: dict) -> bool:  # 예전 이름 호환
    return classify(rec)[0] == "카페"


def collect_licenses(store: dict, today: date, key: str) -> tuple[list[dict], dict, set]:
    """반환: (이번에 처음 본 업소, 소스별 상태, 이번에 백필한 소스 키)"""
    status = {}
    newly: list[dict] = []
    backfilled_now: set[str] = set()
    done = set(store.setdefault("backfilled", []))
    for src in C.LICENSE_SOURCES:
        backfill = src["key"] not in done
        if backfill:
            since = C.BACKFILL_FROM
            upd_since = (today - timedelta(days=30)).strftime("%Y%m%d")
        else:
            since = upd_since = (today - timedelta(days=C.DAILY_LOOKBACK_DAYS)).strftime("%Y%m%d")
        queries = [{"cond[LCPMT_YMD::GTE]": since}, {"cond[DAT_UPDT_PNT::GTE]": upd_since + "000000"}]
        try:
            raws = []
            for q in queries:
                label = f"{list(q)[0]} {list(q.values())[0]}"
                q["cond[ROAD_NM_ADDR::LIKE]"] = C.REGION_ADDR
                got = fetch_all(src["url"], q, key)
                log(f"[{src['label']}] {label}: {len(got)}건")
                raws.extend(got)
            counts = defaultdict(int)
            for raw in raws:
                rec = normalize(raw, src)
                rec["category"], rec["subtype"] = classify(rec)
                counts[rec["category"]] += 1
                old = store["records"].get(rec["id"])
                if old is None:
                    rec["first_seen"] = today.isoformat()
                    rec["via_backfill"] = backfill
                    newly.append(rec)
                else:
                    rec["first_seen"] = old.get("first_seen", today.isoformat())
                    if rec["closed"] and not old.get("closed"):
                        rec["closed_detected"] = today.isoformat()
                    elif old.get("closed_detected"):
                        rec["closed_detected"] = old["closed_detected"]
                store["records"][rec["id"]] = rec
            status[src["key"]] = {"ok": True, "fetched": len(raws), "by_category": dict(counts), "backfill": backfill}
            if backfill:
                done.add(src["key"])
                backfilled_now.add(src["key"])
        except Exception as e:  # noqa: BLE001
            log(f"[{src['label']}] 실패: {e}")
            status[src["key"]] = {"ok": False, "error": str(e)[:300]}
    store["backfilled"] = sorted(done)
    # 예전(카페 전용) 저장분도 종류를 다시 매김
    for r in store["records"].values():
        if "category" not in r:
            r["category"], r["subtype"] = classify(r)
    return newly, status, backfilled_now


# ------------------------------------------------------------------ 2. 네이버 검색 트렌드
def collect_trends(today: date, cid: str, secret: str) -> dict:
    end = today - timedelta(days=1)
    start = end - timedelta(weeks=C.TREND_WEEKS)
    # 2026-07-31 이후 새 키는 NAVER API HUB(네이버 클라우드)에서 발급. 예전 개발자센터 키는 NAVER_API_HUB=0
    if os.environ.get("NAVER_API_HUB", "1") != "0":
        url = "https://naverapihub.apigw.ntruss.com/search-trend/v1/search"
        headers = {"X-NCP-APIGW-API-KEY-ID": cid, "X-NCP-APIGW-API-KEY": secret, "Content-Type": "application/json"}
    else:
        url = "https://openapi.naver.com/v1/datalab/search"
        headers = {"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": secret, "Content-Type": "application/json"}
    series: dict[str, dict[str, float]] = {}
    areas = list(C.TREND_AREAS)
    for i in range(0, len(areas), 4):
        batch = areas[i:i + 4]
        groups = [{"groupName": "__anchor", "keywords": [C.TREND_ANCHOR]}] + [
            {"groupName": a, "keywords": [f"{a} 카페"]} for a in batch
        ]
        body = {"startDate": start.isoformat(), "endDate": end.isoformat(), "timeUnit": "week", "keywordGroups": groups}
        r = requests.post(url, headers=headers, json=body, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"네이버 데이터랩 {r.status_code}: {r.text[:200]}")
        res = {g["title"]: {d["period"]: d["ratio"] for d in g["data"]} for g in r.json()["results"]}
        anchor = res.pop("__anchor", {})
        base = (sum(anchor.values()) / len(anchor)) if anchor else 0
        for a, pts in res.items():
            series[a] = {p: round(v * 100 / base, 1) if base else v for p, v in pts.items()}
        time.sleep(0.3)
    return build_trend_output(series)


def build_trend_output(series: dict[str, dict[str, float]]) -> dict:
    periods = sorted({p for s in series.values() for p in s})
    out = {"periods": periods, "unit": f"'{C.TREND_ANCHOR}' 평균 검색량=100", "areas": []}
    for a, pts in series.items():
        vals = [pts.get(p, 0.0) for p in periods]
        recent, prev = vals[-8:], vals[-16:-8]
        r_avg = sum(recent) / len(recent) if recent else 0
        p_avg = sum(prev) / len(prev) if prev else 0
        change = round((r_avg - p_avg) / p_avg * 100, 1) if p_avg > 0 else None
        out["areas"].append({"area": a, "values": vals, "recent8": round(r_avg, 1), "change8_pct": change})
    out["areas"].sort(key=lambda x: x["recent8"], reverse=True)
    return out


# ------------------------------------------------------------------ 3. 서구 유동인구
def parse_footfall_rows(rows: list[dict]) -> dict:
    F = C.FOOTFALL_FIELD_CANDIDATES
    agg: dict[tuple[str, str], float] = defaultdict(float)
    for r in rows:
        ym = pick(r, F["ym"])
        if ym:
            d = re.sub(r"\D", "", str(ym))[:6]
        else:
            y, m = pick(r, F["year"]), pick(r, F["month"])
            if not (y and m):
                continue
            d = f"{int(float(y)):04d}{int(float(m)):02d}"
        area = pick(r, F["area"]) or pick(r, F["dong"])
        pop = pick(r, F["pop"])
        if not area or pop is None:
            continue
        try:
            agg[(str(area).strip(), f"{d[:4]}-{d[4:6]}")] += float(str(pop).replace(",", ""))
        except ValueError:
            continue
    months = sorted({k[1] for k in agg})[-24:]
    areas = sorted({k[0] for k in agg})
    out = {"months": months, "areas": []}
    for a in areas:
        vals = [round(agg.get((a, m), 0)) for m in months]
        last, prev = (vals[-1], vals[-2]) if len(vals) >= 2 else (vals[-1] if vals else 0, 0)
        yoy_base = vals[-13] if len(vals) >= 13 else None
        out["areas"].append({
            "area": a, "values": vals,
            "mom_pct": round((last - prev) / prev * 100, 1) if prev else None,
            "yoy_pct": round((last - yoy_base) / yoy_base * 100, 1) if yoy_base else None,
        })
    out["areas"].sort(key=lambda x: x["values"][-1] if x["values"] else 0, reverse=True)
    return out


def collect_footfall(url: str | None) -> tuple[dict | None, dict]:
    rows: list[dict] = []
    if url:
        for page in range(1, 51):
            u = re.sub(r"pageNo=\d+", f"pageNo={page}", url) if "pageNo=" in url else url
            r = requests.get(u, timeout=30)
            r.raise_for_status()
            try:
                js = r.json()
            except ValueError:
                raise RuntimeError("JSON 응답이 아닙니다. URL에 JSON 형식 옵션이 있는지 확인하세요.")
            got = find_records(js, must=("유동인구수", "FLPOP_CO", "POP_CNT", "FLOW_POP", "유동인구")) or []
            rows.extend(got)
            if "pageNo=" not in url or not got:
                break
        src = "서구 API"
    else:
        p = ROOT / C.FOOTFALL_MANUAL_CSV
        if not p.exists():
            return None, {"ok": False, "error": "SEOGU_FOOTFALL_URL 또는 data/manual/seogu_footfall.csv 가 없습니다."}
        with p.open(encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        src = "수동 CSV"
    data = parse_footfall_rows(rows)
    if not data["areas"]:
        return None, {"ok": False, "error": f"{src}에서 유동인구 행을 찾지 못했습니다 ({len(rows)}행). config.py의 필드 후보를 확인하세요."}
    return data, {"ok": True, "source": src, "rows": len(rows)}


# ------------------------------------------------------------------ 집계
def month_list(today: date, n: int = 12) -> list[str]:
    y, m = today.year, today.month
    out = []
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out[::-1]


def build_outputs(store: dict, newly: list[dict], today: date, trends: dict | None, footfall: dict | None,
                  status: dict, demo: bool, backfilled_now: set | None = None) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    backfilled_now = backfilled_now or set()
    recs = list(store["records"].values())
    for r in recs:
        if "category" not in r:
            r["category"], r["subtype"] = classify(r)
    months = month_list(today, 12)
    cut365 = (today - timedelta(days=365)).isoformat()

    def close_day(r):
        return (r.get("closed_date") or r.get("closed_detected") or "") if r.get("closed") else ""

    # 지도용 목록 (최근 1년 개업 또는 폐업, 모든 종류)
    map_items = []
    for r in recs:
        recent_open = (r.get("permit_date") or "") >= cut365
        recent_close = close_day(r) >= cut365
        if (recent_open or recent_close) and r.get("lat"):
            map_items.append({k: r.get(k) for k in (
                "name", "category", "subtype", "biz_type", "source", "permit_date", "closed", "closed_date",
                "road_addr", "lot_addr", "gu", "dong", "lat", "lng", "first_seen")})
    map_items.sort(key=lambda x: x.get("permit_date") or "", reverse=True)

    # 동별 x 종류별 월간 개업/폐업
    stats: dict[tuple[str, str], dict] = {}
    for r in recs:
        if not r.get("dong"):
            continue
        k = (r.get("gu", ""), r["dong"])
        s = stats.setdefault(k, {"gu": k[0], "dong": k[1], "lat": [], "lng": [],
                                 "cats": defaultdict(lambda: {"opens": defaultdict(int), "closes": defaultdict(int)})})
        if r.get("lat"):
            s["lat"].append(r["lat"])
            s["lng"].append(r["lng"])
        c = s["cats"][r["category"]]
        if r.get("permit_date"):
            c["opens"][r["permit_date"][:7]] += 1
        if close_day(r):
            c["closes"][close_day(r)[:7]] += 1
    trend_map = {a["area"]: a for a in (trends or {}).get("areas", [])}
    dong_rows = []
    for s in stats.values():
        cats = {}
        for cat, c in s["cats"].items():
            o = [c["opens"].get(m, 0) for m in months]
            cl = [c["closes"].get(m, 0) for m in months]
            if sum(o) + sum(cl):
                cats[cat] = {"opens": o, "closes": cl}
        if not s["lat"] or not cats:
            continue
        t = trend_map.get(s["dong"])
        dong_rows.append({
            "gu": s["gu"], "dong": s["dong"],
            "lat": round(sum(s["lat"]) / len(s["lat"]), 6), "lng": round(sum(s["lng"]) / len(s["lng"]), 6),
            "cats": cats,
            "trend_change8_pct": t["change8_pct"] if t else None,
            "trend_recent8": t["recent8"] if t else None,
        })

    # 알림용 신규 개업 (방식 a: 카페·베이커리는 개별, 식당·주점은 요약)
    def window_for(r):
        days = C.BACKFILL_ALERT_WINDOW_DAYS if r.get("via_backfill") else C.NEW_OPEN_WINDOW_DAYS
        return (today - timedelta(days=days)).isoformat()
    fresh = [r for r in newly if not r.get("closed") and (r.get("permit_date") or "") >= window_for(r)]
    fresh.sort(key=lambda x: x.get("permit_date") or "", reverse=True)
    keep = ("name", "category", "subtype", "biz_type", "permit_date", "road_addr", "lot_addr", "gu", "dong", "lat", "lng")
    detail = [{k: r.get(k) for k in keep} for r in fresh if r["category"] in C.ALERT_DETAIL_CATEGORIES]
    summary_block = {}
    for cat in C.ALERT_SUMMARY_CATEGORIES:
        rs = [r for r in fresh if r["category"] == cat]
        if rs:
            subs = defaultdict(int)
            for r in rs:
                subs[r.get("subtype") or "기타"] += 1
            summary_block[cat] = {"total": len(rs), "subtypes": dict(sorted(subs.items(), key=lambda x: -x[1])),
                                  "dongs": sorted({f"{r.get('gu','')} {r.get('dong','')}".strip() for r in rs})[:10]}
    by_cat = defaultdict(int)
    for r in fresh:
        by_cat[r["category"]] += 1

    cut30 = (today - timedelta(days=30)).isoformat()
    summary = {"new_today": len(fresh), "tracked": len(recs), "by_category": {}}
    for cat in CATEGORIES:
        rc = [r for r in recs if r["category"] == cat]
        summary["by_category"][cat] = {
            "new_today": by_cat.get(cat, 0),
            "opens30": sum(1 for r in rc if (r.get("permit_date") or "") >= cut30),
            "closes30": sum(1 for r in rc if close_day(r) >= cut30),
            "tracked": len(rc),
        }
    now = datetime.now(KST).isoformat(timespec="seconds")
    meta = {"generated_at": now, "date": today.isoformat(), "demo": demo, "months": months, "categories": CATEGORIES,
            "summary": summary, "sources": status, "backfill_from": C.BACKFILL_FROM,
            "backfilled_now": sorted(backfilled_now), "log": log_lines[-40:]}

    def dump(name, obj):
        (OUT / name).write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    dump("places.json", map_items)
    dump("dong_stats.json", {"months": months, "rows": dong_rows})
    openings = {"date": today.isoformat(), "generated_at": now, "demo": demo, "failed": False,
                "count": len(fresh), "by_category": dict(by_cat), "detail": detail, "summary": summary_block,
                "backfill_note": bool(backfilled_now)}
    dump("new_openings_latest.json", openings)
    # 예전 알림 형식 호환 (카페·베이커리만)
    dump("new_cafes_latest.json", {"date": today.isoformat(), "generated_at": now, "demo": demo,
                                   "count": len(detail), "items": detail})
    dump("trends.json", trends or {"periods": [], "areas": []})
    dump("footfall.json", footfall or {"months": [], "areas": []})
    dump("meta.json", meta)
    old_map = OUT / "cafes.json"
    if old_map.exists():
        old_map.unlink()
    log(f"완료: 신규 {len(fresh)}곳 (상세 {len(detail)}), 지도 {len(map_items)}곳, 동 {len(dong_rows)}곳")


# ------------------------------------------------------------------ demo
DEMO_DONGS = {
    ("동구", "소제동"): (36.3335, 127.4385, 3.0), ("중구", "대흥동"): (36.3253, 127.4245, 1.2),
    ("중구", "은행동"): (36.3290, 127.4275, 0.8), ("서구", "둔산동"): (36.3515, 127.3790, -1.5),
    ("서구", "갈마동"): (36.3555, 127.3660, 0.0), ("서구", "탄방동"): (36.3470, 127.3920, -0.5),
    ("유성구", "봉명동"): (36.3555, 127.3450, 2.0), ("유성구", "궁동"): (36.3620, 127.3510, -1.0),
    ("유성구", "어은동"): (36.3665, 127.3600, 0.5), ("유성구", "도룡동"): (36.3760, 127.3870, 1.0),
    ("유성구", "관평동"): (36.4250, 127.3900, 0.3), ("대덕구", "중리동"): (36.3640, 127.4210, -0.3),
}
DEMO_KINDS = [  # (이름 후보, 업태 후보, 소스 index)
    (["카페 오브", "고소한방앗간 커피", "로스터리 담", "커피 한모금", "카페 소담", "느린오후 카페"], ["커피숍", "다방", "까페"], 0),
    (["밀도 베이커리", "빵굽는집", "브레드랩", "동네제과"], ["제과점영업"], 2),
    (["한우마을", "소담식당", "청년국밥", "만복반점", "스시하루", "봉명분식", "숯불갈비집", "파스타공방"],
     ["한식", "한식", "중국식", "일식", "분식", "식육(숯불구이)", "경양식"], 1),
    (["맥주창고", "포차 한잔", "치맥하우스"], ["호프/통닭", "정종/대포집/소주방", "감성주점"], 1),
    (["GS25", "CU", "배스킨라빈스"], ["편의점", "아이스크림"], 0),
]
DEMO_NAMES = ["카페 오브", "고소한방앗간 커피", "로스터리 담", "커피 한모금", "카페 소담", "느린오후 카페", "브루잉랩",
              "카페 온기", "산책커피", "카페 결", "모닝빈 커피", "다락 카페", "호두나무 커피", "카페 봄날", "로스팅하우스 소제"]


def demo_raws(today: date) -> list[tuple[dict, dict]]:
    rnd = random.Random(42)
    out = []
    n = 0
    for (gu, dong), (lat, lng, heat) in DEMO_DONGS.items():
        for _ in range(rnd.randint(8, 22)):
            n += 1
            days_ago = int(rnd.random() ** (1.6 if heat > 0 else 0.8) * 360)
            pdate = today - timedelta(days=days_ago)
            closed = rnd.random() < (0.18 - heat * 0.04)
            cdate = (pdate + timedelta(days=rnd.randint(20, 300))) if closed else None
            if cdate and cdate > today:
                cdate = today - timedelta(days=rnd.randint(0, 20))
            x, y = TO_TM.transform(lng + rnd.uniform(-0.006, 0.006), lat + rnd.uniform(-0.004, 0.004))
            kind = rnd.choices(DEMO_KINDS, weights=[4, 1, 6, 2, 1])[0]
            nm, bz, src_i = kind
            raw = {
                "MNG_NO": f"DEMO{n:05d}", "BPLC_NM": f"{rnd.choice(nm)} {dong}점",
                "BZSTAT_SE_NM": rnd.choice(bz),
                "LCPMT_YMD": pdate.strftime("%Y%m%d"),
                "SALS_STTS_NM": "폐업" if closed else "영업/정상",
                "CLSBIZ_YMD": cdate.strftime("%Y%m%d") if cdate else "",
                "ROAD_NM_ADDR": f"대전광역시 {gu} 샘플로 {rnd.randint(1, 200)}, 1층 ({dong})",
                "LOTNO_ADDR": f"대전광역시 {gu} {dong} {rnd.randint(1, 900)}-{rnd.randint(1, 20)}",
                "XCRD": round(x, 3), "YCRD": round(y, 3),
            }
            out.append((raw, C.LICENSE_SOURCES[src_i]))
    # 어제 개업 2곳 (알림 테스트)
    for i, (gu, dong) in enumerate([("동구", "소제동"), ("유성구", "봉명동")]):
        lat, lng, _ = DEMO_DONGS[(gu, dong)]
        x, y = TO_TM.transform(lng + 0.001 * i, lat)
        out.append(({"MNG_NO": f"DEMONEW{i}", "BPLC_NM": ["고소한 오후 로스터스", "카페 누룽지"][i], "BZSTAT_SE_NM": "커피숍",
                     "LCPMT_YMD": (today - timedelta(days=1)).strftime("%Y%m%d"), "SALS_STTS_NM": "영업/정상",
                     "ROAD_NM_ADDR": f"대전광역시 {gu} 샘플로 {10 + i}, 1층 ({dong})",
                     "LOTNO_ADDR": f"대전광역시 {gu} {dong} {100 + i}", "XCRD": x, "YCRD": y}, C.LICENSE_SOURCES[0]))
    return out


def demo_trends(today: date) -> dict:
    rnd = random.Random(7)
    end = today - timedelta(days=today.weekday() + 1)
    periods = [(end - timedelta(weeks=w)).isoformat() for w in range(C.TREND_WEEKS, -1, -1)]
    heat = {d: h for (_, d), (_, _, h) in DEMO_DONGS.items()}
    series = {}
    for a in C.TREND_AREAS[:10]:
        base = rnd.uniform(8, 60)
        h = heat.get(a, 0)
        series[a] = {p: round(max(1, base * (1 + h * 0.012 * i) + rnd.uniform(-4, 4) + 6 * math.sin(i / 5)), 1)
                     for i, p in enumerate(periods)}
    return build_trend_output(series)


def demo_footfall(today: date) -> dict:
    rnd = random.Random(3)
    rows = []
    areas = {"둔산동 상권": (420000, -0.006), "갈마동 상권": (180000, 0.002), "탄방동 상권": (210000, -0.002),
             "월평동 상권": (150000, 0.004), "가수원 상가": (90000, 0.0)}
    y, m = today.year, today.month
    for k in range(24):
        for a, (base, g) in areas.items():
            for sex in ("남", "여"):
                rows.append({"기준년": y, "기준월": m, "상권명": a, "성별": sex,
                             "유동인구수": int(base / 2 * (1 - g * k) * rnd.uniform(0.93, 1.07))})
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return parse_footfall_rows(rows)


# ------------------------------------------------------------------ 대시보드 비밀번호
AUTH_ITER = 150_000


def write_auth() -> None:
    """DASHBOARD_PASSWORD 시크릿이 있으면 docs/auth.json 에 PBKDF2 해시만 저장 (원문은 저장 안 함).
    없으면 auth.json 을 지워 대시보드를 공개 상태로 둠."""
    import base64
    import hashlib as _h
    pw = os.environ.get("DASHBOARD_PASSWORD", "")
    path = ROOT / "docs" / "auth.json"
    if not pw:
        if path.exists():
            path.unlink()
        return
    salt = None
    if path.exists():
        try:
            salt = json.loads(path.read_text(encoding="utf-8")).get("salt")
        except Exception:  # noqa: BLE001
            salt = None
    salt = salt or base64.b64encode(os.urandom(16)).decode()
    dk = _h.pbkdf2_hmac("sha256", pw.encode("utf-8"), base64.b64decode(salt), AUTH_ITER)
    path.write_text(json.dumps({"salt": salt, "iter": AUTH_ITER, "hash": dk.hex()}), encoding="utf-8")
    log("대시보드 비밀번호 설정됨")


# ------------------------------------------------------------------ main
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="샘플 데이터로 실행 (API 키 불필요)")
    args = ap.parse_args()
    today = datetime.now(KST).date()

    if args.demo:
        store = {"records": {}}
        newly = []
        for raw, src in demo_raws(today):
            rec = normalize(raw, src)
            rec["category"], rec["subtype"] = classify(rec)
            rec["first_seen"] = today.isoformat() if raw["MNG_NO"].startswith("DEMONEW") else rec["permit_date"]
            store["records"][rec["id"]] = rec
            if raw["MNG_NO"].startswith("DEMONEW") or (rec["permit_date"] or "") >= (today - timedelta(days=2)).isoformat():
                newly.append(rec)
        status = {"rest": {"ok": True, "demo": True}, "general": {"ok": True, "demo": True}, "bakery": {"ok": True, "demo": True},
                  "trends": {"ok": True, "demo": True}, "footfall": {"ok": True, "demo": True}}
        build_outputs(store, newly, today, demo_trends(today), demo_footfall(today), status, demo=True)
        return

    write_auth()
    key = os.environ.get("DATA_GO_KR_KEY", "").strip()
    if not key:
        sys.exit("DATA_GO_KR_KEY 환경변수가 없습니다. GitHub Secrets에 등록하세요. (샘플 확인은 --demo)")
    store = json.loads(STORE.read_text(encoding="utf-8")) if STORE.exists() else {"records": {}}
    store.setdefault("records", {})

    newly, status, backfilled_now = collect_licenses(store, today, key)

    trends = None
    cid, sec = os.environ.get("NAVER_CLIENT_ID", "").strip(), os.environ.get("NAVER_CLIENT_SECRET", "").strip()
    if cid and sec:
        try:
            trends = collect_trends(today, cid, sec)
            status["trends"] = {"ok": True, "areas": len(trends["areas"])}
        except Exception as e:  # noqa: BLE001
            log(f"[검색 트렌드] 실패: {e}")
            status["trends"] = {"ok": False, "error": str(e)[:300]}
    else:
        status["trends"] = {"ok": False, "error": "NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 미등록"}
        prev = OUT / "trends.json"
        if prev.exists():
            trends = json.loads(prev.read_text(encoding="utf-8"))

    footfall = None
    try:
        footfall, status["footfall"] = collect_footfall(os.environ.get("SEOGU_FOOTFALL_URL", "").strip() or None)
    except Exception as e:  # noqa: BLE001
        log(f"[유동인구] 실패: {e}")
        status["footfall"] = {"ok": False, "error": str(e)[:300]}

    # 인허가 수집이 전부 실패하면 기존 결과를 덮어쓰지 않음
    if not any(v.get("ok") for k, v in status.items() if k in {s["key"] for s in C.LICENSE_SOURCES}):
        log("인허가 데이터 수집이 모두 실패해 결과 파일을 갱신하지 않습니다.")
        meta_p = OUT / "meta.json"
        old = json.loads(meta_p.read_text(encoding="utf-8")) if meta_p.exists() else {}
        old.update({"last_failure": datetime.now(KST).isoformat(timespec="seconds"),
                    "sources": status, "log": log_lines[-40:]})
        meta_p.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
        for fn in ("new_cafes_latest.json", "new_openings_latest.json"):
            (OUT / fn).write_text(json.dumps({
                "date": today.isoformat(), "failed": True, "count": 0, "items": [], "detail": [], "summary": {},
                "error": "인허가 API 수집 실패"}, ensure_ascii=False), encoding="utf-8")
        sys.exit(1)

    STORE.parent.mkdir(parents=True, exist_ok=True)
    store["last_run"] = today.isoformat()
    STORE.write_text(json.dumps(store, ensure_ascii=False), encoding="utf-8")
    build_outputs(store, newly, today, trends, footfall, status, demo=False, backfilled_now=backfilled_now)


if __name__ == "__main__":
    main()
