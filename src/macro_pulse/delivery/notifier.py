import os
from asyncio import sleep
from pathlib import Path

from telegram import Bot

from ..core.logging import get_logger


logger = get_logger(__name__)


async def send_telegram_report(
    token,
    chat_id,
    message_text="Daily Macro Pulse Report",
    image_path=None,
    image_paths=None,
    attempts=2,
    delivery_receipt_path=None,
    send_text=True,
):
    if not token or not chat_id:
        logger.info("Telegram token or chat_id missing. Skipping Telegram.")
        return False

    photo_paths = list(image_paths or [])

    if image_path and not photo_paths:
        photo_paths.append(image_path)

    bot = Bot(token=token)

    # 1. 텍스트 메시지는 별도로 재시도
    if send_text:
        for attempt in range(1, attempts + 1):
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=message_text,
                )
                break

            except Exception as exc:
                logger.warning(
                    "Failed to send Telegram message (attempt %s/%s): %s",
                    attempt,
                    attempts,
                    exc,
                )

                if attempt == attempts:
                    logger.exception("Telegram message delivery failed")
                    return False

                await sleep(1)

        # Persist the text receipt before optional photos so backup runs do not
        # repeat the report if screenshot generation or photo delivery is slow.
        if delivery_receipt_path:
            receipt = Path(delivery_receipt_path)
            receipt.parent.mkdir(parents=True, exist_ok=True)
            receipt.write_text("text-delivered\n", encoding="utf-8")

    # 2. 이미지는 이미지별로 따로 재시도
    for photo_path in photo_paths:
        if not photo_path or not os.path.exists(photo_path):
            continue

        photo_sent = False

        for attempt in range(1, attempts + 1):
            try:
                with open(photo_path, "rb") as image_handle:
                    await bot.send_photo(
                        chat_id=chat_id,
                        photo=image_handle,
                    )

                logger.info("Telegram photo sent: %s", photo_path)
                photo_sent = True
                break

            except Exception as exc:
                logger.warning(
                    "Failed to send Telegram photo %s (attempt %s/%s): %s",
                    photo_path,
                    attempt,
                    attempts,
                    exc,
                )

                if attempt < attempts:
                    await sleep(1)

        if not photo_sent:
            logger.error("Telegram photo delivery failed: %s", photo_path)
            return False

    return True
