# -*- coding: utf-8 -*-
import os, time, random, json, threading, requests
from typing import Dict, Any
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from requests import HTTPError
from api_client import (
    _normalize_token, login, get_chart, place_order,
    day_history, pending_orders, BASE_URL, _headers  # <-- THÊM BASE_URL và _headers VÀO ĐÂY
)
from functools import wraps
from threading import Semaphore

# ========= Flask app (đúng thư mục của bạn) =========
app = Flask(
    __name__,
    template_folder="templates",   # thư mục HTML
    static_folder="media",         # phục vụ file tĩnh từ /media
    static_url_path="/media"       # URL public là /media/...
)
app.secret_key = os.getenv("SECRET_KEY", "dev_secret_change_me")

SYMBOL = os.getenv("SYMBOL", "BTCUSDT")
TYPE_MODE = os.getenv("TYPE_MODE", "live")
STEP_FALLBACK = 30

# ========= Ratio cache: random 55–75% theo window_id =========
RATIO_CACHE: Dict[str, int] = {}

# ========= COPYTRADE chạy máy chủ =========
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJjdXNPYmoiOnsiaWQiOjh9LCJpYXQiOjE3NTYwNDkxMzcsImV4cCI6NjE3NTYwNDkxMzd9.MI-GUCZJoJgH3B7L6c6MB6gD12zgNIlMMd10yi1QB8M")
ADMIN_API   = "https://fintech3.net/api/binaryOption/getOrderAdmin"
COPYTRADE_STATE_FILE = os.path.join(os.path.dirname(__file__), "copytrade_state.json")

_copy_enabled = False
_copy_cfg: Dict[str, Any] = {}          # lưu cả user_token để chạy khi user offline
_copy_thread: threading.Thread | None = None
_copy_stop = threading.Event()
_processed_ids = set()                   # tránh đặt trùng lệnh
_processed_lock = threading.Lock()       # bảo vệ _processed_ids khi đa luồng

# Giới hạn số request đồng thời
MAX_CONCURRENT = 10
request_sem = Semaphore(MAX_CONCURRENT)

def limit_concurrent(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not request_sem.acquire(timeout=5):  # timeout 5s
            return jsonify({"ok": False, "error": "Server busy"}), 503
        try:
            return f(*args, **kwargs)
        finally:
            request_sem.release()
    return decorated

# ----------------- helpers chung -----------------
def pick_token() -> str | None:
    raw = request.headers.get("X-Token") or session.get("token") or os.getenv("HITOKEN")
    return _normalize_token(raw)

def validate_auth():
    """Check if user has valid token, clear session if invalid"""
    token = pick_token()
    if not token:
        session.clear()
        return None
    return token

def kline_step(data) -> int:
    if not data or len(data) < 2:
        return STEP_FALLBACK
    try:
        return max(1, int(data[-1]["timestamp"] - data[-2]["timestamp"]))
    except Exception:
        return STEP_FALLBACK

def epoch_index(ts: int, step: int) -> int:
    return ts // step

def get_ratio_for(window_id: str) -> int:
    """Random 55–75% nhưng giữ nguyên cho cùng window_id (mọi thiết bị giống nhau)."""
    if window_id not in RATIO_CACHE:
        RATIO_CACHE[window_id] = random.randint(55, 75)
    return RATIO_CACHE[window_id]

# ---- chống khởi động 2 worker khi debug reloader ----
def _worker_should_start() -> bool:
    """
    Khi debug=True, Flask tạo 2 process (reloader). Chỉ process chính (WERKZEUG_RUN_MAIN=true) mới start worker.
    Prod (debug=False) thì start bình thường.
    """
    if app.debug and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        return False
    return True

# ---- khóa duy nhất cho 1 lệnh chuyên gia ----
def _order_key(row: dict) -> str:
    oid = row.get("id")
    if oid is not None:
        return f"id:{oid}"
    # fallback khi không có id
    return f"chart:{row.get('idChart')}-at:{row.get('created_at')}-user:{row.get('username') or ''}"

# ----------------- copytrade state I/O -----------------
def _load_copy_state():
    global _copy_enabled, _copy_cfg, _processed_ids
    try:
        with open(COPYTRADE_STATE_FILE, "r", encoding="utf-8") as f:
            js = json.load(f)
        _copy_enabled = bool(js.get("enabled"))
        _copy_cfg = js.get("cfg") or {}
        _processed_ids = set(js.get("processed_ids") or [])
    except Exception:
        pass

def _save_copy_state():
    try:
        with open(COPYTRADE_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "enabled": _copy_enabled,
                "cfg": _copy_cfg,
                "processed_ids": list(_processed_ids),
            }, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def _fetch_admin_orders(limit: int = 10, page: int = 1) -> list[dict]:
    """Gọi admin API ở server (không dính CORS)."""
    if not ADMIN_TOKEN or ADMIN_TOKEN.startswith("<PUT_"):
        return []
    try:
        r = requests.post(
            ADMIN_API,
            json={"limit": limit, "page": page},
            headers={
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {ADMIN_TOKEN}",
            },
            timeout=10,
        )
        r.raise_for_status()
        js = r.json()
        if js.get("status") and js.get("data", {}).get("array"):
            return js["data"]["array"]
    except Exception:
        pass
    return []

def _match_expert(row: dict, name: str) -> bool:
    """Khớp theo username hoặc email (không phân biệt hoa/thường)."""
    if not name:
        return False
    name = name.strip().lower()
    return (str(row.get("username") or "").lower() == name) or (str(row.get("email") or "").lower() == name)

def _get_user_info(token: str) -> Dict[str, Any] | None:
    """Lấy thông tin user từ token để khớp username khi refresh token"""
    try:
        r = requests.post(
            f"{BASE_URL}/api/user/getProfile",
            headers=_headers(token),
            timeout=10
        )
        if r.status_code == 200:
            js = r.json()
            if js.get("status") and js.get("data"):
                return js["data"]
    except Exception:
        pass
    return None

def _refresh_user_token(old_token: str, username: str) -> str | None:
    """Thử refresh token mới cho user có cùng username"""
    # Logic này cần được implement dựa trên cách backend handle token refresh
    # Hiện tại return None vì chưa có API refresh token
    return None

def _check_order_result(token: str, order_id: str, type_mode: str) -> Dict[str, Any] | None:
    """Kiểm tra kết quả lệnh đã hoàn thành"""
    try:
        # Gọi API lấy lịch sử để tìm order_id
        r = requests.post(
            f"{BASE_URL}/api/binaryOption/dayHistoryOrder",
            json={"type": type_mode, "limit": 50, "page": 1},
            headers=_headers(token),
            timeout=10
        )
        
        if r.status_code == 200:
            js = r.json()
            if js.get("status") and js.get("data", {}).get("array"):
                for order in js["data"]["array"]:
                    if str(order.get("idChart")) == str(order_id):
                        # Tính toán kết quả
                        draw = int(order.get("draw", 0))
                        if draw == 1:
                            return {"result": "draw", "pnl": 0}
                        
                        side = order.get("side", "").lower()
                        entry = float(order.get("entryPrice", 0))
                        close = float(order.get("closingPrice", 0))
                        amount = float(order.get("amount", 0))
                        ratio = float(order.get("configProfit", 0.8))
                        
                        if side == "buy":
                            win = close > entry
                        else:  # sell
                            win = close < entry
                        
                        if win:
                            pnl = amount * ratio  # Tiền thắng
                            return {"result": "win", "pnl": pnl}
                        else:
                            pnl = -amount  # Tiền thua
                            return {"result": "lose", "pnl": pnl}
    except Exception:
        pass
    return None

def _update_capital_after_trade(pnl: float):
    """Cập nhật vốn copytrade sau mỗi lệnh"""
    global _copy_cfg
    
    so_von = _copy_cfg.get("so_von", 0)
    so_von_ban_dau = _copy_cfg.get("so_von_ban_dau", so_von)
    so_tien_tai_khoan = _copy_cfg.get("so_tien_tai_khoan", 0)
    
    if pnl > 0:  # Thắng
        # Cộng tiền thắng vào tài khoản
        so_tien_tai_khoan += pnl
        
        # Nếu vốn chưa phục hồi về ban đầu, tăng vốn
        if so_von < so_von_ban_dau:
            tang_von = min(pnl, so_von_ban_dau - so_von)
            so_von += tang_von
    elif pnl < 0:  # Thua
        # Trừ tiền thua từ vốn copytrade
        so_von += pnl  # pnl âm nên dùng +
        
        # Đảm bảo vốn không âm
        if so_von < 0:
            so_von = 0
    
    # Cập nhật lại config
    _copy_cfg["so_von"] = so_von
    _copy_cfg["so_tien_tai_khoan"] = so_tien_tai_khoan
    
    # Kiểm tra điều kiện dừng
    if so_von <= 0:
        print(f"[COPYTRADE] Vốn đã hết, tự động dừng copytrade")
        _copy_cfg["enabled"] = False
        global _copy_enabled
        _copy_enabled = False
    
    _save_copy_state()
    return so_von > 0

def _check_tp_sl():
    """Kiểm tra chốt lời/lỗ"""
    global _copy_enabled, _copy_cfg
    
    so_tien_tai_khoan = _copy_cfg.get("so_tien_tai_khoan", 0)
    so_tien_ban_dau = _copy_cfg.get("so_tien_ban_dau", so_tien_tai_khoan)
    tp_target = _copy_cfg.get("tp", 0)
    sl_target = _copy_cfg.get("sl", 0)
    
    loi_nhuan_hien_tai = so_tien_tai_khoan - so_tien_ban_dau
    
    # Kiểm tra chốt lời
    if tp_target > 0 and loi_nhuan_hien_tai >= tp_target:
        print(f"[COPYTRADE] Đạt mức chốt lời ${tp_target}, tự động dừng")
        _copy_enabled = False
        _copy_cfg["enabled"] = False
        _save_copy_state()
        return False
    
    # Kiểm tra chốt lỗ
    if sl_target > 0 and loi_nhuan_hien_tai <= -sl_target:
        print(f"[COPYTRADE] Chạm mức chốt lỗ ${sl_target}, tự động dừng")
        _copy_enabled = False
        _copy_cfg["enabled"] = False
        _save_copy_state()
        return False
    
    return True

def _copy_worker():
    """Thread nền: 2s/lần quét lệnh chuyên gia, đặt lệnh cho user."""
    global _copy_enabled, _copy_cfg, _processed_ids
    
    pending_orders = {}  # {order_id: {"timestamp": ..., "amount": ...}}
    
    while not _copy_stop.is_set():
        if not _copy_enabled:
            time.sleep(1.0)
            continue

        cfg = _copy_cfg.copy()
        user_token = cfg.get("user_token")
        username = cfg.get("username", "")
        expert = (cfg.get("expert") or "").strip()
        type_mode = cfg.get("type") or TYPE_MODE
        so_tien_co_dinh = float(cfg.get("so_tien_co_dinh") or 0)
        so_von = float(cfg.get("so_von") or 0)

        # Kiểm tra cấu hình
        if not user_token or not expert or so_tien_co_dinh <= 0 or so_von <= 0:
            time.sleep(1.0)
            continue
        
        # Kiểm tra TP/SL trước khi tiếp tục
        if not _check_tp_sl():
            time.sleep(1.0)
            continue

        try:
            # Kiểm tra kết quả các lệnh pending
            completed_orders = []
            for order_id, order_info in pending_orders.items():
                result = _check_order_result(user_token, order_id, type_mode)
                if result:
                    print(f"[COPYTRADE] Lệnh {order_id}: {result['result']} PnL: ${result['pnl']}")
                    
                    # Cập nhật vốn
                    can_continue = _update_capital_after_trade(result["pnl"])
                    if not can_continue:
                        break
                    
                    completed_orders.append(order_id)
            
            # Xóa các lệnh đã hoàn thành
            for order_id in completed_orders:
                del pending_orders[order_id]
            
            # Nếu vốn hết, dừng ngay
            if not _copy_enabled:
                continue
            
            # Quét lệnh mới từ chuyên gia
            rows = _fetch_admin_orders(limit=10, page=1)
            if rows:
                for r in reversed(rows):
                    if not _match_expert(r, expert):
                        continue

                    key = _order_key(r)

                    # Kiểm tra đã xử lý chưa
                    with _processed_lock:
                        if key in _processed_ids:
                            continue
                        _processed_ids.add(key)
                        _save_copy_state()

                    side = str(r.get("side") or "").lower()
                    symbol = str(r.get("symbol") or SYMBOL)
                    
                    # Kiểm tra vốn còn đủ
                    current_so_von = _copy_cfg.get("so_von", 0)
                    if so_tien_co_dinh > current_so_von:
                        print(f"[COPYTRADE] Vốn không đủ: cần ${so_tien_co_dinh}, còn ${current_so_von}")
                        continue

                    try:
                        # Thử refresh token nếu cần
                        try:
                            # Test token hiện tại
                            test_r = requests.post(
                                f"{BASE_URL}/api/user/getProfile",
                                headers=_headers(user_token),
                                timeout=5
                            )
                            if test_r.status_code == 401:
                                # Token hết hạn, thử refresh
                                new_token = _refresh_user_token(user_token, username)
                                if new_token:
                                    _copy_cfg["user_token"] = new_token
                                    user_token = new_token
                                    _save_copy_state()
                                    print(f"[COPYTRADE] Đã refresh token mới cho user {username}")
                                else:
                                    print(f"[COPYTRADE] Không thể refresh token, dừng copytrade")
                                    _copy_enabled = False
                                    _copy_cfg["enabled"] = False
                                    _save_copy_state()
                                    break
                        except Exception:
                            pass
                        
                        # Đặt lệnh với số tiền cố định
                        response = place_order(user_token, symbol, side, so_tien_co_dinh, type_mode)
                        
                        if response.get("status") and response.get("data", {}).get("idChart"):
                            order_id = str(response["data"]["idChart"])
                            pending_orders[order_id] = {
                                "timestamp": time.time(),
                                "amount": so_tien_co_dinh
                            }
                            print(f"[COPYTRADE] Đặt lệnh thành công: {side.upper()} ${so_tien_co_dinh} - ID: {order_id}")
                        else:
                            print(f"[COPYTRADE] Lỗi đặt lệnh: {response}")
                            
                    except Exception as e:
                        print(f"[COPYTRADE] Exception đặt lệnh: {e}")
                        continue
        except Exception as e:
            print(f"[COPYTRADE] Worker exception: {e}")

        time.sleep(2.0)

def _start_worker_if_needed():
    global _copy_thread, _copy_stop
    if _copy_thread and _copy_thread.is_alive():
        return
    _copy_stop = threading.Event()
    _copy_thread = threading.Thread(target=_copy_worker, daemon=True)
    _copy_thread.start()

# khởi động
_load_copy_state()
if _worker_should_start():
    _start_worker_if_needed()

# ----------------- tính state UI chính -----------------
def compute_state() -> Dict[str, Any]:
    token = pick_token()
    if not token:
        return {"need_login": True}

    js = get_chart(token, SYMBOL, 60, 1)
    if not js.get("status"):
        raise RuntimeError("getChart failed")

    data = js["data"]
    last = data[-1]
    step = kline_step(data)
    last_ts = int(last["timestamp"])
    
    # Logic đúng: order = 0 thì cho đặt cược (kết quả phiên trước), order = 1 thì chờ kết quả
    is_entry = int(last.get("order", 0)) == 0
    
    # window_id dùng timestamp hoặc id để đồng bộ ratio
    window_id = f"{SYMBOL}:{last.get('id', last_ts)}"

    prev_close = float(data[-2]["close"]) if len(data) >= 2 else float(last["open"])
    last_close = float(last["close"])
    suggest = "buy" if last_close >= prev_close else "sell"

    now = int(time.time())
    age = max(0, now - last_ts)
    sec_left = max(0, step - (age % step))

    phase = "Đặt lệnh" if is_entry else "Chờ kết quả"
    ratio_val = get_ratio_for(window_id) if is_entry else None

    return {
        "need_login": False,
        "symbol": SYMBOL,
        "step": step,
        "candle_id": last.get("id"),
        "timestamp": last_ts,
        "window_id": window_id,
        "is_entry_window": is_entry,
        "phase": phase,
        "seconds_left": sec_left,
        "suggest": suggest,
        "ratio": ratio_val,
        "order_allowed": is_entry,
        "order_status": int(last.get("order", 0)),  # Thêm để frontend hiển thị
    }

# ========= LEADERBOARD SYSTEM =========
import hashlib
from datetime import datetime, timedelta

# Vietnamese usernames database - realistic forum/game style
VIETNAMESE_USERNAMES = [
    'mdung2024', 'lelong828', 'okbqn827', 'hoangminh88', 'vuthanh99',
    'trandat007', 'phantuan86', 'buimai1995', 'levietnam', 'nguoidep123',
    'bitcoin_hunter', 'trader_pro', 'vinhlong2k', 'saigon_boy', 'hanoi_girl',
    'lamviec24h', 'kiemtien_online', 'investo_r', 'crypto_king', 'money_maker',
    'vuacanh2023', 'thanhcong88', 'quatang_vip', 'lucky_trade', 'vietgold',
    'sacombank99', 'techcombank', 'bidvbank88', 'vcbpro', 'tpbank_vip',
    'forex_master', 'binary_god', 'option_pro', 'trade_winner', 'profit_king',
    'nguyenvan2k', 'phanthi1999', 'lethuy88', 'buiminh007', 'vuongduc',
    'hochiminh_city', 'cantho_trader', 'danang_boy', 'haiphong99', 'vinh_city',
    'mientay_farmer', 'mienbac_cold', 'mientrung_storm', 'phuquoc_sun', 'sapa_snow',
    'coffee_lover', 'pho_addict', 'banh_mi_pro', 'che_suong', 'nom_bo_kho',
    'xe_om_pro', 'grab_driver', 'motorbike99', 'honda_wave', 'yamaha_winner',
    'student_hust', 'bkdn_alumni', 'vnuhcm_grad', 'neu_student', 'ftu_trading',
    'zalo_user99', 'facebook_vn', 'tiktok_star', 'youtube_vn', 'shopee_buyer',
    'lazada_seller', 'tiki_fan', 'sendo_user', 'grab_eater', 'baemin_lover',
    'arsenal_fan_vn', 'manu_supporter', 'barca_vietnam', 'real_madrid_vn', 'liverpool_sai_gon',
    'dota2_pro_vn', 'lol_vietnam', 'pubg_mobile_vn', 'fifa_online4', 'ao_dai_dep',
    'xoi_che_ngon', 'com_tam_saigon', 'bun_bo_hue', 'cao_lau_hoian', 'mi_quang_dn'
]

LEADERBOARD_FILE = os.path.join(os.path.dirname(__file__), "leaderboard_data.json")
RESET_INTERVAL_HOURS = 12

def _get_day_seed():
    """Generate consistent seed based on current day for reproducible randomness"""
    today = datetime.now().date()
    return int(hashlib.md5(str(today).encode()).hexdigest()[:8], 16)

def _generate_server_leaderboard():
    """Generate server-side leaderboard with day-based seed for consistency"""
    import random as rand
    
    # Use day-based seed for reproducible results
    day_seed = _get_day_seed()
    rand.seed(day_seed)
    
    # Shuffle usernames with daily seed
    shuffled_names = VIETNAMESE_USERNAMES.copy()
    rand.shuffle(shuffled_names)
    selected_names = shuffled_names[:20]
    
    data = []
    for i, name in enumerate(selected_names):
        # Generate consistent win rate and profit for each name+day
        name_seed = int(hashlib.md5(f"{name}_{day_seed}".encode()).hexdigest()[:8], 16)
        rand.seed(name_seed)
        
        win_rate = rand.uniform(60, 85)  # 60-85%
        profit = rand.randint(100, 3500)  # $100-3500
        
        data.append({
            'rank': i + 1,
            'name': name,
            'avatar': "https://img.favpng.com/17/24/10/computer-icons-user-profile-male-avatar-png-favpng-jhVtWQQbMdbcNCahLZztCF5wk.jpg",
            'winRate': win_rate,
            'profit': profit,
            'score': f"{win_rate:.2f}",
            'profitText': f"+${profit}"
        })
    
    # Sort by win rate (highest first)
    data.sort(key=lambda x: x['winRate'], reverse=True)
    
    # Update ranks after sorting
    for i, item in enumerate(data):
        item['rank'] = i + 1
    
    return {
        'data': data[:10],  # Top 10
        'timestamp': time.time(),
        'nextReset': time.time() + (RESET_INTERVAL_HOURS * 3600),
        'seed': day_seed
    }

def _load_server_leaderboard():
    """Load leaderboard from server file or generate new one"""
    try:
        if os.path.exists(LEADERBOARD_FILE):
            with open(LEADERBOARD_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            current_time = time.time()
            current_day_seed = _get_day_seed()
            
            # Check if need reset (time-based or day change)
            if (current_time >= data.get('nextReset', 0) or 
                data.get('seed') != current_day_seed):
                return _generate_and_save_leaderboard()
            
            return data
    except Exception as e:
        print(f"Error loading leaderboard: {e}")
    
    return _generate_and_save_leaderboard()

def _generate_and_save_leaderboard():
    """Generate new leaderboard and save to file"""
    new_data = _generate_server_leaderboard()
    try:
        with open(LEADERBOARD_FILE, 'w', encoding='utf-8') as f:
            json.dump(new_data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Error saving leaderboard: {e}")
    return new_data

# ===================== LEADERBOARD API =====================
@app.route("/api/leaderboard")
def api_leaderboard():
    """Get current leaderboard - synchronized across all devices"""
    try:
        leaderboard_data = _load_server_leaderboard()
        return jsonify({
            "ok": True,
            "data": leaderboard_data['data'],
            "nextReset": leaderboard_data['nextReset'],
            "timestamp": leaderboard_data['timestamp']
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/leaderboard/force-reset", methods=["POST"])
def api_leaderboard_force_reset():
    """Force reset leaderboard (for testing)"""
    try:
        new_data = _generate_and_save_leaderboard()
        return jsonify({
            "ok": True,
            "message": "Leaderboard reset successfully",
            "data": new_data['data']
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ===================== PAGES =====================
@app.route("/login")
def page_login():
    token = request.args.get("token")
    if token:
        # Chuẩn hóa token
        clean_token = _normalize_token(token)
        if clean_token:
            # Lưu vào session
            session["token"] = clean_token
            # Chuyển hướng với token trong query string
            return redirect(f"/?token={clean_token}")
    return render_template("login.html")

@app.route("/")
def page_dashboard():
    # Ưu tiên lấy token từ session
    token = session.get("token")
    if not token:
        return redirect("/login")
    return render_template("bangdieukhien.html", token=token)

@app.route("/aidudoan")
def page_aidudoan():
    # Ưu tiên lấy token từ session
    token = session.get("token")
    if not token:
        return redirect("/login")
    return render_template("index.html", token=token)

@app.route("/copytrade")
def page_copytrade():
    return render_template("copytrade.html")

@app.route("/top-expert")
def page_top_expert():
    return render_template("top_expert.html")

# ===================== APIs =====================
@app.route("/api/login", methods=["POST"])
def api_login():
    try:
        body = request.get_json(force=True)
        email = (body.get("email") or "").strip()
        password = (body.get("password") or "").strip()
        twofa = (body.get("twofa") or "").strip() or None

        if not email:
            return jsonify({"ok": False, "error": "Vui lòng nhập email."}), 400
        if not password:
            return jsonify({"ok": False, "error": "Vui lòng nhập mật khẩu."}), 400

        js = login(email, password, twofa)
        if not js.get("status"):
            msg = js.get("message") or "Sai email hoặc mật khẩu."
            return jsonify({"ok": False, "error": msg}), 400

        data = js.get("data") or {}
        token = data.get("token")
        if token:
            session["token"] = token  # <-- Đảm bảo dòng này luôn chạy khi có token
            return jsonify({"ok": True, "token": token, "data": data})
        else:
            return jsonify({"ok": True, "token": None, "need_twofa": True, "data": data})
    except HTTPError as he:
        status = he.response.status_code if he.response else 500
        text = he.response.text if he.response else str(he)
        return jsonify({"ok": False, "error": f"Sai email hoặc mật khẩu. (HTTP {status}) {text}"}), status
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.pop("token", None)
    return jsonify({"ok": True})

@app.route("/api/state")
@limit_concurrent 
def api_state():
    try:
        token = validate_auth()
        if not token:
            return jsonify({"ok": False, "error": "Authentication required"}), 401
        return jsonify({"ok": True, "state": compute_state()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/order", methods=["POST"])
def api_order():
    try:
        token = validate_auth()
        if not token:
            session.clear()
            return jsonify({"status": False, "message": "Bạn chưa đăng nhập."}), 401

        b = request.get_json(force=True)
        side = (b.get("side") or "").lower()
        amount = float(str(b.get("amount", "0")).replace(",", "."))
        type_mode = b.get("type_mode", TYPE_MODE)

        rsp = place_order(token, SYMBOL, side, amount, type_mode)
        
        # Return the same format as backend API
        if rsp.get("status") == True:
            return jsonify(rsp)  # Forward exact response from backend
        else:
            return jsonify({
                "status": False,
                "message": rsp.get("message", "Đặt lệnh thất bại.")
            })

    except HTTPError as he:
        status = he.response.status_code if he.response else 500
        if status == 401:
            session.clear()
            return jsonify({"status": False, "message": "Phiên đăng nhập đã hết hạn."}), 401
        
        try:
            error_detail = he.response.json()
            message = error_detail.get("message", "Đặt lệnh thất bại.")
        except:
            message = "Đặt lệnh thất bại."
            
        return jsonify({"status": False, "message": message}), status
    except Exception as e:
        return jsonify({"status": False, "message": "Đặt lệnh thất bại.", "detail": str(e)}), 500

@app.route("/api/history")
@limit_concurrent
def api_history():
    try:
        token = pick_token()
        if not token:
            return jsonify({"ok": True, "rows": []})
        tmode = request.args.get("type_mode", TYPE_MODE)
        js = day_history(token, tmode, limit=int(request.args.get("limit", 10)), page=1)
        arr = js["data"]["array"]

        def pnl(r):
            if int(r.get("draw") or 0) == 1:
                return 0.0
            side = r["side"].lower()
            entry = float(r["entryPrice"])
            close = float(r["closingPrice"])
            amt = float(r["amount"])
            ratio = float(r.get("configProfit", 0))
            win = (close > entry) if side == "buy" else (close < entry)
            return round(amt * ratio if win else -amt, 2)

        rows = [{
            "time": r["created_at"],
            "side": r["side"],
            "amount": float(r["amount"]),
            "symbol": r["symbol"],
            "status": r["status"],
            "idChart": r["idChart"],
            "entry": float(r["entryPrice"]),
            "close": float(r["closingPrice"]),
            "ratio": float(r.get("configProfit", 0)),
            "draw": int(r.get("draw") or 0),
            "pnl": pnl(r),
        } for r in arr]
        return jsonify({"ok": True, "rows": rows})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/pending")
@limit_concurrent
def api_pending():
    try:
        token = pick_token()
        if not token:
            return jsonify({"ok": True, "rows": []})
        tmode = request.args.get("type_mode", TYPE_MODE)
        js = pending_orders(token, tmode)
        arr = js.get("data") or []
        rows = [{
            "time": r.get("created_at"),
            "side": r.get("side"),
            "amount": float(r.get("amount")),
            "symbol": r.get("symbol"),
            "status": r.get("status"),
            "idChart": r.get("idChart"),
        } for r in arr]
        return jsonify({"ok": True, "rows": rows})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ---- ratio API (đồng bộ giữa thiết bị & reload) ----
@app.route("/api/ratio")
def api_ratio():
    window_id = request.args.get("window_id") or request.args.get("run_idx")
    if not window_id:
        return jsonify({"ok": False, "error": "missing window_id"}), 400
    if ":" not in window_id:  # cho phép chỉ gửi run_idx
        window_id = f"{SYMBOL}:{window_id}"
    return jsonify({"ok": True, "ratio": get_ratio_for(window_id)})

# ===================== COPYTRADE APIs =====================
@app.route("/api/copytrade/start", methods=["POST"])
def api_copytrade_start():
    """Nhận config từ client, lưu và bật worker (server chạy cả khi user offline)."""
    global _copy_enabled, _copy_cfg
    try:
        user_token = pick_token()
        if not user_token:
            return jsonify({"ok": False, "user_msg": "Bạn chưa đăng nhập."}), 401

        # Lấy thông tin user để lưu username
        user_info = _get_user_info(user_token)
        if not user_info:
            return jsonify({"ok": False, "user_msg": "Không thể lấy thông tin tài khoản."}), 400

        b = request.get_json(force=True) or {}
        so_tien_co_dinh = float(str(b.get("so_tien_co_dinh") or "0").replace(",", "."))
        so_von_ban_dau = float(str(b.get("so_von") or "0").replace(",", "."))
        so_tien_tai_khoan = float(str(b.get("so_tien_tai_khoan") or "0").replace(",", "."))
        
        cfg = {
            "type": (b.get("type") or TYPE_MODE),
            "expert": (b.get("expert") or "").strip(),
            "so_tien_co_dinh": so_tien_co_dinh,
            "so_von": so_von_ban_dau,  # Vốn hiện tại
            "so_von_ban_dau": so_von_ban_dau,  # Vốn ban đầu để tham chiếu
            "so_tien_tai_khoan": so_tien_tai_khoan,  # Số tiền tài khoản hiện tại
            "so_tien_ban_dau": so_tien_tai_khoan,  # Số tiền ban đầu để tính P&L
            "tp": float(str(b.get("tp") or "0").replace(",", ".")),
            "sl": float(str(b.get("sl") or "0").replace(",", ".")),
            "enabled": bool(b.get("enabled")),
            "user_token": user_token,
            "username": user_info.get("username", ""),
        }

        # Validation
        if not cfg["expert"]:
            return jsonify({"ok": False, "user_msg": "Vui lòng nhập tên chuyên gia."}), 400
        if so_tien_co_dinh <= 0:
            return jsonify({"ok": False, "user_msg": "Số tiền cố định phải > 0."}), 400
        if so_von_ban_dau <= 0:
            return jsonify({"ok": False, "user_msg": "Vốn copytrade phải > 0."}), 400
        if so_tien_co_dinh > so_von_ban_dau:
            return jsonify({"ok": False, "user_msg": "Số tiền cố định không được lớn hơn vốn copytrade."}), 400
        if so_von_ban_dau > so_tien_tai_khoan:
            return jsonify({"ok": False, "user_msg": "Vốn copytrade không được lớn hơn số dư tài khoản."}), 400
        if not ADMIN_TOKEN or ADMIN_TOKEN.startswith("<PUT_"):
            return jsonify({"ok": False, "user_msg": "Chưa cấu hình ADMIN_TOKEN ở server."}), 500

        _copy_cfg = cfg
        _copy_enabled = bool(cfg["enabled"])
        _save_copy_state()
        if _worker_should_start():
            _start_worker_if_needed()
        return jsonify({"ok": True, "enabled": _copy_enabled})
    except Exception as e:
        return jsonify({"ok": False, "user_msg": "Cập nhật thất bại.", "error": str(e)}), 500

@app.route("/api/copytrade/stop", methods=["POST"])
def api_copytrade_stop():
    global _copy_enabled
    _copy_enabled = False
    _save_copy_state()
    return jsonify({"ok": True, "enabled": _copy_enabled})

@app.route("/api/copytrade/status")
def api_copytrade_status():
    try:
        pub_cfg = {k: v for k, v in _copy_cfg.items() if k != "user_token"}
        return jsonify({"ok": True, "enabled": _copy_enabled, "cfg": pub_cfg, "processed": len(_processed_ids)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/user/getProfile", methods=['POST']) 
def api_get_profile():
    token = validate_auth()
    if not token:
        session.clear()
        return jsonify({"status": False, "message": "Unauthorized"}), 401
        
    try:
        r = requests.post(
            f"{BASE_URL}/api/user/getProfile",
            headers=_headers(token),
            timeout=10
        )
        
        # If backend returns 401, clear session
        if r.status_code == 401:
            session.clear()
            return jsonify({"status": False, "message": "Token expired"}), 401
            
        response_data = r.json()
        
        # Check if response indicates auth failure
        if not response_data.get("status", False):
            session.clear()
            return jsonify({"status": False, "message": "Authentication failed"}), 401
            
        return response_data
        
    except requests.exceptions.Timeout:
        return jsonify({"status": False, "message": "Request timeout"}), 504
    except requests.exceptions.RequestException as e:
        return jsonify({"status": False, "message": f"Request failed: {str(e)}"}), 500
    except Exception as e:
        return jsonify({"status": False, "message": str(e)}), 500

@app.route("/api/dashboard/statistics", methods=["POST"])
def api_dashboard_statistics():
    try:
        token = validate_auth()
        if not token:
            session.clear()
            return jsonify({"ok": False, "user_msg": "Bạn chưa đăng nhập."}), 401

        b = request.get_json(force=True) or {}
        type_mode = b.get("type", "live")
        userid = b.get("userid")
        timeStart = b.get("timeStart")
        timeEnd = b.get("timeEnd")

        # Call the API
        r = requests.post(
            f"{BASE_URL}/api/binaryOption/dayStatisticsOrderToTime",
            json={
                "type": type_mode,
                "userid": userid,
                "timeStart": timeStart,
                "timeEnd": timeEnd
            },
            headers=_headers(token),
            timeout=15
        )
        
        # Handle auth failure
        if r.status_code == 401:
            session.clear()
            return jsonify({"ok": False, "user_msg": "Phiên đăng nhập đã hết hạn."}), 401
            
        r.raise_for_status()
        return r.json()
        
    except HTTPError as he:
        status = he.response.status_code if he.response else 500
        if status == 401:
            session.clear()
            return jsonify({"ok": False, "user_msg": "Phiên đăng nhập đã hết hạn."}), 401
        detail = he.response.text if he.response else str(he)
        return jsonify({
            "ok": False,
            "user_msg": "Không thể tải thống kê.",
            "detail": f"HTTP {status}: {detail}"
        }), status
    except Exception as e:
        return jsonify({"ok": False, "user_msg": "Không thể tải thống kê.", "detail": str(e)}), 500

@app.route("/test-api")
def test_api():
    """Trang test các API"""
    return render_template("test_api.html")

# ---- API lịch sử giao dịch (format mới) ----
@app.route("/api/dayHistoryOrder", methods=["POST"])
@limit_concurrent
def api_day_history_order():
    """API lịch sử giao dịch theo format giống backend"""
    try:
        token = pick_token()
        if not token:
            return jsonify({
                "message": "get dayHistoryOrder", 
                "data": {"array": [], "total": 0}, 
                "status": True
            })
        
        data = request.get_json() or {}
        type_mode = data.get("type", TYPE_MODE)
        limit = int(data.get("limit", 10))
        page = int(data.get("page", 1))
        
        js = day_history(token, type_mode, limit=limit, page=page)
        
        # Trả về format giống như backend
        return jsonify({
            "message": "get dayHistoryOrder",
            "data": {
                "array": js.get("data", {}).get("array", []),
                "total": js.get("data", {}).get("total", 0)
            },
            "status": True
        })
        
    except Exception as e:
        return jsonify({
            "message": "get dayHistoryOrder failed",
            "data": {"array": [], "total": 0},
            "status": False,
            "error": str(e)
        }), 500

@app.route("/api/getAllOrderPendingUser", methods=["POST"])
@limit_concurrent
def api_get_all_order_pending_user():
    """API lấy tất cả lệnh pending theo format giống backend"""
    try:
        token = pick_token()
        if not token:
            return jsonify({
                "message": "get order pendding success",
                "data": [],
                "status": True
            })
        
        data = request.get_json() or {}
        type_mode = data.get("type", TYPE_MODE)
        
        js = pending_orders(token, type_mode)
        
        # Trả về format giống như backend
        return jsonify({
            "message": "get order pendding success",
            "data": js.get("data", []),
            "status": True
        })
        
    except Exception as e:
        return jsonify({
            "message": "get order pendding failed", 
            "data": [],
            "status": False,
            "error": str(e)
        }), 500

# ===================== run =====================
if __name__ == "__main__":
    # Port 8000 để chạy không cần quyền admin
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "8000")), debug=True)
