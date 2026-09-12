import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.error import TimedOut

import calculator


class DownloadDocumentTests(unittest.IsolatedAsyncioTestCase):
    async def test_retries_temporary_network_failure(self):
        telegram_file = AsyncMock()
        document = AsyncMock()
        document.get_file.side_effect = [TimedOut("temporary timeout"), telegram_file]

        with patch.object(calculator.asyncio, "sleep", new_callable=AsyncMock) as sleep:
            await calculator.download_document(document, "receipt.json")

        self.assertEqual(document.get_file.await_count, 2)
        sleep.assert_awaited_once_with(1)
        telegram_file.download_to_drive.assert_awaited_once()

    async def test_raises_after_all_attempts_fail(self):
        document = AsyncMock()
        document.get_file.side_effect = TimedOut("persistent timeout")

        with patch.object(calculator.asyncio, "sleep", new_callable=AsyncMock) as sleep:
            with self.assertRaises(TimedOut):
                await calculator.download_document(document, "receipt.json")

        self.assertEqual(document.get_file.await_count, calculator.FILE_DOWNLOAD_ATTEMPTS)
        self.assertEqual(sleep.await_count, calculator.FILE_DOWNLOAD_ATTEMPTS - 1)

    async def test_process_json_keeps_waiting_after_download_timeout(self):
        message = SimpleNamespace(
            document=SimpleNamespace(file_name="receipt.json"),
            reply_text=AsyncMock(),
        )
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=123),
            message=message,
        )

        with (
            patch.object(calculator, "ALLOWED_USERS", []),
            patch.object(
                calculator,
                "download_document",
                AsyncMock(side_effect=TimedOut("persistent timeout")),
            ),
        ):
            state = await calculator.process_json(update, None)

        self.assertEqual(state, calculator.PROCESSING_JSON)
        self.assertEqual(message.reply_text.await_count, 2)
        self.assertIn("Попробуйте отправить JSON ещё раз", message.reply_text.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
