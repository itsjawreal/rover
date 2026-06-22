from __future__ import annotations

import logging
import unittest

from src.core.state import setup_logging


class StateLoggingTests(unittest.TestCase):
    def test_setup_logging_replaces_and_closes_existing_handlers(self) -> None:
        root = logging.getLogger()
        original_handlers = list(root.handlers)
        try:
            setup_logging()
            first_handlers = list(root.handlers)
            self.assertEqual(len(first_handlers), 2)
            first_file_handlers = [handler for handler in first_handlers if isinstance(handler, logging.FileHandler)]
            self.assertEqual(len(first_file_handlers), 1)

            setup_logging()
            second_handlers = list(root.handlers)
            self.assertEqual(len(second_handlers), 2)
            self.assertNotEqual(first_handlers, second_handlers)

            for handler in first_file_handlers:
                self.assertTrue(handler.stream is None or getattr(handler.stream, "closed", False))
        finally:
            for handler in list(root.handlers):
                try:
                    handler.close()
                except Exception:
                    pass
            root.handlers.clear()
            for handler in original_handlers:
                root.addHandler(handler)
