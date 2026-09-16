import os
import uuid
import json
import logging
from pathlib import Path

from dotenv import load_dotenv
from aiohttp import web, ClientSession
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_API_KEY = os.getenv("CLIENT_API_KEY")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "payx_secret_8f3k9m2x7p1")
WEBAPP_URL = os.getenv("WEBAPP_URL", "")

PAYX_URL = "https://api.payxgateway.com/api/public/exchange/orders"
MIN_AMOUNT = 100
MAX_AMOUNT = 300000

orders = {}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    if not WEBAPP_URL:
        await message.answer("Бот ещё не настроен. Попробуйте позже.")
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="💳 Оплатить",
            web_app=WebAppInfo(url=f"{WEBAPP_URL}/webapp/")
        )]
    ])
    
    await message.answer(
        "Привет! Нажми кнопку ниже, чтобы создать платёж.\n"
        "Ты сам указываешь сумму.",
        reply_markup=kb
    )


async def create_order(request: web.Request):
    try:
        data = await request.json()
        amount = float(data.get("amount", 0))
        init_data = data.get("initData", "")

        if amount < MIN_AMOUNT or amount > MAX_AMOUNT:
            return web.json_response({"ok": False, "error": "Некорректная сумма"}, status=400)

        if not init_data:
            return web.json_response({"ok": False, "error": "Нет данных Telegram"}, status=400)

        user_id = None
        try:
            from urllib.parse import parse_qs
            parsed = parse_qs(init_data)
            user_json = parsed.get("user", ["{}"])[0]
            user = json.loads(user_json)
            user_id = user.get("id")
        except Exception:
            pass

        if not user_id:
            return web.json_response({"ok": False, "error": "Не удалось определить пользователя"}, status=400)

        client_order_id = f"tg_{user_id}_{uuid.uuid4().hex[:10]}"

        base_url = str(request.url.origin())
        webhook_url = f"{base_url}/webhook/{WEBHOOK_SECRET}"

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

        async with ClientSession() as session:
            async with session.post(PAYX_URL, json=payload, headers=headers, timeout=15) as resp:
                result = await resp.json()

        if result.get("status") != 1:
            error = result.get("errors", [{}])[0].get("code", "ORDER_FAILED")
            logger.error(f"PayX error: {result}")
            return web.json_response({"ok": False, "error": f"Ошибка PayX: {error}"}, status=400)

        payment_link = result.get("payment", {}).get("paymentLink")
        if not payment_link:
            return web.json_response({"ok": False, "error": "Нет ссылки на оплату"}, status=400)

        orders[client_order_id] = {
            "user_id": user_id,
            "amount": amount,
            "status": "created",
            "order_id": result.get("order", {}).get("id")
        }

        logger.info(f"Created order {client_order_id} for user {user_id}, amount {amount}")

        return web.json_response({
            "ok": True,
            "paymentLink": payment_link,
            "clientOrderId": client_order_id
        })

    except Exception as e:
        logger.exception("create_order error")
        return web.json_response({"ok": False, "error": str(e)}, status=500)


async def payx_webhook(request: web.Request):
    secret = request.match_info.get("secret")
    if secret != WEBHOOK_SECRET:
        return web.Response(status=404)

    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400)

    client_order_id = data.get("clientOrderId")
    status = data.get("status")
    credited = data.get("creditedAmount")

    logger.info(f"Webhook: {client_order_id} → {status}")

    order = orders.get(client_order_id)
    if not order:
        return web.Response(status=200)

    if order.get("status") == "confirmed":
        return web.Response(status=200)

    if status == "CONFIRMED":
        order["status"] = "confirmed"
        user_id = order["user_id"]
        amount = order["amount"]

        try:
            await bot.send_message(
                user_id,
                f"✅ Оплата получена!\n\n"
                f"Сумма: <b>{amount:,.0f} ₽</b>\n"
                f"Зачислено: {credited} {data.get('creditedCurrency', '')}\n"
                f"ID: <code>{client_order_id}</code>",
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Notify error: {e}")

    elif status in ("CANCELED", "FAILED"):
        order["status"] = status.lower()

    return web.Response(status=200)


async def serve_webapp(request: web.Request):
    html_path = Path(__file__).parent / "webapp" / "index.html"
    return web.FileResponse(html_path)


async def on_startup(app):
    # Запускаем бота в режиме polling
    import asyncio
    asyncio.create_task(dp.start_polling(bot))


def main():
    app = web.Application()
    
    app.router.add_post("/api/create-order", create_order)
    app.router.add_post("/webhook/{secret}", payx_webhook)
    app.router.add_get("/webapp/", serve_webapp)
    app.router.add_get("/webapp", serve_webapp)

    app.on_startup.append(on_startup)

    port = int(os.getenv("PORT", 8080))
    web.run_app(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
