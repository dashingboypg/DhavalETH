#!/usr/bin/env python3
import os
import json
import time
import hmac
import hashlib
import fcntl
import uuid
import requests
import boto3

from pathlib import Path
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP
from urllib.parse import urlencode

# ============================================================
# DHAVALETH - ETHUSD SELL LADDER
# ============================================================

BOT_NAME = "DhavalETH"
SECRET_NAME = "DhavalETH"
AWS_REGION = "ap-south-1"

BASE_URL = "https://api.india.delta.exchange"
SYMBOL = "ETHUSD"
PRODUCT_ID = 3136

PREFIX = "DHE-"

BASE_DIR = Path("/home/ec2-user/delta-bot/DhavalETH")
STATE_FILE = BASE_DIR / "DhavalETH_state.json"
LOCK_FILE = BASE_DIR / "DhavalETH.lock"

POLL_SECONDS = 3
RETRY_SECONDS = 15
MAX_EXCHANGE_ORDERS = 100
PAGE_SIZE = 100


# ============================================================
# LOGGING
# ============================================================

def log(message):
    print(
        time.strftime("%Y-%m-%d %H:%M:%S"),
        "|",
        message,
        flush=True
    )


# ============================================================
# AWS SECRETS MANAGER
# ============================================================

def load_config():

    client = boto3.client(
        "secretsmanager",
        region_name=AWS_REGION
    )

    response = client.get_secret_value(
        SecretId=SECRET_NAME
    )

    data = json.loads(response["SecretString"])

    required = [
        "API_Key",
        "API_Secret",
        "Max_Position",
        "Order_Size",
        "Step",
        "Counter_Step"
    ]

    for key in required:
        if key not in data:
            raise RuntimeError(
                f"Missing secret field: {key}"
            )

    def number(key):

        value = str(data[key]).strip()

        value = (
            value.replace("$", "")
            .replace(",", "")
            .replace("lots", "")
            .replace("lot", "")
            .strip()
        )

        result = Decimal(value)

        if not result.is_finite() or result <= 0:
            raise RuntimeError(
                f"Invalid secret value: {key}"
            )

        return result

    api_key = str(data["API_Key"]).strip()
    api_secret = str(data["API_Secret"]).strip()

    maximum = number("Max_Position")
    order_size = number("Order_Size")
    step = number("Step")
    counter_step = number("Counter_Step")

    if not api_key or not api_secret:
        raise RuntimeError("Empty API credentials")

    if maximum != maximum.to_integral_value():
        raise RuntimeError("Max_Position must be whole lots")

    if order_size != order_size.to_integral_value():
        raise RuntimeError("Order_Size must be whole lots")

    if maximum < order_size:
        raise RuntimeError("Max_Position is too small")

    if maximum % order_size != 0:
        raise RuntimeError(
            "Max_Position must be divisible by Order_Size"
        )

    return (
        api_key,
        api_secret,
        int(maximum),
        int(order_size),
        step,
        counter_step
    )


API_KEY, API_SECRET, MAX_POSITION, ORDER_SIZE, STEP, COUNTER_STEP = load_config()


# ============================================================
# PRICE ROUNDING
# Nearest whole-dollar price ending in 7
# Examples: 1707, 1717, 1727, 1737
# ============================================================

def round_price(value):

    value = Decimal(str(value))

    lower = (
        (value - Decimal(7)) / Decimal(10)
    ).to_integral_value(rounding=ROUND_FLOOR)

    lower_price = lower * Decimal(10) + Decimal(7)
    upper_price = lower_price + Decimal(10)

    if abs(value - lower_price) <= abs(value - upper_price):
        return lower_price

    return upper_price


def price_text(value):
    return format(
        Decimal(str(value)).normalize(),
        "f"
    )


def new_client_id():
    return PREFIX + uuid.uuid4().hex[:24]


def counter_client_id(fill_id):
    digest = hashlib.sha256(
        str(fill_id).encode()
    ).hexdigest()[:24]

    return PREFIX + "F" + digest


# ============================================================
# DELTA EXCHANGE API
# ============================================================

class DeltaAPI:

    def __init__(self):

        self.session = requests.Session()

        self.session.headers.update({
            "User-Agent": "python-3.11 DhavalETH",
            "Accept": "application/json",
            "Content-Type": "application/json"
        })

    def request(
        self,
        method,
        path,
        params=None,
        body=None,
        authenticated=True
    ):

        params = params or {}

        query = urlencode(params)

        payload = ""

        if body is not None:
            payload = json.dumps(
                body,
                separators=(",", ":"),
                ensure_ascii=False
            )

        headers = {}

        if authenticated:

            timestamp = str(int(time.time()))

            message = (
                method.upper()
                + timestamp
                + path
                + ("?" + query if query else "")
                + payload
            )

            signature = hmac.new(
                API_SECRET.encode(),
                message.encode(),
                hashlib.sha256
            ).hexdigest()

            headers.update({
                "api-key": API_KEY,
                "timestamp": timestamp,
                "signature": signature
            })

        response = self.session.request(
            method.upper(),
            BASE_URL + path,
            params=params if params else None,
            data=payload if body is not None else None,
            headers=headers,
            timeout=(5, 25)
        )

        try:
            data = response.json()
        except Exception:
            raise RuntimeError(
                f"Non-JSON response HTTP {response.status_code}"
            )

        if not response.ok or data.get("success") is False:
            raise RuntimeError(
                f"Delta HTTP {response.status_code}: "
                f"{json.dumps(data)[:600]}"
            )

        return data

    def ticker(self):

        data = self.request(
            "GET",
            f"/v2/tickers/{SYMBOL}",
            authenticated=False
        )

        result = data["result"]

        price = (
            result.get("close")
            or result.get("mark_price")
        )

        if not price:
            raise RuntimeError("ETHUSD price unavailable")

        return Decimal(str(price))

    def active_orders(self):

        orders = []
        cursor = None

        while True:

            params = {
                "product_ids": str(PRODUCT_ID),
                "states": "open,pending",
                "page_size": PAGE_SIZE
            }

            if cursor:
                params["after"] = cursor

            data = self.request(
                "GET",
                "/v2/orders",
                params=params
            )

            orders.extend(data.get("result", []))

            cursor = data.get("meta", {}).get("after")

            if not cursor:
                break

        return orders

    def order_by_id(self, order_id):

        data = self.request(
            "GET",
            f"/v2/orders/{order_id}"
        )

        return data["result"]

    def order_by_client_id(self, client_id):

        data = self.request(
            "GET",
            f"/v2/orders/client_order_id/{client_id}"
        )

        return data["result"]

    def create_order(self, intent):

        body = {
            "product_id": PRODUCT_ID,
            "size": int(intent["size"]),
            "side": intent["side"],
            "order_type": "limit_order",
            "limit_price": price_text(intent["price"]),
            "time_in_force": "gtc",
            "post_only": False,
            "reduce_only": intent["side"] == "sell",
            "client_order_id": intent["client_id"]
        }

        data = self.request(
            "POST",
            "/v2/orders",
            body=body
        )

        return data["result"]

    def fills(self, start_time):

        fills = []
        cursor = None

        while True:

            params = {
                "product_id": PRODUCT_ID,
                "start_time": int(start_time),
                "page_size": PAGE_SIZE
            }

            if cursor:
                params["after"] = cursor

            data = self.request(
                "GET",
                "/v2/fills",
                params=params
            )

            fills.extend(data.get("result", []))

            cursor = data.get("meta", {}).get("after")

            if not cursor:
                break

        return fills


API = DeltaAPI()


# ============================================================
# STATE MANAGEMENT
# ============================================================

def default_state():

    return {
        "version": 3,
        "initialized": False,
        "anchor": None,
        "net_sold": 0,
        "orders": {},
        "pending": {},
        "processed_fills": [],
        "last_fill_scan": int(time.time() * 1000000),
        "created_at": int(time.time())
    }


def load_state():

    if not STATE_FILE.exists():
        return default_state()

    with STATE_FILE.open() as file:
        state = json.load(file)

    if state.get("version") != 3:
        raise RuntimeError(
            "Incompatible state file. "
            "Existing state will not be overwritten."
        )

    return state


STATE = None


def save_state():

    temporary = STATE_FILE.with_suffix(".tmp")

    with temporary.open("w") as file:

        json.dump(
            STATE,
            file,
            indent=2
        )

        file.flush()
        os.fsync(file.fileno())

    os.replace(
        temporary,
        STATE_FILE
    )


# ============================================================
# ORDER QUEUE
# ============================================================

def queue_order(
    side,
    price,
    size,
    role,
    client_id=None
):

    price = round_price(price)

    if price <= 0 or size <= 0:
        raise RuntimeError("Invalid order intent")

    if client_id is None:
        client_id = new_client_id()

    if client_id in STATE["pending"]:
        return

    for order in STATE["orders"].values():

        if order["client_id"] == client_id:
            return

    STATE["pending"][client_id] = {
        "client_id": client_id,
        "side": side,
        "price": str(price),
        "size": int(size),
        "role": role,
        "last_attempt": 0,
        "created_at": int(time.time())
    }

    save_state()


def attach_order(order, intent):

    order_id = str(order["id"])

    STATE["orders"][order_id] = {
        "client_id": intent["client_id"],
        "side": intent["side"],
        "price": str(intent["price"]),
        "size": int(intent["size"]),
        "role": intent["role"],
        "processed_terminal": False
    }

    STATE["pending"].pop(
        intent["client_id"],
        None
    )

    save_state()

    log(
        f"ORDER CONFIRMED | "
        f"{intent['side'].upper()} "
        f"{intent['size']} lots "
        f"@ ${price_text(intent['price'])} "
        f"| ID={order_id}"
    )


# ============================================================
# INITIAL SELL LADDER
# ============================================================

def initialize():

    if STATE["initialized"]:
        return

    cmp = API.ticker()

    anchor = round_price(cmp)

    levels = MAX_POSITION // ORDER_SIZE

    STATE["anchor"] = str(anchor)

    for level in range(1, levels + 1):

        price = round_price(
            anchor + STEP * level
        )

        queue_order(
            "sell",
            price,
            ORDER_SIZE,
            "INITIAL"
        )

    STATE["initialized"] = True

    save_state()

    log(
        f"INITIAL LADDER | CMP=${price_text(cmp)} "
        f"| Anchor=${price_text(anchor)}"
    )

    log(
        f"Levels={levels} | Order size={ORDER_SIZE}"
    )

    log(
        f"Step=${price_text(STEP)} "
        f"| Counter=${price_text(COUNTER_STEP)}"
    )


# ============================================================
# FILL PROCESSING
# ============================================================

def process_fills():

    start = max(
        0,
        int(STATE["last_fill_scan"]) - 600000000
    )

    fills = API.fills(start)

    processed = set(
        STATE["processed_fills"]
    )

    order_lookup = {
        str(order_id): info
        for order_id, info in STATE["orders"].items()
    }

    latest_timestamp = STATE["last_fill_scan"]

    for fill in fills:

        fill_id = str(fill.get("id", ""))

        if not fill_id or fill_id in processed:
            continue

        order_id = str(fill.get("order_id", ""))

        info = order_lookup.get(order_id)

        if info is None:
            continue

        fill_size = int(fill["size"])

        fill_price = round_price(
            fill["price"]
        )

        if fill_size <= 0:
            continue

        if info["side"] == "sell":

            counter_side = "buy"

            counter_price = (
                fill_price - COUNTER_STEP
            )

            role = "COUNTER_BUY"

            STATE["net_sold"] += fill_size

        else:

            counter_side = "sell"

            counter_price = (
                fill_price + COUNTER_STEP
            )

            role = "COUNTER_SELL"

            STATE["net_sold"] -= fill_size

        queue_order(
            counter_side,
            counter_price,
            fill_size,
            role,
            counter_client_id(fill_id)
        )

        STATE["processed_fills"].append(
            fill_id
        )

        processed.add(fill_id)

        timestamp = int(
            fill.get("created_at_unix", 0)
            or fill.get("created_at", 0)
            or 0
        )

        latest_timestamp = max(
            latest_timestamp,
            timestamp
        )

        log(
            f"FILL | {info['side'].upper()} "
            f"{fill_size} lots "
            f"@ ${price_text(fill_price)} "
            f"| Counter {counter_side.upper()} "
            f"@ ${price_text(counter_price)}"
        )

        save_state()

    STATE["processed_fills"] = (
        STATE["processed_fills"][-20000:]
    )

    STATE["last_fill_scan"] = max(
        int(time.time() * 1000000),
        latest_timestamp
    )

    save_state()


# ============================================================
# TERMINAL ORDER RECOVERY
# ============================================================

def process_terminal_orders(active):

    active_ids = {
        str(order["id"])
        for order in active
    }

    for order_id, info in list(
        STATE["orders"].items()
    ):

        if info["processed_terminal"]:
            continue

        if order_id in active_ids:
            continue

        try:

            order = API.order_by_id(order_id)

        except Exception as error:

            log(
                f"ORDER STATUS RETRY "
                f"{order_id}: {error}"
            )

            continue

        status = str(
            order.get("state", "")
        ).lower()

        if status in ("open", "pending"):
            continue

        if status not in (
            "closed",
            "cancelled",
            "canceled",
            "rejected",
            "expired"
        ):
            continue

        remaining = order.get("unfilled_size")

        if remaining is None:
            log(
                f"Missing unfilled_size for {order_id}"
            )
            continue

        remaining = int(remaining)

        if remaining > 0 and status in (
            "rejected",
            "expired"
        ):

            queue_order(
                info["side"],
                info["price"],
                remaining,
                info["role"]
            )

            log(
                f"FAILED ORDER RETRIED | "
                f"{order_id} | Remaining={remaining}"
            )

        info["processed_terminal"] = True

        save_state()


# ============================================================
# SUBMIT PENDING ORDERS
# ============================================================

def submit_pending():

    if not STATE["pending"]:
        return

    active = API.active_orders()

    # Exchange limit is account/instrument-wide.
    # Manual orders count toward capacity but are never modified.
    if len(active) >= MAX_EXCHANGE_ORDERS:

        log(
            f"EXCHANGE ORDER LIMIT | "
            f"{len(active)} active orders"
        )

        return

    bot_active = [
        order for order in active
        if str(
            order.get("client_order_id", "")
        ).startswith(PREFIX)
    ]

    bot_sell_open = sum(
        int(order.get("unfilled_size", 0))
        for order in bot_active
        if order.get("side") == "sell"
    )

    now = int(time.time())

    for client_id, intent in list(
        STATE["pending"].items()
    ):

        if len(active) >= MAX_EXCHANGE_ORDERS:
            break

        if (
            now - int(intent.get("last_attempt", 0))
            < RETRY_SECONDS
        ):
            continue

        if intent["side"] == "sell":

            projected = (
                int(STATE["net_sold"])
                + bot_sell_open
                + int(intent["size"])
            )

            if projected > MAX_POSITION:
                continue

        try:

            # Recover an order accepted before a lost response.
            try:

                existing = API.order_by_client_id(
                    client_id
                )

                if existing:

                    attach_order(
                        existing,
                        intent
                    )

                    active.append(existing)

                    if intent["side"] == "sell":

                        bot_sell_open += int(
                            existing.get("unfilled_size", 0)
                        )

                    continue

            except Exception as lookup_error:

                message = str(
                    lookup_error
                ).lower()

                if (
                    "404" not in message
                    and "not_found" not in message
                    and "not found" not in message
                ):
                    raise

            intent["last_attempt"] = now

            save_state()

            order = API.create_order(intent)

            if not order.get("id"):
                raise RuntimeError(
                    "Exchange did not return an order ID"
                )

            attach_order(
                order,
                intent
            )

            active.append(order)

            if intent["side"] == "sell":
                bot_sell_open += int(intent["size"])

        except Exception as error:

            log(
                f"ORDER SUBMISSION RETRY | "
                f"{client_id} | {error}"
            )


# ============================================================
# MAIN
# ============================================================

def main():

    global STATE

    BASE_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    lock = LOCK_FILE.open("w")

    try:

        fcntl.flock(
            lock,
            fcntl.LOCK_EX | fcntl.LOCK_NB
        )

    except BlockingIOError:

        raise RuntimeError(
            "DhavalETH is already running"
        )

    STATE = load_state()

    log("====================================")
    log("DHAVALETH STARTING")
    log("====================================")

    log(f"Secret: {SECRET_NAME}")
    log(f"Instrument: {SYMBOL}")
    log(f"Product ID: {PRODUCT_ID}")

    log(f"Maximum position: {MAX_POSITION}")
    log(f"Order size: {ORDER_SIZE}")

    log(f"Step: ${price_text(STEP)}")
    log(f"Counter step: ${price_text(COUNTER_STEP)}")

    log("Order prices round to nearest number ending in 7.")
    log("Manual orders are not modified.")
    log("SELL orders are reduce-only.")

    initialize()

    while True:

        try:

            process_fills()

            active = API.active_orders()

            process_terminal_orders(active)

            submit_pending()

            time.sleep(POLL_SECONDS)

        except KeyboardInterrupt:

            log("DHAVALETH STOPPED")

            break

        except Exception as error:

            log(
                f"MAIN LOOP ERROR | "
                f"{type(error).__name__}: {error}"
            )

            time.sleep(8)


if __name__ == "__main__":
    main()

