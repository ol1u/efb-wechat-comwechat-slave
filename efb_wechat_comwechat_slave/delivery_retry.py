"""微信 -> Telegram 投递失败后的延迟重投队列。

背景:ComWechat.send_efb_msgs 用 60 秒超时隔离投递,超时/异常即放弃该条
(此前只记日志,120 秒后发超时提示)。10-25 曾因此丢过 108 条消息。
本队列把失败消息留下来,按退避间隔重投,多次失败后才发超时提示("[微信→Telegram 投递超时，请在微信查看原文]"),
把"静默丢失"变成"延迟送达或明确提示"。

设计要点:
- 单后台 daemon 线程,不持久化:进程重启(每日 04:00)即清空,和此前行为一致。
- 每次重投前先查原投递是否已成功,避免重复;原投递仍在跑时重投可能
  产生重复(与此前注释接受的风险相同),退避间隔已把概率压得很低。
- 队列有上限,满时丢弃最早提交的一条并记日志,防止长断网时内存膨胀。
- 重投顺序可能晚于后续消息(乱序),好过丢失。
"""

import heapq
import itertools
import logging
import threading
import time
from collections import deque
from typing import Callable, Deque, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 退避间隔(秒):第 1/2/3 次重试前分别等待,总计约 21 分钟后放弃。
RETRY_BACKOFFS = (60.0, 300.0, 900.0)
MAX_QUEUE = 200


class _Item:
    __slots__ = ("deliver", "check_done", "on_exhausted", "label",
                 "attempts", "next_try", "dropped")

    def __init__(self, deliver: Callable[[], None],
                 check_done: Callable[[], bool],
                 on_exhausted: Callable[[], None],
                 label: str):
        self.deliver = deliver
        self.check_done = check_done
        self.on_exhausted = on_exhausted
        self.label = label
        self.attempts = 0
        self.next_try = 0.0
        self.dropped = False


class DeliveryRetryQueue:
    """延迟重投队列。deliver 成功返回、失败抛异常;check_done 为真则跳过。"""

    def __init__(self, backoffs: Tuple[float, ...] = RETRY_BACKOFFS,
                 max_queue: int = MAX_QUEUE):
        self._backoffs = backoffs
        self._max_queue = max_queue
        self._heap: List[Tuple[float, int, _Item]] = []
        self._fifo: Deque[_Item] = deque()
        self._seq = itertools.count()
        self._cond = threading.Condition()
        self._thread = threading.Thread(target=self._run,
                                        name="cw-deliver-retry", daemon=True)
        self._thread.start()

    def submit(self, deliver: Callable[[], None],
               check_done: Callable[[], bool],
               on_exhausted: Callable[[], None],
               label: str) -> None:
        """投递失败后调用。队列满时丢弃最早提交的一条并记日志。"""
        item = _Item(deliver, check_done, on_exhausted, label)
        item.next_try = time.monotonic() + self._backoffs[0]
        with self._cond:
            if len(self._fifo) >= self._max_queue:
                dropped = self._fifo.popleft()
                dropped.dropped = True
                logger.error("重投队列已满,丢弃最早提交的一条: %s", dropped.label)
            self._fifo.append(item)
            heapq.heappush(self._heap, (item.next_try, next(self._seq), item))
            self._cond.notify()

    def _run(self) -> None:
        while True:
            with self._cond:
                while True:
                    while self._heap and self._heap[0][2].dropped:
                        heapq.heappop(self._heap)
                    if not self._heap:
                        self._cond.wait()
                        continue
                    wait = self._heap[0][0] - time.monotonic()
                    if wait <= 0:
                        break
                    self._cond.wait(timeout=wait)
                _, _, item = heapq.heappop(self._heap)
                try:
                    self._fifo.remove(item)
                except ValueError:
                    pass
            self._attempt(item)

    def _attempt(self, item: _Item) -> None:
        try:
            if item.check_done():
                logger.info("重投跳过(原投递已成功): %s", item.label)
                return
        except Exception:
            pass
        try:
            item.deliver()
        except Exception:
            logger.exception("重投失败: %s", item.label)
            item.attempts += 1
            if item.attempts < len(self._backoffs):
                item.next_try = time.monotonic() + self._backoffs[item.attempts]
                with self._cond:
                    self._fifo.append(item)
                    heapq.heappush(self._heap,
                                   (item.next_try, next(self._seq), item))
                    self._cond.notify()
            else:
                logger.error("重投 %d 次均失败,放弃: %s", item.attempts, item.label)
                try:
                    item.on_exhausted()
                except Exception:
                    logger.exception("重投耗尽后发提示失败: %s", item.label)
            return
        logger.info("重投成功: %s", item.label)
