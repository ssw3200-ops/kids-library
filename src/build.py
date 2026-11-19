"""우리 동네 도서관 그림책 — 데이터 만들기

1) 정보나루 인기대출도서 loanItemSrch (연령별·아동도서, 전국 + 17개 시·도)
2) 정보나루 도서 소장 도서관 libSrchByBook (책 × 시·도) — 하루 한도 안에서 나눠 채우고 캐시
결과: docs/data/<시도>.enc.json (비밀번호 암호화), docs/meta.json, docs/status.json
캐시: docs/cache/ (공개 데이터만: 인기 목록, 소장 도서관, 도서관 정보, 호출 수)

사용: python src/build.py            (D4L_KEY, PAGE_PASSWORD 필요)
      python src/build.py --sample   (키 없이 샘플 데이터로 페이지 확인)
"""
from __future__ import annotations

import json
import os
import random
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

sys.path.insert(0, str(Path(__file__).parent))
from lock import encrypt  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
CACHE = DOCS / "cache"
KST = timezone(timedelta(hours=9))
API = "https://data4library.kr/api/"

# 화면 이름 → (정보나루 지역코드, 파일 이름)
REGIONS = {
    "서울": ("11", "seoul"), "부산": ("21", "busan"), "대구": ("22", "daegu"), "인천": ("23", "incheon"),
    "광주": ("24", "gwangju"), "대전": ("25", "daejeon"), "울산": ("26", "ulsan"), "세종": ("29", "sejong"),
    "경기": ("31", "gyeonggi"), "강원": ("32", "gangwon"), "충북": ("33", "chungbuk"), "충남": ("34", "chungnam"),
    "전북": ("35", "jeonbuk"), "전남": ("36", "jeonnam"), "경북": ("37", "gyeongbuk"), "경남": ("38", "gyeongnam"),
    "제주": ("39", "jeju"),
}

CFG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
TODAY = datetime.now(KST).date()
status: dict = {"calls": {}, "errors": [], "notes": []}


# ---------------------------------------------------------------- 캐시
def jload(name: str, default):
    p = CACHE / name
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return default


def jsave(name: str, obj) -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    (CACHE / name).write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


# ---------------------------------------------------------------- API 호출 (하루 한도 관리)
class Budget(Exception):
    pass


class Fatal(Exception):
    """키 미승인·잘못된 키 — 더 불러도 소용없음"""


class Client:
    def __init__(self, key: str):
        self.key = key
        u = jload("usage.json", {})
        self.used = u.get("calls", 0) if u.get("date") == TODAY.isoformat() else 0
        self.limit = int(CFG.get("daily_call_limit", 480))
        self.session = requests.Session()

    def left(self) -> int:
        return self.limit - self.used

    def save(self) -> None:
        jsave("usage.json", {"date": TODAY.isoformat(), "calls": self.used})

    def call(self, name: str, params: dict) -> ET.Element:
        if self.left() <= 0:
            raise Budget("오늘 호출 한도 도달")
        last = None
        for i in range(3):
            self.used += 1
            status["calls"][name] = status["calls"].get(name, 0) + 1
            try:
                r = self.session.get(API + name, params={**params, "authKey": self.key, "format": "xml"}, timeout=40)
                r.raise_for_status()
                root = ET.fromstring(r.content)
                err = root.findtext(".//error")
                if err:
                    msg = err.strip()[:120]
                    if re.search(r"활성화|인증|authKey|키", msg):
                        raise Fatal(f"정보나루 키 문제: {msg}")
                    raise RuntimeError(f"정보나루 오류: {msg}")
                time.sleep(0.25)
                return root
            except ET.ParseError:
                last = RuntimeError(f"XML 아님: {r.text[:120]!r}")
            except (RuntimeError, Fatal) as e:
                raise e  # 키·한도 오류는 다시 해도 같음
            except Exception as e:  # noqa: BLE001
                last = e
            if self.left() <= 0:
                break
            time.sleep(2 * (i + 1))
        raise last  # type: ignore[misc]


def rec(el: ET.Element) -> dict:
    return {c.tag: (c.text or "").strip() for c in el}


# ---------------------------------------------------------------- 인기 대출
def clean_author(a: str) -> str:
    a = re.sub(r"(지은이|글|그림|글·그림|글/그림|옮긴이|저자)\s*[:：]\s*", "", a or "")
    return re.sub(r"\s+", " ", a).strip(" ;,")


def https(u: str) -> str:
    return "https://" + u[7:] if u.startswith("http://") else u


def popular(cl: Client, age: str, size: int, region: str | None) -> list[dict]:
    end = TODAY - timedelta(days=1)
    start = end - timedelta(days=int(CFG.get("period_days", 30)) - 1)
    p = {"startDt": start.isoformat(), "endDt": end.isoformat(), "age": age,
         "addCode": CFG.get("add_code", "7"), "pageNo": 1, "pageSize": size}
    if region:
        p["region"] = region
    docs = [rec(d) for d in cl.call("loanItemSrch", p).iter("doc")]
    out = []
    for d in docs:
        isbn = re.sub(r"\D", "", d.get("isbn13", ""))
        if len(isbn) != 13 or any(x["isbn"] == isbn for x in out):
            continue
        out.append({
            "isbn": isbn, "title": d.get("bookname", ""), "author": clean_author(d.get("authors", "")),
            "pub": d.get("publisher", ""), "year": d.get("publication_year", ""),
            "img": https(d.get("bookImageURL", "")), "loan": int(re.sub(r"\D", "", d.get("loan_count", "")) or 0),
        })
    return out


def collect_lists(cl: Client) -> dict:
    """{"N"|시도코드: {age: [book...]}} — 실패한 칸은 이전 캐시 유지"""
    old = jload("lists.json", {})
    lists: dict = {}
    keys = [("N", None, int(CFG["national_size"]))] + [(c, c, int(CFG["region_size"])) for c, _ in REGIONS.values()]
    for k, reg, size in keys:
        lists[k] = {}
        for a in CFG["ages"]:
            try:
                got = popular(cl, a["code"], size, reg)
                if not got:
                    raise RuntimeError("0건")
                lists[k][a["code"]] = got
            except (Budget, Fatal):
                raise
            except Exception as e:  # noqa: BLE001
                prev = old.get(k, {}).get(a["code"], [])
                lists[k][a["code"]] = prev
                status["errors"].append(f"인기대출 {k}/{a['code']}: {e} (이전 {len(prev)}건 유지)")
    jsave("lists.json", lists)
    return lists


# ---------------------------------------------------------------- 소장 도서관
def city_of(addr: str, reg_name: str) -> str:
    if reg_name == "세종":
        return "세종시"
    parts = (addr or "").split()
    for c in parts[1:3]:
        if re.search(r"(시|군|구)$", c):
            return c
    return ""


def num(v: str) -> float | None:
    try:
        f = float(v)
        return round(f, 5) if f else None
    except (TypeError, ValueError):
        return None


def holdings(cl: Client, isbn: str, region: str, libs: dict) -> list[str]:
    root = cl.call("libSrchByBook", {"isbn": isbn, "region": region, "pageNo": 1, "pageSize": 500})
    codes = []
    for el in root.iter("lib"):
        d = rec(el)
        code = d.get("libCode", "")
        if not code:
            continue
        codes.append(code)
        libs[code] = [d.get("libName", ""), d.get("address", ""), d.get("tel", ""), d.get("homepage", ""),
                      num(d.get("latitude", "")), num(d.get("longitude", ""))]
    found = root.findtext(".//numFound")
    if found and found.isdigit() and int(found) > len(codes):
        status["notes"].append(f"{region}/{isbn}: {found}곳 중 {len(codes)}곳만 받음")
    return codes


def books_for(lists: dict, rcode: str) -> list[str]:
    """그 지역 화면에 나오는 책 (순위 높은 것부터)"""
    seen, out = set(), []
    ages = [a["code"] for a in CFG["ages"]]
    rows = [lists.get(rcode, {}).get(a, []) for a in ages] + [lists.get("N", {}).get(a, []) for a in ages]
    for i in range(max((len(r) for r in rows), default=0)):
        for r in rows:
            if i < len(r) and r[i]["isbn"] not in seen:
                seen.add(r[i]["isbn"])
                out.append(r[i]["isbn"])
    return out


def fill_holdings(cl: Client, lists: dict) -> tuple[dict, dict]:
    hold = jload("holdings.json", {})   # {시도코드: {isbn: {"t": 날짜, "c": [libCode]}}}
    libs = jload("libs.json", {})       # {libCode: [이름, 주소, 전화, 홈페이지, 위도, 경도]}
    ttl = int(CFG.get("holding_ttl_days", 21))
    pri = [REGIONS[r][0] for r in CFG.get("priority_regions", []) if r in REGIONS]
    others = [c for c, _ in REGIONS.values() if c not in pri]
    want = {c: books_for(lists, c) for c, _ in REGIONS.values()}

    def missing(c):
        return [i for i in want[c] if i not in hold.get(c, {})]

    queue: list[tuple[str, str]] = []
    for c in pri:                                   # 1) 우선 지역 빈칸
        queue += [(c, i) for i in missing(c)]
    rest = {c: missing(c) for c in others}          # 2) 나머지 지역 빈칸 — 돌아가며 골고루
    for i in range(max((len(v) for v in rest.values()), default=0)):
        queue += [(c, v[i]) for c, v in rest.items() if i < len(v)]
    stale = []                                      # 3) 오래된 것 다시 확인
    for c, m in hold.items():
        for isbn, v in m.items():
            if isbn in want.get(c, []) and (TODAY - datetime.fromisoformat(v["t"]).date()).days >= ttl:
                stale.append((v["t"], c, isbn))
    queue += [(c, i) for _, c, i in sorted(stale)]

    done = fail = 0
    for c, isbn in queue:
        if cl.left() <= 0:
            break
        try:
            hold.setdefault(c, {})[isbn] = {"t": TODAY.isoformat(), "c": holdings(cl, isbn, c, libs)}
            done += 1
        except (Budget, Fatal):
            break
        except Exception as e:  # noqa: BLE001
            fail += 1
            if fail <= 5:
                status["errors"].append(f"소장 {c}/{isbn}: {e}")
            if fail >= 20:
                status["errors"].append("소장 조회 실패가 많아 중단")
                break
    status["holdings"] = {"checked_today": done, "failed": fail, "queue": len(queue)}
    jsave("holdings.json", hold)
    jsave("libs.json", libs)
    return hold, libs


# ---------------------------------------------------------------- 결과 파일
def build(lists: dict, hold: dict, libs: dict, password: str, sample: bool) -> None:
    (DOCS / "data").mkdir(parents=True, exist_ok=True)
    ages = [a["code"] for a in CFG["ages"]]
    meta_regions = {}
    for name, (rcode, fname) in REGIONS.items():
        books, used = {}, []
        lst = {}
        for a in ages:
            lst[a] = {}
            for kind, src in (("r", lists.get(rcode, {})), ("n", lists.get("N", {}))):
                rows = src.get(a, [])
                lst[a][kind] = [[b["isbn"], b["loan"]] for b in rows]
                for b in rows:
                    books[b["isbn"]] = [b["title"], b["author"], b["pub"], b["year"], b["img"]]
        idx, lib_rows, h = {}, [], {}
        for isbn in books:
            v = hold.get(rcode, {}).get(isbn)
            if v is None:
                continue
            ids = []
            for code in v["c"]:
                if code not in libs:
                    continue
                if code not in idx:
                    idx[code] = len(lib_rows)
                    n, addr, tel, home, lat, lon = libs[code]
                    lib_rows.append([code, n, addr, tel, home, lat, lon, city_of(addr, name)])
                ids.append(idx[code])
            h[isbn] = ids
            used.append(isbn)
        data = {"lists": lst, "books": books, "hold": h, "libs": lib_rows}
        enc = encrypt(data, password, compress=True)
        (DOCS / "data" / f"{fname}.enc.json").write_text(json.dumps(enc), encoding="utf-8")
        pts = [(r[5], r[6]) for r in lib_rows if r[5] and r[6]]
        meta_regions[name] = {
            "file": fname, "books": len(books), "checked": len(used), "libs": len(lib_rows),
            "lat": round(sum(p[0] for p in pts) / len(pts), 4) if pts else None,
            "lon": round(sum(p[1] for p in pts) / len(pts), 4) if pts else None,
        }
    end = TODAY - timedelta(days=1)
    start = end - timedelta(days=int(CFG.get("period_days", 30)) - 1)
    meta = {
        "updated": datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
        "period": f"{start:%m.%d}~{end:%m.%d}",
        "sample": sample,
        "ages": CFG["ages"],
        "priority": CFG.get("priority_regions", []),
        "regions": meta_regions,
    }
    (DOCS / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    status["updated"] = meta["updated"]
    status["regions"] = {r: f"소장 확인 {m['checked']}/{m['books']}권 · 도서관 {m['libs']}곳" for r, m in meta_regions.items()}
    (DOCS / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(status, ensure_ascii=False, indent=1))


# ---------------------------------------------------------------- 샘플
def sample_data() -> tuple[dict, dict, dict]:
    rnd = random.Random(7)
    words = ["달님", "곰돌이", "구름빵", "아기 토끼", "무지개", "공룡", "꼬마 기차", "수박", "고양이", "눈사람",
             "팬케이크", "우산", "펭귄", "사과", "자동차", "별", "코끼리", "목욕", "잠자리", "숲속"]
    ends = ["의 하루", " 어디 있니?", "랑 놀자", " 대모험", "는 잠이 안 와", " 친구", "가 좋아", " 냠냠"]
    pool = []
    for i in range(80):
        isbn = f"979110000{i:04d}"
        pool.append({"isbn": isbn, "title": rnd.choice(words) + rnd.choice(ends), "author": f"샘플 작가{i % 9 + 1}",
                     "pub": rnd.choice(["샘플출판", "그림책방", "꿈나무"]), "year": str(rnd.randint(2010, 2025)),
                     "img": "", "loan": 0})
    lists = {}
    for k in ["N"] + [c for c, _ in REGIONS.values()]:
        lists[k] = {}
        for a in CFG["ages"]:
            rows = [dict(b) for b in rnd.sample(pool, 20)]
            for j, b in enumerate(rows):
                b["loan"] = 900 - j * 35 + rnd.randint(0, 20)
            lists[k][a["code"]] = rows
    libs, hold = {}, {}
    base = {"29": (36.48, 127.26, "세종특별자치시 한누리대로"), "25": (36.35, 127.38, "대전광역시 서구 둔산로")}
    for rcode, (lat, lon, road) in base.items():
        codes = []
        for j in range(8):
            code = f"{rcode}{j:04d}"
            codes.append(code)
            libs[code] = [f"샘플 {'세종' if rcode == '29' else '대전'} {j + 1}도서관", f"{road} {100 + j}", "044-000-0000",
                          "https://example.com", round(lat + rnd.uniform(-.05, .05), 5), round(lon + rnd.uniform(-.05, .05), 5)]
        hold[rcode] = {}
        for isbn in books_for(lists, rcode):
            if rnd.random() < 0.85:
                hold[rcode][isbn] = {"t": TODAY.isoformat(), "c": rnd.sample(codes, rnd.randint(0, 6))}
    return lists, hold, libs


# ---------------------------------------------------------------- 실행
def has_real() -> bool:
    """이미 실제 데이터로 만든 페이지가 있는지"""
    meta = DOCS / "meta.json"
    if not meta.exists():
        return False
    m = json.loads(meta.read_text(encoding="utf-8"))
    return not m.get("sample") and any(r.get("books") for r in m.get("regions", {}).values())


def any_books(lists: dict) -> bool:
    return any(rows for v in lists.values() for rows in v.values())


def write_status() -> None:
    (DOCS / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(status, ensure_ascii=False, indent=1))


def main() -> None:
    sample = "--sample" in sys.argv
    password = os.environ.get("PAGE_PASSWORD", "").strip() or ("sample" if sample else "")
    key = os.environ.get("D4L_KEY", "").strip()
    if not password:
        print("PAGE_PASSWORD 가 아직 없어요 (GitHub Secrets) — 이번 실행은 건너뜀")
        return

    def fallback(why: str) -> None:
        """실제 데이터가 아직 없으면 샘플로, 있으면 그대로 두고 실패 처리"""
        status["errors"].append(why)
        if has_real():
            write_status()
            sys.exit(why + " — 기존 데이터 유지")
        print(why + " — 샘플 데이터로 만들어요")
        lists, hold, libs = sample_data()
        build(lists, hold, libs, password, True)

    if sample:
        lists, hold, libs = sample_data()
        build(lists, hold, libs, password, True)
        return
    if not key:
        fallback("D4L_KEY 가 없어요 (정보나루 인증키를 Secrets 에 넣어주세요)")
        return
    cl = Client(key)
    status["budget_start"] = cl.left()
    try:
        lists = collect_lists(cl)
    except Fatal as e:
        cl.save()
        fallback(f"{e} — 정보나루 마이페이지에서 키 승인 상태를 확인해 주세요")
        return
    except Budget:
        status["errors"].append("오늘 호출 한도를 이미 다 써서 인기 목록을 못 받았어요 (내일 자동 실행 때 다시)")
        lists = jload("lists.json", {})
    if not any_books(lists):
        cl.save()
        fallback("인기 목록을 하나도 못 받았어요")
        return
    hold, libs = fill_holdings(cl, lists)
    cl.save()
    status["calls_today"] = cl.used
    build(lists, hold, libs, password, False)


if __name__ == "__main__":
    main()
