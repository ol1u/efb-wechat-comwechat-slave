import time
import unittest

from efb_wechat_comwechat_slave.delivery_retry import DeliveryRetryQueue


def _wait_until(predicate, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class DeliveryRetryTest(unittest.TestCase):
    def test_retry_then_success(self):
        q = DeliveryRetryQueue(backoffs=(0.02, 0.05), max_queue=10)
        calls = []

        def deliver():
            calls.append(1)
            if len(calls) < 2:
                raise RuntimeError("boom")

        q.submit(deliver, lambda: False, lambda: calls.append("exhausted"), "t1")
        self.assertTrue(_wait_until(lambda: len(calls) >= 2))
        self.assertNotIn("exhausted", calls)

    def test_original_success_skips_retry(self):
        q = DeliveryRetryQueue(backoffs=(0.02,), max_queue=10)
        delivered = []
        q.submit(lambda: delivered.append(1), lambda: True,
                 lambda: delivered.append("ex"), "t2")
        time.sleep(0.2)
        self.assertEqual(delivered, [])

    def test_exhaustion_calls_on_exhausted(self):
        q = DeliveryRetryQueue(backoffs=(0.02, 0.03), max_queue=10)
        exhausted = []

        def deliver():
            raise RuntimeError("always fails")

        q.submit(deliver, lambda: False, lambda: exhausted.append(1), "t3")
        self.assertTrue(_wait_until(lambda: len(exhausted) == 1))
        self.assertEqual(exhausted, [1])

    def test_queue_cap_drops_oldest(self):
        q = DeliveryRetryQueue(backoffs=(60.0,), max_queue=2)
        noop = lambda: None
        q.submit(noop, lambda: False, noop, "a")
        q.submit(noop, lambda: False, noop, "b")
        q.submit(noop, lambda: False, noop, "c")
        self.assertEqual([it.label for it in q._fifo], ["b", "c"])


if __name__ == "__main__":
    unittest.main()
