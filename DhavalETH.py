import hashlib
import os
import json
import time
import uuid
import boto3
from decimal import Decimal, ROUND_HALF_UP
from delta_rest_client import DeltaRestClient

# ============================================================
# DHAVALETH - ETHUSD INVENTORY-BACKED SELL LADDER
# ============================================================

BOT_NAME = "DhavalETH"
SECRET_NAME = "DhavalETH"
AWS_REGION = "ap-south-1"

BASE_URL = "https://api.india.delta.exchange"
SYMBOL = "ETHUSD"
PRODUCT_ID = 3136

BOT_PREFIX = "DHE-"
BASE_DIR = "/home/ec2-user/delta-bot/DhavalETH"
STATE_FILE = os.path.join(BASE_DIR, "DhavalETH_state.json")

POLL_SECONDS = 2
ERROR_SLEEP = 5


def log(message):
    print(
        time.strftime("%Y-%m-%d %H:%M:%S"),
        "|",
        message,
        flush=True
    )


# ============================================================
# LOAD AWS SECRET
# ============================================================

def load_config():
    session = boto3.session.Session(region_name=AWS_REGION)
    sm = session.client("secretsmanager")

    response = sm.get_secret_value(SecretId=SECRET_NAME)
    data = json.loads(response["SecretString"])

    required = [
        "API_Key",
        "API_Secret",
        "Max_Position",
        "Order_Size",
        "Step",
        "Counter_Step"
    ]

    missing = [key for key in required if key not in data]
    if missing:
        raise RuntimeError(f"Missing secret fields: {missing}")

    api_key = str(data["API_Key"]).strip()
    api_secret = str(data["API_Secret"]).strip()

    def read_number(key):
        raw = str(data[key]).strip()
        cleaned = (
            raw.replace("$", "")
               .replace(",", "")
               .replace("lots", "")
               .replace("lot", "")
               .strip()
        )
        try:
            value = Decimal(cleaned)
        except Exception:
            raise RuntimeError(f"Invalid secret value for {key}: {raw!r}")

        if not value.is_finite() or value <= 0:
            raise RuntimeError(f"Invalid secret value: {key}")

        return value

    max_position = read_number("Max_Position")
    order_size = read_number("Order_Size")
    step = read_number("Step")
    counter_step = read_number("Counter_Step")

    if max_position != max_position.to_integral_value():
        raise RuntimeError("Max_Position must be whole lots")

    if order_size != order_size.to_integral_value():
        raise RuntimeError("Order_Size must be whole lots")

    if max_position < order_size:
        raise RuntimeError("Max_Position is smaller than Order_Size")

    if not api_key or not api_secret:
        raise RuntimeError("AWS API credentials are empty")

    return (
        api_key,
        api_secret,
        int(max_position),
        int(order_size),
        step,
        counter_step
    )


API_KEY, API_SECRET, MAX_POSITION, ORDER_SIZE, STEP, COUNTER_STEP = load_config()

client = DeltaRestClient(
    base_url=BASE_URL,
    api_key=API_KEY,
    api_secret=API_SECRET,
    raise_for_status=True
)


# ============================================================
# STATE MANAGEMENT
# ============================================================

def default_state():
    return {
        "version": 1,
        "initialized": False,
        "next_sequence": 1,
        "bot_net_sold": 0,
        "orders": {}
    }


def load_state():
    if not os.path.exists(STATE_FILE):
        return default_state()

    with open(STATE_FILE, "r") as file:
        state = json.load(file)

    if not isinstance(state, dict) or state.get("version") != 1:
        raise RuntimeError("Invalid DhavalETH state file")

    required = [
        "initialized",
        "next_sequence",
        "bot_net_sold",
        "orders"
    ]

    for key in required:
        if key not in state:
            raise RuntimeError(f"State file missing {key}")

    if not isinstance(state["orders"], dict):
        raise RuntimeError("Invalid order records in state")

    return state


STATE = load_state()
STATE.setdefault("counter_queue", {})


def save_state():
    temp = STATE_FILE + ".tmp"

    with open(temp, "w") as file:
        json.dump(STATE, file, indent=2)
        file.flush()
        os.fsync(file.fileno())

    os.replace(temp, STATE_FILE)


# ============================================================
# HELPERS
# ============================================================

def unwrap(response):
    if isinstance(response, dict) and isinstance(response.get("result"), dict):
        return response["result"]
    return response


def round_price(value):
    return Decimal(str(value)).quantize(
        Decimal("1"),
        rounding=ROUND_HALF_UP
    )


def price_text(value):
    return format(Decimal(str(value)).normalize(), "f")


def new_client_id(role):
    sequence = int(STATE["next_sequence"])
    STATE["next_sequence"] = sequence + 1
    save_state()

    return f"{BOT_PREFIX}{role}-{sequence}-{uuid.uuid4().hex[:6]}"


# ============================================================
# MARKET DATA
# ============================================================

def get_cmp():
    ticker = unwrap(client.get_ticker(SYMBOL))

    if not isinstance(ticker, dict):
        raise RuntimeError(f"Invalid ticker response: {ticker}")

    for key in ("close", "last_price", "mark_price", "spot_price"):
        if ticker.get(key) is not None:
            price = Decimal(str(ticker[key]))
            if price > 0:
                return price

    raise RuntimeError(f"Cannot read ETHUSD CMP: {ticker}")


def get_net_position():
    position = unwrap(client.get_position(PRODUCT_ID))

    if not isinstance(position, dict):
        raise RuntimeError(f"Invalid position response: {position}")

    if position.get("size") is None:
        raise RuntimeError(f"Position size missing: {position}")

    return int(position["size"])


# ============================================================
# BOT OPEN ORDERS
# ============================================================

def get_live_orders():
    response = client.get_live_orders(
        query={
            "product_ids": PRODUCT_ID,
            "states": "open,pending"
        }
    )

    if response is None:
        return []

    if isinstance(response, dict):
        response = response.get("result", response.get("orders"))

    if not isinstance(response, list):
        raise RuntimeError(f"Invalid live orders response: {response}")

    return [
        order for order in response
        if int(order.get("product_id", -1)) == PRODUCT_ID
    ]


def get_bot_open_orders():
    result = {}

    for order in get_live_orders():
        client_id = str(order.get("client_order_id", ""))

        if client_id.startswith(BOT_PREFIX):
            result[str(order["id"])] = order

    return result


def get_bot_open_sells():
    total = 0

    for order in get_bot_open_orders().values():
        if str(order.get("side", "")).lower() == "sell":
            remaining = order.get("unfilled_size")

            if remaining is None:
                raise RuntimeError(
                    f"Missing remaining size for bot order {order.get('id')}"
                )

            total += int(remaining)

    return total


def get_allocation():
    return int(STATE["bot_net_sold"]) + get_bot_open_sells()


# ============================================================
# PLACE ORDER
# ============================================================

def place_limit(side, price, role, client_id=None):
    side = side.lower()

    if side not in ("buy", "sell"):
        raise RuntimeError("Invalid order side")

    price = round_price(price)

    if price <= 0:
        raise RuntimeError("Invalid order price")

    if side == "sell":
        position = get_net_position()

        if position <= 0:
            raise RuntimeError(
                "SELL BLOCKED: account is not net long"
            )

        reserved = get_bot_open_sells()

        if position < reserved + ORDER_SIZE:
            log(
                "SELL BLOCKED: insufficient unreserved long inventory. "
                f"Position={position}, reserved={reserved}"
            )
            return None

        allocation = int(STATE["bot_net_sold"]) + reserved

        if allocation + ORDER_SIZE > MAX_POSITION:
            log(
                f"SELL BLOCKED: bot allocation cap "
                f"{allocation}/{MAX_POSITION}"
            )
            return None

    client_id = client_id or new_client_id(role)

    payload = {
        "product_id": PRODUCT_ID,
        "size": ORDER_SIZE,
        "side": side,
        "order_type": "limit_order",
        "limit_price": price_text(price),
        "time_in_force": "gtc",
        "post_only": False,
        "reduce_only": side == "sell",
        "client_order_id": client_id
    }

    log(
        f"PLACING {side.upper()} "
        f"{ORDER_SIZE} LOT @ ${price_text(price)}"
    )

    response = unwrap(client.create_order(payload))

    if not isinstance(response, dict) or response.get("id") is None:
        raise RuntimeError(f"Order not confirmed: {response}")

    order_id = str(response["id"])

    STATE["orders"][order_id] = {
        "side": side,
        "role": role,
        "price": str(price),
        "size": ORDER_SIZE,
        "client_order_id": client_id,
        "handled": False
    }

    save_state()

    log(f"ORDER CREATED | ID={order_id} | {client_id}")

    return order_id


# ============================================================
# STARTUP CHECK
# ============================================================

def reconcile_startup():
    live = get_bot_open_orders()

    exchange_ids = set(live.keys())

    local_ids = {
        str(order_id)
        for order_id, info in STATE["orders"].items()
        if not info.get("handled", False)
        and str(order_id) in exchange_ids
    }

    unknown_ids = exchange_ids - set(STATE["orders"])
    if unknown_ids:
        raise RuntimeError(
            "STARTUP HALTED: unknown DhavalETH live orders: "
            f"{sorted(unknown_ids)}"
        )

    missing_ids = {
        str(order_id)
        for order_id, info in STATE["orders"].items()
        if not info.get("handled", False)
        and str(order_id) not in exchange_ids
    }

    for order_id in missing_ids:
        order = get_order_status(order_id)
        state = str(order.get("state", "")).lower()
        if state in ("open", "pending"):
            raise RuntimeError(f"STARTUP HALTED: order {order_id} missing from live orders")

    position = get_net_position()

    if position < 0:
        raise RuntimeError(
            f"STARTUP HALTED: account is net short by {abs(position)} lots"
        )

    reserved = get_bot_open_sells()

    if position < reserved:
        raise RuntimeError(
            "STARTUP HALTED: outstanding DhavalETH sells exceed "
            "the current net long position"
        )

    allocation = int(STATE["bot_net_sold"]) + reserved

    if allocation > MAX_POSITION:
        raise RuntimeError(
            f"STARTUP HALTED: allocation {allocation} exceeds "
            f"Max_Position {MAX_POSITION}"
        )

    log(
        f"STARTUP CHECK OK | Net long={position} | "
        f"Bot net sold={STATE['bot_net_sold']} | "
        f"Open bot sells={reserved} | Allocation={allocation}"
    )


# ============================================================
# INITIAL LADDER
# ============================================================

def initialize_ladder():
    if STATE["initialized"]:
        return

    cmp = get_cmp()
    anchor = round_price(cmp)

    levels = MAX_POSITION // ORDER_SIZE

    log("========================================")
    log("DHAVALETH INITIAL SELL LADDER")
    log("========================================")
    log(f"CMP: {cmp}")
    log(f"First sell: ${price_text(anchor + STEP)}")
    log(f"Order size: {ORDER_SIZE}")
    log(f"Step: ${STEP}")
    log(f"Counter step: ${COUNTER_STEP}")
    log(f"Maximum allocation: {MAX_POSITION}")

    for level in range(1, levels + 1):
        price = anchor + (STEP * level)

        order_id = place_limit(
            side="sell",
            price=price,
            role="I"
        )

        if order_id is None:
            raise RuntimeError(
                "Initial ladder stopped because inventory or allocation "
                "was insufficient. Initialization remains incomplete."
            )

        time.sleep(0.15)

    STATE["initialized"] = True
    save_state()

    log(f"INITIAL LADDER READY | {levels} sell orders")


# ============================================================
# ORDER STATUS
# ============================================================

def get_order_status(order_id):
    response = unwrap(client.get_order_by_id(int(order_id)))

    if not isinstance(response, dict):
        raise RuntimeError(f"Invalid order status: {response}")

    return response


def get_verified_fill(order, info):
    total_size = int(order.get("size", info["size"]))
    unfilled = order.get("unfilled_size")

    if unfilled is None:
        raise RuntimeError(
            f"Cannot verify filled quantity for order {order.get('id')}"
        )

    filled = total_size - int(unfilled)

    if filled < 0 or filled > total_size:
        raise RuntimeError("Invalid fill quantity")

    if filled not in (0, total_size):
        raise RuntimeError(
            "Partial fill detected. Bot halted for manual reconciliation."
        )

    price = order.get("average_fill_price")

    if price is None:
        price = order.get("limit_price", info["price"])

    return filled, round_price(price)


# ============================================================
# PROCESS FILLS
# ============================================================


def find_order_by_client_id(client_id):
    path = f"/v2/orders/client_order_id/{client_id}"
    try:
        response = client.request("GET", path, auth=True)
    except Exception as exc:
        message = str(exc).lower()
        if "order_not_found" in message or "order not found" in message:
            return None
        raise

    response = unwrap(response)
    if isinstance(response, dict):
        error_text = str(response.get("error", response.get("message", ""))).lower()
        if "order_not_found" in error_text or "order not found" in error_text:
            return None
        if response.get("id") is not None:
            return response
    raise RuntimeError(f"Unexpected client-order lookup response: {response}")


def process_pending_counters():
    for client_id, item in list(STATE["counter_queue"].items()):
        if item.get("status") != "pending":
            continue

        # Recover an order accepted by Delta before a previous process crashed.
        existing = find_order_by_client_id(client_id)

        if existing is not None:
            order_id = str(existing["id"])
            STATE["orders"][order_id] = {
                "side": item["side"],
                "role": item["role"],
                "price": str(item["price"]),
                "size": int(item["size"]),
                "client_order_id": client_id,
                "handled": False
            }
            item["status"] = "submitted"
            item["exchange_order_id"] = order_id
            save_state()
            log(f"RECOVERED COUNTER ORDER | ID={order_id} | CID={client_id}")
            continue

        order_id = place_limit(
            side=item["side"],
            price=Decimal(str(item["price"])),
            role=item["role"],
            client_id=client_id
        )

        # None means a sell was blocked by current inventory/allocation checks.
        # Keep it pending and retry on the next polling cycle.
        if order_id is None:
            continue

        item["status"] = "submitted"
        item["exchange_order_id"] = str(order_id)
        save_state()
        log(f"COUNTER ORDER SUBMITTED | ID={order_id} | CID={client_id}")


def process_tracked_orders():
    for order_id, info in list(STATE["orders"].items()):
        if info.get("handled"):
            continue

        order = get_order_status(order_id)
        status = str(order.get("state", "")).lower()

        if status in ("open", "pending"):
            continue

        if status in ("cancelled", "canceled"):
            filled, _ = get_verified_fill(order, info)
            if filled:
                raise RuntimeError(
                    f"Cancelled order {order_id} has a fill. "
                    "Manual reconciliation required."
                )
            info["handled"] = True
            save_state()
            log(f"ORDER CANCELLED | {order_id}")
            continue

        if status != "closed":
            continue

        filled, fill_price = get_verified_fill(order, info)

        if filled == 0:
            info["handled"] = True
            save_state()
            continue

        if filled != ORDER_SIZE:
            raise RuntimeError("Partial fill detected. Manual reconciliation required.")

        # Prepare a deterministic counter order ID.
        counter_side = "buy" if info["side"] == "sell" else "sell"
        counter_role = "B" if counter_side == "buy" else "S"
        counter_price = (
            fill_price - COUNTER_STEP
            if counter_side == "buy"
            else fill_price + COUNTER_STEP
        )
        counter_price = round_price(counter_price)
        counter_id = (
            "DHE-C-" +
            hashlib.sha256(str(order_id).encode()).hexdigest()[:20]
        )

        # One atomic local save records the fill AND its pending counter.
        # A restart cannot lose the counter-order instruction.
        if counter_id not in STATE["counter_queue"]:
            STATE["counter_queue"][counter_id] = {
                "status": "pending",
                "parent_order_id": str(order_id),
                "side": counter_side,
                "role": counter_role,
                "price": str(counter_price),
                "size": filled
            }

            if info["side"] == "sell":
                STATE["bot_net_sold"] = int(STATE["bot_net_sold"]) + filled
            else:
                STATE["bot_net_sold"] = int(STATE["bot_net_sold"]) - filled

        info["handled"] = True
        info["fill_price"] = str(fill_price)
        info["filled_size"] = filled
        save_state()

        log(
            f"{info['side'].upper()} FILLED | ID={order_id} | "
            f"Price={price_text(fill_price)} | "
            f"Counter queued={counter_id}"
        )

    process_pending_counters()


def main():
    log("========================================")
    log("STARTING DHAVALETH")
    log("========================================")

    log(f"AWS secret: {SECRET_NAME}")
    log(f"Symbol: {SYMBOL}")
    log(f"Product ID: {PRODUCT_ID}")
    log(f"Max position: {MAX_POSITION}")
    log(f"Order size: {ORDER_SIZE}")
    log(f"Step: {STEP}")
    log(f"Counter step: {COUNTER_STEP}")

    cmp = get_cmp()
    log(f"Delta API connected | CMP=${cmp}")

    reconcile_startup()
    initialize_ladder()

    log("DHAVALETH RUNNING")

    while True:
        try:
            process_tracked_orders()
            time.sleep(POLL_SECONDS)

        except KeyboardInterrupt:
            log("DHAVALETH STOPPED")
            break

        except Exception as error:
            log(f"ERROR: {type(error).__name__}: {error}")
            time.sleep(ERROR_SLEEP)


if __name__ == "__main__":
    main()
