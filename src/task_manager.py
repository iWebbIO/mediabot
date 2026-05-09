from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Dict


@dataclass(frozen=True)
class JobScope:
    chat_id: int
    user_id: int


class TaskManager:
    def __init__(
        self,
        max_global: int,
        max_per_chat: int,
        max_per_user: int,
    ):
        self._global_sem = asyncio.Semaphore(max_global)
        self._per_chat_sem: Dict[int, asyncio.Semaphore] = {}
        self._per_user_sem: Dict[int, asyncio.Semaphore] = {}
        self._max_per_chat = max_per_chat
        self._max_per_user = max_per_user

        self._jobs: Dict[str, asyncio.Task] = {}

    def _chat_sem(self, chat_id: int) -> asyncio.Semaphore:
        if chat_id not in self._per_chat_sem:
            self._per_chat_sem[chat_id] = asyncio.Semaphore(self._max_per_chat)
        return self._per_chat_sem[chat_id]

    def _user_sem(self, user_id: int) -> asyncio.Semaphore:
        if user_id not in self._per_user_sem:
            self._per_user_sem[user_id] = asyncio.Semaphore(self._max_per_user)
        return self._per_user_sem[user_id]

    async def acquire(self, job_id: str, chat_id: int, user_id: int):
        await self._global_sem.acquire()
        try:
            await self._chat_sem(chat_id).acquire()
            try:
                await self._user_sem(user_id).acquire()
            except Exception:
                self._chat_sem(chat_id).release()
                raise
        except Exception:
            self._global_sem.release()
            raise

    def release(self, chat_id: int, user_id: int):
        self._user_sem(user_id).release()
        self._chat_sem(chat_id).release()
        self._global_sem.release()

