import os
import uuid
import json
import hmac
import hashlib
import logging
import asyncio
from pathlib import Path
from urllib.parse import parse_qs

from dotenv import load_dotenv
from aiohttp import web, ClientSession

from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from aiogram.types import (
    WebAppInfo,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)


# ============================================================
# CONFIG
# ============================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_API_KEY = os.getenv("CLIENT_API_KEY")
WEBAPP_URL = os.getenv("WEBAPP_URL")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

PAYX_URL = "https://api.payxgateway.com/api/public/exchange/orders"

MIN_AMOUNT = 100
MAX_AMOUNT = 300000

PORT = int(os.getenv("PORT", "8080"))

BASE_DIR = Path(__file__).resolve().parent
WEBAPP_DIR = BASE_DIR / "webapp"


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)


# ============================================================
# ENV CHECK
# ============================================================

required_variables = {
    "BOT_TOKEN": BOT_TOKEN,
    "CLIENT_ID": CLIENT_ID,
    "CLIENT_API_KEY": CLIENT_API_KEY,
    "WEBAPP_URL": WEBAPP_URL,
    "WEBHOOK_SECRET": WEBHOOK_SECRET,
}

missing = [
    name
    for name, value in required_variables.items()
    if not value
]

if missing:
    raise RuntimeError(
        "Missing Railway environment variables: "
        + ", ".join(missing)
    )


# ============================================================
# BOT
# ============================================================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# ============================================================
# TEMPORARY ORDERS STORAGE
# ============================================================

orders = {}


# ============================================================
# TELEGRAM WEBAPP INIT DATA VALIDATION
# ============================================================

def validate_telegram_init_data(init_data: str) -> dict:

    if not init_data:
        raise ValueError("initData is empty")

    parsed = parse_qs(
        init_data,
        keep_blank_values=True
    )

    received_hash = parsed.get(
        "hash",
        [None]
    )[0]

    if not received_hash:
        raise ValueError(
            "Telegram hash is missing"
        )

    data_check_items = []

    for key in sorted(parsed.keys()):

        if key == "hash":
            continue

        value = parsed[key][0]

        data_check_items.append(
            f"{key}={value}"
        )

    data_check_string = "\n".join(
        data_check_items
    )

    secret_key = hmac.new(
        b"WebAppData",
        BOT_TOKEN.encode(),
        hashlib.sha256
    ).digest()

    calculated_hash = hmac.new(
        secret_key,
        data_check_string.encode(),
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(
        calculated_hash,
        received_hash
    ):
        raise ValueError(
            "Invalid Telegram WebApp initData"
        )

    user_data = parsed.get(
        "user",
        [None]
    )[0]

    if not user_data:
        raise ValueError(
            "Telegram user data missing"
        )

    return json.loads(user_data)


# ============================================================
# /START
# ============================================================

@dp.message(CommandStart())
async def start_handler(message: types.Message):

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💳 Оплатить",
                    web_app=WebAppInfo(
                        url=f"{WEBAPP_URL.rstrip('/')}/webapp/"
                    )
                )
            ]
        ]
    )

    await message.answer(
        "Привет! 👋\n\n"
        "Нажми кнопку ниже, чтобы создать оплату.",
        reply_markup=keyboard
    )


# ============================================================
# ORDINARY MESSAGE HANDLER
# ============================================================

@dp.message()
async def message_handler(message: types.Message):

    text = message.text or ""

    await message.answer(
        "Получил сообщение: " + text
    )


# ============================================================
# CREATE PAYX ORDER
# ============================================================

async def create_order(request: web.Request):

    try:
        data = await request.json()

    except Exception:

        return web.json_response(
            {
                "success": False,
                "error": "Invalid JSON"
            },
            status=400
        )

    try:
        amount = int(
            data.get("amount", 0)
        )

    except Exception:

        amount = 0

    if (
        amount < MIN_AMOUNT
        or amount > MAX_AMOUNT
    ):

        return web.json_response(
            {
                "success": False,
                "error": (
                    f"Сумма должна быть от "
                    f"{MIN_AMOUNT} до {MAX_AMOUNT} ₽"
                )
            },
            status=400
        )

    init_data = data.get(
        "initData",
        ""
    )

    try:

        telegram_user = (
            validate_telegram_init_data(
                init_data
            )
        )

    except Exception as e:

        logger.warning(
            "Telegram initData validation failed: %s",
            e
        )

        return web.json_response(
            {
                "success": False,
                "error": (
                    "Telegram authorization failed"
                )
            },
            status=403
        )

    telegram_user_id = telegram_user.get("id")

    if not telegram_user_id:

        return web.json_response(
            {
                "success": False,
                "error": (
                    "Telegram user ID missing"
                )
            },
            status=400
        )

    client_order_id = (
        f"tg_{telegram_user_id}_"
        f"{uuid.uuid4().hex[:12]}"
    )

    webhook_url = (
        f"{request.url.origin()}"
        f"/webhook/payx"
        f"?secret={WEBHOOK_SECRET}"
    )

    payload = {
        "webhook": webhook_url,
        "paymentMethodCode": "deeplink",
        "fiatAmount": amount,
        "clientOrderId": client_order_id
    }

    headers = {
        "Content-Type": "application/json",
        "x-client-id": CLIENT_ID,
        "x-client-api-key": CLIENT_API_KEY
    }

    logger.info(
        "Creating PayX order %s for %s RUB",
        client_order_id,
        amount
    )

    try:

        timeout = 30

        async with ClientSession() as session:

            async with session.post(
                PAYX_URL,
                json=payload,
                headers=headers,
                timeout=timeout
            ) as response:

                response_text = (
                    await response.text()
                )

                logger.info(
                    "PayX HTTP %s: %s",
                    response.status,
                    response_text
                )

                try:

                    result = json.loads(
                        response_text
                    )

                except Exception:

                    return web.json_response(
                        {
                            "success": False,
                            "error": (
                                "PayX returned "
                                "invalid JSON"
                            ),
                            "http_status": (
                                response.status
                            )
                        },
                        status=502
                    )

    except Exception:

        logger.exception(
            "PayX request failed"
        )

        return web.json_response(
            {
                "success": False,
                "error": (
                    "Payment service unavailable"
                )
            },
            status=502
        )

    if response.status >= 400:

        return web.json_response(
            {
                "success": False,
                "error": "PayX API error",
                "details": result
            },
            status=502
        )

    if result.get("status") != 1:

        return web.json_response(
            {
                "success": False,
                "error": (
                    "PayX rejected the order"
                ),
                "details": result
            },
            status=400
        )

    payment = result.get("payment") or {}

    payment_link = payment.get(
        "paymentLink"
    )

    if not payment_link:

        logger.error(
            "PayX response has no paymentLink: %s",
            result
        )

        return web.json_response(
            {
                "success": False,
                "error": (
                    "Payment link was not "
                    "returned by PayX"
                )
            },
            status=502
        )

    orders[client_order_id] = {
        "telegram_user_id": telegram_user_id,
        "amount": amount,
        "status": "PENDING",
        "payment_link": payment_link
    }

    logger.info(
        "Order created: %s",
        client_order_id
    )

    return web.json_response(
        {
            "success": True,
            "orderId": client_order_id,
            "paymentLink": payment_link
        }
    )


# ============================================================
# PAYX WEBHOOK
# ============================================================

async def payx_webhook(request: web.Request):

    secret = request.query.get("secret")

    if (
        not secret
        or not hmac.compare_digest(
            secret,
            WEBHOOK_SECRET
        )
    ):

        return web.json_response(
            {
                "success": False,
                "error": "Unauthorized"
            },
            status=401
        )

    try:

        data = await request.json()

    except Exception:

        return web.json_response(
            {
                "success": False,
                "error": "Invalid JSON"
            },
            status=400
        )

    logger.info(
        "PayX webhook received: %s",
        data
    )

    client_order_id = data.get(
        "clientOrderId"
    )

    status = str(
        data.get("status", "")
    ).upper()

    if not client_order_id:

        return web.json_response(
            {
                "success": False,
                "error": (
                    "clientOrderId missing"
                )
            },
            status=400
        )

    order = orders.get(
        client_order_id
    )

    if not order:

        logger.warning(
            "Unknown order: %s",
            client_order_id
        )

        return web.json_response(
            {
                "success": True
            }
        )

    # PAYMENT CONFIRMED

    if status == "CONFIRMED":

        order["status"] = "CONFIRMED"

        credited_amount = data.get(
            "creditedAmount"
        )

        telegram_user_id = (
            order["telegram_user_id"]
        )

        text = (
            "✅ <b>Оплата подтверждена</b>\n\n"
            f"Сумма: "
            f"<b>{order['amount']} ₽</b>"
        )

        if credited_amount is not None:

            text += (
                f"\nЗачислено: "
                f"<b>{credited_amount}</b>"
            )

        try:

            await bot.send_message(
                telegram_user_id,
                text,
                parse_mode="HTML"
            )

        except Exception:

            logger.exception(
                "Failed to send "
                "Telegram confirmation"
            )

    # PAYMENT FAILED

    elif status in (
        "CANCELED",
        "CANCELLED",
        "FAILED"
    ):

        order["status"] = status

        telegram_user_id = (
            order["telegram_user_id"]
        )

        try:

            await bot.send_message(
                telegram_user_id,
                "❌ Оплата не была завершена."
            )

        except Exception:

            logger.exception(
                "Failed to send "
                "Telegram failure message"
            )

    else:

        order["status"] = status

    return web.json_response(
        {
            "success": True
        }
    )


# ============================================================
# ORDER STATUS
# ============================================================

async def order_status(
    request: web.Request
):

    order_id = request.match_info.get(
        "order_id"
    )

    order = orders.get(
        order_id
    )

    if not order:

        return web.json_response(
            {
                "success": False,
                "error": "Order not found"
            },
            status=404
        )

    return web.json_response(
        {
            "success": True,
            "order": order
        }
    )


# ============================================================
# WEBAPP
# ============================================================

async def webapp_handler(
    request: web.Request
):

    index_file = (
        WEBAPP_DIR / "index.html"
    )

    if not index_file.exists():

        return web.Response(
            text="webapp/index.html not found",
            status=500
        )

    return web.FileResponse(
        index_file
    )


# ============================================================
# HEALTH CHECK
# ============================================================

async def health(
    request: web.Request
):

    return web.json_response(
        {
            "status": "ok"
        }
    )


# ============================================================
# STARTUP
# ============================================================

async def on_startup(
    app: web.Application
):

    logger.info(
        "Starting Telegram bot polling..."
    )

    await bot.delete_webhook(
        drop_pending_updates=True
    )

    app["polling_task"] = (
        asyncio.create_task(
            dp.start_polling(bot)
        )
    )


# ============================================================
# SHUTDOWN
# ============================================================

async def on_cleanup(
    app: web.Application
):

    logger.info(
        "Stopping Telegram bot..."
    )

    task = app.get(
        "polling_task"
    )

    if task:

        task.cancel()

        try:

            await task

        except asyncio.CancelledError:

            pass

        except Exception:

            logger.exception(
                "Polling task shutdown error"
            )

    await bot.session.close()


# ============================================================
# APPLICATION
# ============================================================

app = web.Application()

app.router.add_get(
    "/",
    health
)

app.router.add_get(
    "/webapp/",
    webapp_handler
)

app.router.add_post(
    "/api/create-order",
    create_order
)

app.router.add_post(
    "/webhook/payx",
    payx_webhook
)

app.router.add_get(
    "/api/order/{order_id}",
    order_status
)

app.on_startup.append(
    on_startup
)

app.on_cleanup.append(
    on_cleanup
)


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    logger.info(
        "Server starting on port %s",
        PORT
    )

    web.run_app(
        app,
        host="0.0.0.0",
        port=PORT
    )
