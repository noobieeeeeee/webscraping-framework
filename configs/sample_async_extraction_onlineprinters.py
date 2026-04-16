import re
import os
import csv
import glob
import asyncio
import httpx
from bs4 import BeautifulSoup
from urllib.parse import urlencode
from pathlib import Path
from typing import Any
from datetime import datetime

# ----- CONFIG -----
IN_CSV = "onlineprinters_brochures_kombination_redone_normalized.csv"

OUT_DIR = Path("out_parts_broschueren_klammerheftung_testing-1")
OUT_DIR.mkdir(exist_ok=True)

POST_COUNT = 0
POST_COUNT_LOCK = asyncio.Lock()

WORKERS_PER_VARIANT = 3
GLOBAL_MAX_INFLIGHT = 28
BASE_DELAY_S = 0.15
MAX_RETRIES = 3
LOG_EVERY_N = 25

PROGRESS: dict[tuple[str, int], dict[str, int]] = {}
PROGRESS_LOCK = asyncio.Lock()

RETRY_FAILED = True  # True = re-run UIDs in failed_*.csv, False = skip them too    

WARNINGS_PATH = OUT_DIR / "warnings_cover_mismatch.csv"
WARNINGS_LOCK = asyncio.Lock()

WARN_COUNTER = {
    "cover_mismatch": 0,
}
WARN_COUNTER_LOCK = asyncio.Lock()

VariantKey = tuple[str, str, str]  # (Type, Ausrichtung, Format)

URL_BY_VARIANT: dict[VariantKey, str] = {
    ("broschueren-klammerheftung", "Hochformat", "DIN A3"): "https://www.onlineprinters.de/p/broschueren-klammerheftung-din-a3",
    ("broschueren-klammerheftung", "Hochformat", "DIN A4"): "https://www.onlineprinters.de/p/broschueren-klammerheftung-din-a4",
    ("broschueren-klammerheftung", "Hochformat", "DIN A5"): "https://www.onlineprinters.de/p/broschueren-klammerheftung-din-a5",
    ("broschueren-klammerheftung", "Hochformat", "DIN A6"): "https://www.onlineprinters.de/p/broschueren-klammerheftung-din-a6",
    ("broschueren-klammerheftung", "Hochformat", "DIN Lang"): "https://www.onlineprinters.de/p/broschueren-klammerheftung-din-lang",
    ("broschueren-klammerheftung", "Hochformat", "DIN Lang Spezial"): "https://www.onlineprinters.de/p/broschueren-klammerheftung-din-lang-spezial",
    ("broschueren-klammerheftung", "Querformat", "DIN A4"): "https://www.onlineprinters.de/p/broschueren-klammerheftung-querformat-a4",
    ("broschueren-klammerheftung", "Querformat", "DIN A5"): "https://www.onlineprinters.de/p/broschueren-klammerheftung-querformat-a5",
    ("broschueren-klammerheftung", "Querformat", "DIN A6"): "https://www.onlineprinters.de/p/broschueren-klammerheftung-querformat-a6",
    ("broschueren-klammerheftung", "Querformat", "DIN Lang"): "https://www.onlineprinters.de/p/broschueren-klammerheftung-querformat-din-lang",
    ("broschueren-klammerheftung", "Querformat", "DIN Lang Spezial"): "https://www.onlineprinters.de/p/broschueren-klammerheftung-querformat-din-lang-spezial",
    ("broschueren-klammerheftung", "Quadrat", "A3 Quadrat"): "https://www.onlineprinters.de/p/broschueren-klammerheftung-quadratisch-a3-quadrat",
    ("broschueren-klammerheftung", "Quadrat", "A4 Quadrat"): "https://www.onlineprinters.de/p/broschueren-klammerheftung-quadratisch-a4-quadrat",
    ("broschueren-klammerheftung", "Quadrat", "A5 Quadrat"): "https://www.onlineprinters.de/p/broschueren-klammerheftung-quadratisch-a5-quadrat",
    ("broschueren-klammerheftung", "Quadrat", "A6 Quadrat"): "https://www.onlineprinters.de/p/broschueren-klammerheftung-quadratisch-a6-quadrat",
}

# NOTE: Ensure these are correct; wrong prod/cover will cause normalization.
KEYS_BY_VARIANT: dict[VariantKey, dict[str, str]] = {
    ("broschueren-klammerheftung", "Hochformat", "DIN A3"): {"prod": "PBRA344", "cover": "ZBRA301U"},
    ("broschueren-klammerheftung", "Hochformat", "DIN A4"): {"prod": "PBRA444", "cover": "ZBRA401U"},
    ("broschueren-klammerheftung", "Hochformat", "DIN A5"): {"prod": "PBRA544", "cover": "ZBRA501U"},
    ("broschueren-klammerheftung", "Hochformat", "DIN A6"): {"prod": "PBRA644", "cover": "ZBRA601U"},
    ("broschueren-klammerheftung", "Hochformat", "DIN Lang"): {"prod": "PBRDL44", "cover": "ZBRDL01U"},
    ("broschueren-klammerheftung", "Hochformat", "DIN Lang Spezial"): {"prod": "PBRDF44", "cover": "ZBRDF01U"},
    ("broschueren-klammerheftung", "Quadrat", "A3 Quadrat"): {"prod": "PQBQ344", "cover": "ZBRQ325U"},
    ("broschueren-klammerheftung", "Quadrat", "A4 Quadrat"): {"prod": "PQBQ444", "cover": "ZBRQ425U"},
    ("broschueren-klammerheftung", "Quadrat", "A5 Quadrat"): {"prod": "PQBQ544", "cover": "ZBRQ525U"},
    ("broschueren-klammerheftung", "Quadrat", "A6 Quadrat"): {"prod": "PQBQ644", "cover": "ZBRQ625U"},
    ("broschueren-klammerheftung", "Querformat", "DIN A4"): {"prod": "PBQA444", "cover": "ZBRA407U"},
    ("broschueren-klammerheftung", "Querformat", "DIN A5"): {"prod": "PBQA544", "cover": "ZBRA507U"},
    ("broschueren-klammerheftung", "Querformat", "DIN A6"): {"prod": "PBQA644", "cover": "ZBRA607U"},
    ("broschueren-klammerheftung", "Querformat", "DIN Lang"): {"prod": "PBQDL44", "cover": "ZBRDL07U"},
    ("broschueren-klammerheftung", "Querformat", "DIN Lang Spezial"): {"prod": "PBQDF44", "cover": "ZBRDF07U"},
}

# DO NOT CHANGE (per your instruction)
HEADERS_GET = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
# DO NOT CHANGE (per your instruction)
HEADERS_POST_BASE = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": "https://www.onlineprinters.de",
    "content-type": "application/WS-targetISO-8859-1xWS-target",
}

default_quantities = [
    50, 100, 150, 200, 250, 300, 400, 500, 600, 700, 750, 800, 900, 1000,
    1250, 1500, 1750, 2000, 2500, 3000, 3500, 4000, 4500, 5000, 5500, 6000,
    6500, 7000, 7500, 8000, 8500, 9000, 9500, 10000, 11000, 12000, 13000,
    14000, 15000, 16000, 17000, 18000, 19000, 20000, 25000, 30000, 40000,
    50000, 60000, 70000, 80000, 90000, 100000,
]
# default_quantities = [
#     19000
# ]

# ----------------- progress / resume -----------------

async def log_progress(
    label: str,
    worker_id: int,
    ok_inc: int = 0,
    fail_inc: int = 0,
    msg: str | None = None,
) -> None:
    async with PROGRESS_LOCK:
        key = (label, worker_id)
        p = PROGRESS.setdefault(key, {"ok": 0, "fail": 0})
        p["ok"] += ok_inc
        p["fail"] += fail_inc
        ok = p["ok"]
        fail = p["fail"]

    if msg:
        print(f"[{label} w{worker_id}] {msg} | ok={ok} fail={fail}")
    elif (ok + fail) % LOG_EVERY_N == 0:
        print(f"[{label} w{worker_id}]      : ok={ok} fail={fail}")


def load_done_uid_qty(out_dir: Path) -> set[tuple[str, str]]:
    done: set[tuple[str, str]] = set()
    for path in glob.glob(str(out_dir / "priced_*.csv")):
        with open(path, "r", encoding="utf-8", newline="") as f:
            r = csv.DictReader(f, delimiter=";")
            for row in r:
                uid = (row.get("UID") or "").strip()
                qty = (row.get("Auflage") or "").strip()
                if uid and qty:
                    done.add((uid, qty))
    return done


def load_failed_uid_qty(out_dir: Path) -> set[tuple[str, str]]:
    failed: set[tuple[str, str]] = set()
    for path in glob.glob(str(out_dir / "failed_*.csv")):
        with open(path, "r", encoding="utf-8", newline="") as f:
            r = csv.DictReader(f, delimiter=";")
            for row in r:
                uid = (row.get("UID") or "").strip()
                err = row.get("error") or ""
                m = re.search(r"\bqty=(\d+)\b", err)
                if uid and m:
                    failed.add((uid, m.group(1)))
    return failed


# ----------------- payload building -----------------

def build_payload_pairs_from_get(html: str) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, "lxml")
    form = soup.select_one("form#productForm")
    if not form:
        raise RuntimeError("form#productForm not found")

    payload: list[tuple[str, str]] = []
    for inp in form.select("input[name]"):
        name = inp.get("name")
        if not name:
            continue

        if name == "js_dep_var":
            payload.append((name, "ajax"))
            continue

        if "preflight" in name or "button" in name:
            continue

        value = inp.get("value", "")
        itype = (inp.get("type") or "").lower()
        if itype in ("radio", "checkbox"):
            if inp.has_attr("checked"):
                payload.append((name, value))
        else:
            payload.append((name, value))

    return payload


def set_cover_dynamic(
    pairs: list[tuple[str, str]],
    cover_input_name: str,
    cover_value: str,
) -> list[tuple[str, str]]:
    """
    Sets the cover using a dynamically learned key like:
      cover_input_name = "input_var_ZBRA313U_1_2"
    Also removes any stale "input_var_ZBRA..._1_2" keys to avoid sending both.
    """
    out: list[tuple[str, str]] = []
    for k, v in pairs:
        # drop any old cover group keys (they can change)
        if re.fullmatch(r"input_var_ZBR[0-9A-Z]+_1_2", k):
            continue
        out.append((k, v))
    # now set the active one
    out = set_all(out, cover_input_name, cover_value)
    return out


def set_all(pairs: list[tuple[str, str]], key: str, value: str) -> list[tuple[str, str]]:
    found = False
    out: list[tuple[str, str]] = []
    for k, v in pairs:
        if k == key:
            out.append((k, value))
            found = True
        else:
            out.append((k, v))
    if not found:
        out.append((key, value))
    return out


def set_qty_in_pairs(pairs: list[tuple[str, str]], prod: str, qty: int) -> list[tuple[str, str]]:
    """
    Quantity logic (keep your current heuristic):
    - qty < 20000: set tile quantity: input_var_{prod}_3_1 = qty
    - qty >= 20000:
        - set input_var_{prod}_3_1 = Interpolation
        - set the *second* input_qty_1 after the marker to qty
        - drop input_qty_1 fields before marker
    """
    key_qty_mode = f"input_var_{prod}_3_1"

    if qty < 20000:
        return set_all(pairs, key_qty_mode, str(qty))

    new_payload: list[tuple[str, str]] = []
    seen_marker = False
    qty_counter = 0
    typed_set = False

    for k, v in pairs:
        if k == key_qty_mode:
            seen_marker = True
            new_payload.append((k, "Interpolation"))
            continue

        if k == "input_qty_1":
            if not seen_marker:
                continue

            qty_counter += 1
            if qty_counter == 1:
                continue
            if qty_counter == 2:
                new_payload.append((k, str(qty)))
                typed_set = True
                continue

        new_payload.append((k, v))

    if not typed_set:
        raise RuntimeError("set_qty_in_pairs: did not set typed qty (structure changed?)")
    return new_payload


def normalize_option_text(s: str) -> str:
    if not s:
        return s
    # common mojibake for "²" when UTF-8 was decoded as cp1252
    s = s.replace("mï¿½", "m²")
    s = s.replace("g/mï¿½", "g/m²")
    return s




# ----------------- response parsing / verification -----------------

def verify_cover_from_json(j: dict[str, Any], cover_input_name: str, cover_value: str) -> bool:
    """
    Validates in WS-Ajax-essentialsAdditionalOptionsAjax that:
      - there is an <input ... name="{cover_input_name}" ... checked ...>
      - and that checked input has value == cover_value
    """
    html = j.get("WS-Ajax-additionalOptionAjax", "")
    if not isinstance(html, str) or not html:
        return False

    # Find the checked input for this cover group
    m = re.search(
        rf'<input[^>]*\bname="{re.escape(cover_input_name)}"[^>]*\bchecked\b[^>]*\bvalue="([^"]*)"',
        html
    )
    if not m:
        return False

    checked_value = m.group(1)
    return checked_value == cover_value


# def cover_value_exists_in_response(j: dict[str, Any], cover_input_name: str, cover_value: str) -> bool:
#     html = j.get("WS-Ajax-additionalOptionAjax", "")
#     if not isinstance(html, str) or not html:
#         return False
#     return bool(re.search(
#         rf'\bname="{re.escape(cover_input_name)}"[^>]*\bvalue="{re.escape(cover_value)}"\b',
#         html
#     ))
    
def cover_value_exists_in_response(j: dict[str, Any], cover_input_name: str, cover_value: str) -> bool:
    html = j.get("WS-Ajax-additionalOptionAjax", "")
    if not isinstance(html, str) or not html:
        return False

    for m in re.finditer(
        rf'(<input[^>]*\bname="{re.escape(cover_input_name)}"[^>]*>)',
        html
    ):
        tag = m.group(1)
        mval = re.search(r'\bvalue="([^"]*)"', tag)
        if mval and mval.group(1) == cover_value:
            return True
    return False


def get_checked_cover_value(j: dict[str, Any], cover_input_name: str) -> str | None:
    html = j.get("WS-Ajax-additionalOptionAjax", "")
    html1 = j.get("WS-Ajax-essentialsAdditionalOptionsAjax")
    
    # if not isinstance(html, str) or not html:
    #     return None

    # Find the input tag for this name that is checked (attribute order independent)
    m = re.search(
        rf'(<input[^>]*\bname="{re.escape(cover_input_name)}"[^>]*\bchecked\b[^>]*>)',
        html
    )
    m1 = re.search(
        rf'(<input[^>]*\bname="{re.escape(cover_input_name)}"[^>]*\bchecked\b[^>]*>)',
        html1
    )
    # print("In get_checked_cover_value: m",m," m1",m1)
    if (m!=None) and (m1!=None):
        # print("here? gone?")
        return None

    tag = m.group(1) if m else m1.group(1)
    m2 = re.search(r'\bvalue="([^"]*)"', tag)
    return m2.group(1) if m2 else None


def verify_cover_from_json(j: dict[str, Any], cover_input_name: str, cover_value: str) -> bool:
    got = get_checked_cover_value(j, cover_input_name)
    return got == cover_value



def extract_active_cover_input_name(j: dict[str, Any]) -> str | None:
    """
    Returns something like: input_var_ZBRA313U_1_2
    from WS-Ajax-essentialsAdditionalOptionsAjax.Before is wrong, it is actually in WS-Ajax-additionalOptionAjax
    """
    # 
    html = j.get("WS-Ajax-additionalOptionAjax", "")
    # if len(html)<10:
    #     print("ehre?")
    html1 = j.get("WS-Ajax-essentialsAdditionalOptionsAjax")
    # print("first:",html)
    Path("debug_essentialsAdditionalOptionsAjax.html").write_text(html if isinstance(html, str) else repr(html), encoding="utf-8")
    # if not isinstance(html, str) or not html or not html1:
    #     print("bomb")
    #     return None

    # Find the first occurrence of a cover-like input_var_ZBRA..._1_2
    m = re.search(r'\bname="(input_var_ZBR[0-9A-Z]+_1_2)"', html)
    m1 = re.search(r'\bname="(input_var_ZBR[0-9A-Z]+_1_2)"', html1)
    # print("m:",m," m1:",m1)
    # return m.group(1) if m else None
    if m != None:    
        # print("Found in additionalOptionAjax")
        return m.group(1) 
    if m1!= None:
        # print("Found in WS-Ajax-essentialsAdditionalOptionsAjax")
        return m1.group(1) 
    # print("Nothing found")
    return None



def parse_interpolation_state_from_json(j: dict[str, Any], prod: str) -> tuple[bool, int | None]:
    """
    Returns (is_interpolation_checked, returned_qty_value)
    returned_qty_value is parsed from the number input input_qty_1 value="...".
    """
    html = j.get("WS-Ajax-productConfigContentAjax3", "")
    if not isinstance(html, str) or not html:
        return False, None

    is_checked = bool(re.search(
        rf'name="input_var_{re.escape(prod)}_3_1"[^>]*checked[^>]*value="Interpolation"',
        html
    ))

    m = re.search(
        r'<input[^>]*\btype="number"[^>]*\bname="input_qty_1"[^>]*\bvalue="(\d+)"',
        html,
    )
    returned_qty = int(m.group(1)) if m else None
    return is_checked, returned_qty

def verify_typed_qty_from_json(j: dict[str, Any], prod: str, qty: int) -> bool:
    html = j.get("WS-Ajax-productConfigContentAjax3", "")
    if not isinstance(html, str) or not html:
        return False

    checked = bool(
        re.search(
            rf'name="input_var_{re.escape(prod)}_3_1"[^>]*checked[^>]*value="Interpolation"',
            html,
        )
    )
    if not checked:
        return False

    m = re.search(
        r'<input[^>]*\btype="number"[^>]*\bname="input_qty_1"[^>]*\bvalue="(\d+)"',
        html,
    )
    if not m:
        return False

    return int(m.group(1)) == int(qty)


def extract_next_setlink(j: dict[str, Any]) -> str | None:
    init = j.get("WS-Ajax-initLogicAjax", "")
    if not isinstance(init, str):
        return None
    m = re.search(r"productPageObj\.(?:currentLink|setLink)='([^']+)'", init)
    if not m:
        return None
    return m.group(1).replace("&amp;", "&")


def parse_net_gross(j: dict[str, Any]) -> tuple[str | None, str | None]:
    total = j.get("WS-Ajax-productConfigTotalPriceAjax", "")
    if not isinstance(total, str):
        return None, None

    m_net = re.search(r'netto</span><span[^>]*>\s*&euro;\s*([^<]+)<', total)
    m_gross = re.search(r'productConfigTotalPriceBruttoPrice"[^>]*>\s*&euro;\s*([^<]+)<', total)

    return (
        m_net.group(1).strip() if m_net else None,
        m_gross.group(1).strip() if m_gross else None,
    )


# ----------------- HTTP -----------------

async def post_pairs(
    client: httpx.AsyncClient,
    url: str,
    headers_post: dict[str, str],
    pairs: list[tuple[str, str]],
    sem: asyncio.Semaphore,
) -> dict[str, Any]:
    # DO NOT CHANGE ENCODING (per your instruction)
    global POST_COUNT
    # print("\n",pairs)
    body = urlencode(pairs).encode("iso-8859-1")

    async with sem:
        await asyncio.sleep(BASE_DELAY_S)
        async with POST_COUNT_LOCK:
            POST_COUNT += 1
            n = POST_COUNT
        r = await client.post(url, headers=headers_post, content=body)
        # print("JSON keys:", [k for k,v in r.json().items() if isinstance(v, str) and v])
        # print("\nCover extracted:",extract_active_cover_input_name(r.json()))

    ct = r.headers.get("content-type", "")
    if r.status_code != 200 or "application/json" not in ct:
        raise RuntimeError(f"POST failed: {r.status_code} {ct} {r.text[:200]}")
    return r.json()


# ----------------- pricing logic -----------------

def build_configured_pairs(
    *,
    base_pairs: list[tuple[str, str]],
    setlink: str,
    prod: str,
    # cover: str,
    row: dict[str, str],
) -> list[tuple[str, str]]:
    """Sets non-qty fields + SetLink; qty is applied separately."""
    # print("Hello???")
    p = set_all(base_pairs, "SetLink", setlink)
    p = set_all(p, f"input_var_{prod}_2_1", (row.get("Innenteil") or "").strip())
    # p = set_all(p, f"input_var_{cover}_1_2", normalize_option_text((row.get("Zusätzlicher_Umschlag") or "").strip()))
    p = set_all(p, f"input_var_{prod}_1_1",  ((row.get("Papier") or "").strip()))
    # print("Zusätzlicher_Umschlag:",row.get("Zusï¿½tzlicher_Umschlag") or "")
    return p


async def post_more_then_type_qty_if_needed(
    client: httpx.AsyncClient,
    variant: VariantKey,
    url: str,
    headers_post: dict[str, str],
    base_pairs: list[tuple[str, str]],
    setlink_anchor: str,
    row: dict[str, str],
    qty: int,
    cover_input_name: str,
    sem: asyncio.Semaphore,
) -> tuple[dict[str, str], str | None, str]:
    """
    Returns (priced_row, next_setlink_from_last_post, updated_cover_input_name)
    """
    keys = KEYS_BY_VARIANT[variant]
    prod = keys["prod"]
    typ, ori, fmt = variant

    # IMPORTANT: your CSV header is mojibake, so use that key unless you normalized earlier
    cover_value = ((row.get("Zusätzlicher_Umschlag") or "").strip())
    
    async def post_and_price(pairs: list[tuple[str, str]]) -> tuple[dict[str, Any], dict[str, str], str | None]:
        j = await post_pairs(client, url, headers_post, pairs, sem)
        net, gross = parse_net_gross(j)
        next_link = extract_next_setlink(j)
        priced = {
            "UID": (row.get("UID") or "").strip(),
            "Type": typ,
            "Ausrichtung": ori,
            "Format": fmt,
            "Innenteil": row.get("Innenteil", ""),
            "Zusätzlicher_Umschlag": row.get("Zusätzlicher_Umschlag", ""),
            "Papier": row.get("Papier", ""),
            "Auflage": str(qty),
            "net": net or "",
            "gross": gross or "",
        }
        return j, priced, next_link

    def update_cover_key_from_json(j: dict[str, Any], current: str) -> str:
        new_name = extract_active_cover_input_name(j)
        if new_name and new_name != current:
            print(f"[cover-key-change] UID={(row.get('UID') or '').strip()} {current} -> {new_name}")
            return new_name
        return current

    def build_configured_with_cover(setlink: str) -> list[tuple[str, str]]:
        p = build_configured_pairs(
            base_pairs=base_pairs,
            setlink=setlink,
            prod=prod,
            row=row,
        )
        # apply cover using current dynamic key
        p = set_cover_dynamic(p, cover_input_name, cover_value)
        return p
    
    def cover_ok(j: dict[str, Any]) -> bool:
        got = get_checked_cover_value(j, cover_input_name)
        return got == cover_value

    def cover_exists(j: dict[str, Any]) -> bool:
        return cover_value_exists_in_response(j, cover_input_name, cover_value)

    # ---------------- qty < 20000 ----------------
    if qty < 20000:
        configured = build_configured_with_cover(setlink_anchor)
        pairs = set_qty_in_pairs(configured, prod, qty)

        j, priced, next_link = await post_and_price(pairs)
        cover_input_name = update_cover_key_from_json(j, cover_input_name)
        
        if cover_value and cover_value != "kein Umschlag":
            if not verify_cover_from_json(j, cover_input_name, cover_value):
                # print("Values sent for got and exists:",cover_input_name,cover_value)
                got = get_checked_cover_value(j, cover_input_name)
                exists = cover_value_exists_in_response(j, cover_input_name, cover_value) or (got == cover_value)
                # print(f"[cover-mismatch] ... exists_in_options={exists}")
                print(
                    f"[cover-mismatch] UID={row.get('UID')} exists_in_options={exists} qty:{qty} "
                    f"cover_key={cover_input_name} requested={cover_value!r} got={got!r}"
                )
                uid = (row.get("UID") or "").strip()
                variant_str = f"{typ}|{ori}|{fmt}"

                await warn_cover_mismatch({
                    "ts": datetime.utcnow().isoformat(timespec="seconds"),
                    "UID": uid,
                    "variant": variant_str,
                    "qty": str(qty),
                    "prod": prod,
                    "setlink": setlink_anchor,              # or next1/interp_link depending on where you are
                    "cover_key": cover_input_name,
                    "requested_cover": cover_value,
                    "selected_cover": got or "",
                    "exists_in_options": "1" if exists else "0",
                    "note": "before_retry",                 # or "after_retry", "final", etc.
                })

                # if not exists:
                if got!=cover_value:
                    # PREP/RETRY: use next link to let server settle depvars
                    retry_link = next_link or setlink_anchor
                    configured_retry = build_configured_with_cover(retry_link)
                    pairs_retry = set_qty_in_pairs(configured_retry, prod, qty)
                    j_retry, priced_retry, next_retry = await post_and_price(pairs_retry)

                    cover_input_name = update_cover_key_from_json(j_retry, cover_input_name)

                    got2 = get_checked_cover_value(j_retry, cover_input_name)
                    exists2 = cover_value_exists_in_response(j_retry, cover_input_name, cover_value) or (got2 == cover_value)
                    print(f"[cover-retry] UID={row.get("UID")} exists_in_options={exists2} requested={cover_value!r} got={got2!r}")

                    # If retry succeeded, use retry response for pricing
                    if got2 == cover_value:
                        return priced_retry, next_retry, cover_input_name

        return priced, next_link, cover_input_name

    # ---------------- qty >= 20000: try single-post ----------------
    configured = build_configured_with_cover(setlink_anchor)
    pairs_try = set_qty_in_pairs(configured, prod, qty)

    j1, priced1, next1 = await post_and_price(pairs_try)
    cover_input_name = update_cover_key_from_json(j1, cover_input_name)
    
    if cover_value and cover_value != "kein Umschlag":
            if not verify_cover_from_json(j1, cover_input_name, cover_value):
                got = get_checked_cover_value(j1, cover_input_name)
                exists = cover_value_exists_in_response(j1, cover_input_name, cover_value) or (got == cover_value)
                print(
                    f"[cover-mismatch] UID={row.get('UID')} exists_in_options={exists} qty:{qty}"
                    f"cover_key={cover_input_name} requested={cover_value!r} got={got!r}"
                )
                uid = (row.get("UID") or "").strip()
                variant_str = f"{typ}|{ori}|{fmt}"

                await warn_cover_mismatch({
                    "ts": datetime.utcnow().isoformat(timespec="seconds"),
                    "UID": uid,
                    "variant": variant_str,
                    "qty": str(qty),
                    "prod": prod,
                    "setlink": next1,              # or next1/interp_link depending on where you are
                    "cover_key": cover_input_name,
                    "requested_cover": cover_value,
                    "selected_cover": got or "",
                    "exists_in_options": "1" if exists else "0",
                    "note": "before_retry",                 # or "after_retry", "final", etc.
                })
                # if not exists:
                if got!=cover_value:
                    # PREP/RETRY: use next link to let server settle depvars
                    retry_link = next1 or setlink_anchor
                    configured_retry = build_configured_with_cover(retry_link)
                    pairs_retry = set_qty_in_pairs(configured_retry, prod, qty)
                    j_retry, priced_retry, next_retry = await post_and_price(pairs_retry)

                    cover_input_name = update_cover_key_from_json(j_retry, cover_input_name)

                    got2 = get_checked_cover_value(j_retry, cover_input_name)
                    exists2 = cover_value_exists_in_response(j_retry, cover_input_name, cover_value) or (got2 == cover_value)
                    print(f"[cover-retry] UID={row.get("UID")} exists_in_options={exists2} requested={cover_value!r} got={got2!r}")

                    # If retry succeeded, use retry response for pricing
                    if got2 == cover_value:
                        return priced_retry, next_retry, cover_input_name

    if verify_typed_qty_from_json(j1, prod, qty):
        return priced1, next1, cover_input_name

    # log interpolation mismatch
    is_checked, returned_qty = parse_interpolation_state_from_json(j1, prod)
    print(
        f"[interp-mismatch] UID={(row.get('UID') or '').strip()} "
        f"variant={typ}|{ori}|{fmt} prod={prod} "
        f"requested_qty={qty} returned_qty={returned_qty} checked={is_checked} "
        f"net={priced1.get('net')} gross={priced1.get('gross')} "
        f"setlink_anchor={setlink_anchor} next_link={next1}"
    )

    # ---------------- fallback: "more" then typed qty ----------------
    # Step 1: enter interpolation branch (Interpolation only)
    configured_more = build_configured_with_cover(setlink_anchor)
    pairs_more = set_all(configured_more, f"input_var_{prod}_3_1", "Interpolation")

    j_more, _, link_more = await post_and_price(pairs_more)
    cover_input_name = update_cover_key_from_json(j_more, cover_input_name)

    interp_link = link_more or setlink_anchor

    # Step 2: typed qty in interpolation branch, using new SetLink
    configured2 = build_configured_with_cover(interp_link)
    pairs2 = set_qty_in_pairs(configured2, prod, qty)

    j2, priced2, next2 = await post_and_price(pairs2)
    cover_input_name = update_cover_key_from_json(j2, cover_input_name)

    if not verify_typed_qty_from_json(j2, prod, qty):
        is_checked2, returned_qty2 = parse_interpolation_state_from_json(j2, prod)
        print(
            f"[interp-mismatch-after-fallback] UID={(row.get('UID') or '').strip()} "
            f"variant={typ}|{ori}|{fmt} prod={prod} "
            f"requested_qty={qty} returned_qty={returned_qty2} checked={is_checked2} "
            f"interp_link={interp_link} next2={next2} "
            f"net={priced2.get('net')} gross={priced2.get('gross')}"
        )
        raise RuntimeError(f"Typed qty did not stick after fallback (prod={prod}, qty={qty})")

    return priced2, next2, cover_input_name


async def get_baseline_setlink(
    client: httpx.AsyncClient,
    url: str,
    headers_post: dict[str, str],
    base_pairs: list[tuple[str, str]],
    sem: asyncio.Semaphore,
) -> str:
    j0 = await post_pairs(client, url, headers_post, base_pairs, sem)
    link = extract_next_setlink(j0)
    if not link:
        raise RuntimeError("No setLink/currentLink in baseline response")
    return link


# ----------------- worker / orchestration -----------------



async def warn_cover_mismatch(row: dict[str, Any]) -> None:
    async with WARNINGS_LOCK:
        with open(WARNINGS_PATH, "a", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(
                f,
                fieldnames=[
                    "ts",
                    "UID",
                    "variant",
                    "qty",
                    "prod",
                    "setlink",
                    "cover_key",
                    "requested_cover",
                    "selected_cover",
                    "exists_in_options",
                    "note",
                ],
                delimiter=";",
            )
            w.writerow(row)

    async with WARN_COUNTER_LOCK:
        WARN_COUNTER["cover_mismatch"] += 1

def round_robin_chunks(lst: list[dict[str, str]], n: int) -> list[list[dict[str, str]]]:
    chunks: list[list[dict[str, str]]] = [[] for _ in range(n)]
    for i, item in enumerate(lst):
        chunks[i % n].append(item)
    return chunks


async def worker(
    variant: VariantKey,
    worker_id: int,
    rows: list[dict[str, str]],
    sem: asyncio.Semaphore,
    skip_pairs: set[tuple[str, str]],
) -> None:
    url = URL_BY_VARIANT[variant]
    headers_post = dict(HEADERS_POST_BASE)
    headers_post["Referer"] = url

    refresh_every = 200
    done_since_refresh = 0

    typ, ori, fmt = variant
    label = f"{typ}|{ori}|{fmt}"

    out_path = OUT_DIR / f"priced_{ori}_{fmt.replace(' ', '_')}_w{worker_id}.csv"
    fail_path = OUT_DIR / f"failed_{ori}_{fmt.replace(' ', '_')}_w{worker_id}.csv"
    print(f"[{label} w{worker_id}] starting. rows_assigned={len(rows)} out={out_path.name} fail={fail_path.name}")

    static_cover = KEYS_BY_VARIANT[variant]["cover"]
    static_cover_input_name = f"input_var_{static_cover}_1_2"
    cover_input_name = static_cover_input_name

    async with httpx.AsyncClient(http2=False, verify=False, follow_redirects=True, timeout=30) as client:
        r = await client.get(url, headers=HEADERS_GET)
        r.raise_for_status()
        base_pairs = build_payload_pairs_from_get(r.text)

        fieldnames = [
            "UID", "Type", "Ausrichtung", "Format",
            "Innenteil", "Zusätzlicher_Umschlag", "Papier",
            "Auflage", "net", "gross",
        ]

        out_exists = out_path.exists() and out_path.stat().st_size > 0
        fail_exists = fail_path.exists() and fail_path.stat().st_size > 0

        setlink_anchor = await get_baseline_setlink(client, url, headers_post, base_pairs, sem)

        # baseline refresh implies "state reset": start cover key guessing from static again
        cover_input_name = static_cover_input_name

        with (
            open(out_path, "a", newline="", encoding="utf-8") as f_out,
            open(fail_path, "a", newline="", encoding="utf-8") as f_fail,
        ):
            w_out = csv.DictWriter(f_out, fieldnames=fieldnames, delimiter=";")
            w_fail = csv.DictWriter(f_fail, fieldnames=["UID", "variant", "error"], delimiter=";")

            if not out_exists:
                w_out.writeheader()
            if not fail_exists:
                w_fail.writeheader()

            for row in rows:
                uid = (row.get("UID") or "").strip()

                for qty in default_quantities:
                    if (uid, str(qty)) in skip_pairs:
                        continue

                    for attempt in range(1, MAX_RETRIES + 1):
                        try:
                            priced, next_link, cover_input_name = await post_more_then_type_qty_if_needed(
                                client=client,
                                variant=variant,
                                url=url,
                                headers_post=headers_post,
                                base_pairs=base_pairs,
                                setlink_anchor=setlink_anchor,
                                row=row,
                                qty=qty,
                                cover_input_name=cover_input_name,
                                sem=sem,
                            )

                            w_out.writerow(priced)

                            # advance anchor if we got a next link
                            if next_link:
                                setlink_anchor = next_link

                            await log_progress(
                                label,
                                worker_id,
                                ok_inc=1,
                                msg=f"UID={uid} qty={qty} net={priced.get('net')} gross={priced.get('gross')}"
                                if (PROGRESS.get((label, worker_id), {"ok": 0, "fail": 0})["ok"] + 1) % LOG_EVERY_N == 0
                                else None,
                            )

                            done_since_refresh += 1
                            if done_since_refresh >= refresh_every:
                                # refresh anchor from server (one baseline POST)
                                setlink_anchor = await get_baseline_setlink(client, url, headers_post, base_pairs, sem)
                                done_since_refresh = 0

                                # reset cover key guess after baseline refresh
                                cover_input_name = static_cover_input_name
                                print(f"[{label} w{worker_id}] refreshed baseline; reset cover_input_name={cover_input_name}")

                            break

                        except Exception as e:
                            if attempt == MAX_RETRIES:
                                w_fail.writerow(
                                    {
                                        "UID": uid,
                                        "variant": label,
                                        "error": f"qty={qty} cover_key={cover_input_name} {str(e)[:250]}",
                                    }
                                )
                                await log_progress(
                                    label,
                                    worker_id,
                                    fail_inc=1,
                                    msg=f"UID={uid} qty={qty} FAILED: {str(e)[:120]}",
                                )

                                # optional: after hard failure, reset to static cover key guess
                                cover_input_name = static_cover_input_name

                            else:
                                print(
                                    f"[{label} w{worker_id}] UID={uid} qty={qty} attempt={attempt} "
                                    f"cover_key={cover_input_name} error={str(e)[:80]}"
                                )
                                await asyncio.sleep(0.5 * attempt)


async def main_async() -> None:
    done_pairs = load_done_uid_qty(OUT_DIR)
    failed_pairs = load_failed_uid_qty(OUT_DIR)
    
    def ensure_warnings_file():
        if not WARNINGS_PATH.exists() or WARNINGS_PATH.stat().st_size == 0:
            with open(WARNINGS_PATH, "w", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(
                    f,
                    fieldnames=[
                        "ts",
                        "UID",
                        "variant",
                        "qty",
                        "prod",
                        "setlink",
                        "cover_key",
                        "requested_cover",
                        "selected_cover",
                        "exists_in_options",
                        "note",
                    ],
                    delimiter=";",
                )
                w.writeheader()
                
    ensure_warnings_file()

    if RETRY_FAILED:
        skip_pairs = done_pairs
        print(f"Resuming: {len(done_pairs)} done (UID,Auflage); retrying {len(failed_pairs)} failed pairs")
    else:
        skip_pairs = done_pairs | failed_pairs
        print(f"Resuming: skipping {len(done_pairs)} done and {len(failed_pairs)} failed (UID,Auflage) pairs")

    with open(IN_CSV, "r", encoding="utf-8", newline="") as f:
        rows_all = list(csv.DictReader(f, delimiter=";"))

    groups: dict[VariantKey, list[dict[str, str]]] = {}
    skipped_pairs = 0
    total_pairs = 0
    
    HEADER_ALIASES = {
        "Zusï¿½tzlicher_Umschlag": "Zusätzlicher_Umschlag",
        "Ausfï¿½hrung Innenteil": "Ausführung Innenteil",
        # add more if you want to clean up other mojibake headers
    }

    def normalize_row_keys(row: dict[str, str]) -> dict[str, str]:
        out = dict(row)
        for bad, good in HEADER_ALIASES.items():
            if good not in out and bad in out:
                out[good] = out[bad]
        return out
    
    rows_all = [normalize_row_keys(r) for r in rows_all]
    
    # print("Rows all:",rows_all[0].keys())
    for r in rows_all:
        typ = (r.get("Type") or "").strip()
        if typ != "broschueren-klammerheftung":
            continue
        

        uid = (r.get("UID") or "").strip()
        ori = (r.get("Ausrichtung") or "").strip()
        fmt = (r.get("Format") or "").strip()
        
        if ori == "Querformat":
            continue
        
        variant: VariantKey = (typ, ori, fmt)

        if variant not in URL_BY_VARIANT:
            raise RuntimeError(f"Unknown variant {variant} (UID={uid})")
        if variant not in KEYS_BY_VARIANT:
            raise RuntimeError(f"Missing KEYS for variant {variant} (UID={uid})")

        # count skip pairs for reporting only
        for qty in default_quantities:
            # print("Row details:",qty,ori,r.get("Zusätzlicher_Umschlag"),r.get("Papier"))
            total_pairs += 1
            if (uid, str(qty)) in skip_pairs:
                skipped_pairs += 1

        groups.setdefault(variant, []).append(r)

    print(f"Skipping {skipped_pairs}/{total_pairs} already-completed (UID,Auflage) pairs")

    for variant, rs in groups.items():
        print(f"To process: {variant} -> {len(rs)} rows")

    sem = asyncio.Semaphore(GLOBAL_MAX_INFLIGHT)
    tasks: list[asyncio.Task[None]] = []
    for variant, variant_rows in groups.items():
        chunks = round_robin_chunks(variant_rows, WORKERS_PER_VARIANT)
        for wid, ch in enumerate(chunks):
            if ch:
                tasks.append(asyncio.create_task(worker(variant, wid, ch, sem, skip_pairs)))

    await asyncio.gather(*tasks)
    print(f"Total POST requests sent: {POST_COUNT}")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()