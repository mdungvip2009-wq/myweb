# -*- coding: utf-8 -*-
import requests

BASE_URL = "https://fintech3.net"

def _normalize_token(token: str | None) -> str | None:
    if not token: return None
    t = token.strip()
    if t.lower().startswith("bearer "): t = t[7:].strip()
    return t

def _headers(token: str | None):
    tok = _normalize_token(token)
    h = {"Accept": "application/json, text/plain, */*",
         "Content-Type": "application/json"}
    # ⇩⇩ luôn gửi “Bearer <token>”
    if tok: h["Authorization"] = f"Bearer {tok}"
    else:   h["Authorization"] = "Bearer null"
    return h

def login(email: str, password: str, twofa: str | None = None):
    payload = {"email": email, "password": password}
    if twofa:
        payload["twofa"] = twofa  # nếu backend dùng key khác, đổi tại đây
    r = requests.post(f"{BASE_URL}/api/user/login",
                      json=payload, headers=_headers(None), timeout=15)
    r.raise_for_status()
    return r.json()

def get_chart(token: str, symbol: str, limit: int = 60, page: int = 1):
    r = requests.post(f"{BASE_URL}/api/binaryOption/getChart",
                      json={"symbol": symbol, "limit": limit, "page": page},
                      headers=_headers(token), timeout=10)
    r.raise_for_status()
    return r.json()

def place_order(token: str, symbol: str, side: str, amount: float, type_mode: str):
    payload = {"symbol": symbol, "type": type_mode, "side": side,
               "amount": amount, "api": "order"}
    r = requests.post(f"{BASE_URL}/api/binaryOption/order",
                      json=payload, headers=_headers(token), timeout=10)
    r.raise_for_status()
    return r.json()

def day_history(token: str, type_mode: str, limit: int = 10, page: int = 1):
    r = requests.post(f"{BASE_URL}/api/binaryOption/dayHistoryOrder",
                      json={"type": type_mode, "limit": limit, "page": page},
                      headers=_headers(token), timeout=10)
    r.raise_for_status()
    return r.json()

def pending_orders(token: str, type_mode: str):
    r = requests.post(f"{BASE_URL}/api/binaryOption/getAllOrderPendingUser",
                      json={"type": type_mode},
                      headers=_headers(token), timeout=10)
    r.raise_for_status()
    return r.json()
