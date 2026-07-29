"""彩票系统 Plus 远程插件。"""

from __future__ import annotations

import asyncio
import hashlib
import random
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from app.worker.command import current_command_prefix
from app.worker.plugins.base import Plugin, PluginContext, public_entity_display_name, register


@dataclass
class Bet:
    user_id: int
    user_name: str
    number: str
    count: int
    cost: int
    created_at: float = field(default_factory=time.time)


@dataclass
class RoundState:
    round_id: int = 1
    jackpot: int = 0
    bets: list[Bet] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)


@register
class LotteryPlusPlugin(Plugin):
    key = "lottery_plus"
    display_name = "彩票系统 Plus"
    message_channels = {"incoming", "outgoing"}
    owner_only = False
    command_config_keys = {
        "command", "buy_aliases", "my_aliases", "pool_aliases", "history_aliases", "stats_aliases", "hot_aliases", "help_aliases",
        "draw_aliases", "reset_aliases", "sponsor_aliases", "unsponsor_aliases", "refund_aliases",
    }

    def __init__(self) -> None:
        super().__init__()
        self._cfg: dict[str, Any] = {}
        self._state: dict[int, RoundState] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task] = set()
        self._command = "lotto"
        self._aliases: dict[str, set[str]] = {}
        self.commands = {self._command: self._cmd_handler}

    async def on_startup(self, ctx: PluginContext) -> None:
        self._cfg = dict(ctx.config or {})
        self._command = str(self._cfg.get("command", "lotto")).strip() or "lotto"
        self._init_aliases()
        self.commands = {self._command: self._cmd_handler}
        if ctx.log:
            await ctx.log("info", f"[lottery_plus] 已启动，指令：{self._command}")

    async def on_shutdown(self, ctx: PluginContext) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self._locks.clear()
        if ctx.log:
            await ctx.log("info", "[lottery_plus] 已停止")

    async def on_interaction(
        self,
        ctx: PluginContext,
        entry_key: str,
        payload: dict[str, Any],
    ) -> list[dict[str, Any]] | None:
        if entry_key != "start_lottery_plus":
            return None
        event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
        event_type = str(event.get("type") or payload.get("event_type") or "").strip()
        chat_id = self._payload_chat_id(payload, event)
        reply_to = self._payload_message_id(payload, event)
        if not chat_id:
            return [{"type": "send_message", "text": "❌ 彩票模块需要在群聊里使用。", "parse_mode": "plain", "reply_to_message_id": reply_to}]

        lock = self._get_lock(chat_id)
        async with lock:
            st = self._get_state(chat_id)
            self._ensure_auto_draw(chat_id, ctx)

            if event_type == "payment_confirmed":
                amount = self._int_value(payload.get("amount") or event.get("data", {}).get("amount"))
                parsed = self._parse_bet_from_amount(amount)
                if parsed is None:
                    return [
                        {"type": "send_message", "text": self._render_amount_help(amount), "parse_mode": "plain", "reply_to_message_id": reply_to},
                        {"type": "end_session"},
                    ]
                user_id, user_name = self._interaction_user(payload, event, prefer_payer=True)
                text = self._place_bet(st, user_id, user_name, parsed[0], str(parsed[1]))
                return [{"type": "send_message", "text": text, "parse_mode": "plain", "reply_to_message_id": reply_to}, {"type": "end_session"}]

            if event_type == "message":
                return [{"type": "end_session"}]

            message = str(payload.get("message") or "开始下注，祝你好运。")
            return [
                {
                    "type": "send_message",
                    "text": self._render_interaction_prompt(message),
                    "parse_mode": "plain",
                    "reply_to_message_id": reply_to,
                },
                {"type": "end_session"},
            ]

    @staticmethod
    def _int_value(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _payload_chat_id(payload: dict[str, Any], event: dict[str, Any]) -> int:
        return LotteryPlusPlugin._int_value(payload.get("chat_id") or event.get("chat_id"))

    @staticmethod
    def _payload_message_id(payload: dict[str, Any], event: dict[str, Any]) -> int | None:
        value = LotteryPlusPlugin._int_value(payload.get("message_id") or event.get("message_id") or payload.get("source_message_id"))
        return value or None

    @staticmethod
    def _synthetic_user_id(name: str) -> int:
        raw = hashlib.sha256(name.encode("utf-8")).hexdigest()[:12]
        return -int(raw, 16)

    def _interaction_user(self, payload: dict[str, Any], event: dict[str, Any], *, prefer_payer: bool) -> tuple[int, str]:
        event_data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if prefer_payer:
            raw_user_id = payload.get("payer_user_id") or event_data.get("payer_user_id")
            raw_name = payload.get("payer_name") or event_data.get("payer_name")
        else:
            raw_user_id = payload.get("sender_user_id") or event.get("user_id")
            raw_name = payload.get("sender_name") or event.get("display_name")
        name = str(raw_name or "玩家").strip() or "玩家"
        user_id = self._int_value(raw_user_id)
        return (user_id or self._synthetic_user_id(name), name)

    def _parse_bet_from_amount(self, amount: int) -> tuple[str, int] | None:
        if amount <= 0:
            return None
        price_base = int(self._cfg.get("price_base", 10000))
        if price_base <= 0:
            return None
        for number in self._draw_numbers():
            try:
                num_value = int(number)
            except ValueError:
                continue
            if amount <= num_value:
                continue
            rest = amount - num_value
            if rest % price_base:
                continue
            count = rest // price_base
            if count > 0:
                return number, count
        return None

    def _render_amount_help(self, amount: int = 0) -> str:
        sample_number = "3" if "3" in self._draw_numbers() else self._draw_numbers()[0]
        price_base = int(self._cfg.get("price_base", 10000))
        draw_hour, draw_minute = self._draw_clock()
        try:
            sample_amount = price_base + int(sample_number)
            sample_amount_more = price_base * 5 + int(sample_number)
        except ValueError:
            sample_amount = price_base
            sample_amount_more = price_base * 5
        prefix = f"❌ 转账金额 {amount} 无法识别为彩票下注。\n" if amount else ""
        return (
            prefix +
            "参与方式：对收款人的任意消息回复转账金额，金额 = 基础下注金额 × 注数 + 号码。\n"
            f"示例：+{sample_amount} 表示买 {sample_number} 号 1 注；+{sample_amount_more} 表示买 {sample_number} 号 5 注。\n"
            f"可选号码：{', '.join(self._draw_numbers())}\n"
            f"每日 {draw_hour:02d}:{draw_minute:02d} 开奖。"
        )

    def _render_interaction_prompt(self, message: str) -> str:
        prefix = self._command_prefix()
        return (
            f"🎟️ {message}\n"
            f"{self._render_amount_help()}\n"
            f"查询请使用 {prefix}{self._command} 我的 / {prefix}{self._command} 盘口 / {prefix}{self._command} 历史。"
        )

    def _init_aliases(self) -> None:
        def parse_alias(key: str, default: str) -> set[str]:
            raw = str(self._cfg.get(key, default) or default)
            return {x.strip().lower() for x in raw.split() if x.strip()}

        self._aliases = {
            "buy": parse_alias("buy_aliases", "买 buy bet"),
            "my": parse_alias("my_aliases", "我的 my"),
            "pool": parse_alias("pool_aliases", "盘口 pool"),
            "history": parse_alias("history_aliases", "历史 history"),
            "stats": parse_alias("stats_aliases", "统计 stats"),
            "hot": parse_alias("hot_aliases", "热度 hot trend"),
            "help": parse_alias("help_aliases", "帮助 help"),
            "draw": parse_alias("draw_aliases", "开奖 draw"),
            "reset": parse_alias("reset_aliases", "清盘 reset"),
            "sponsor": parse_alias("sponsor_aliases", "赞助 sponsor"),
            "unsponsor": parse_alias("unsponsor_aliases", "取消赞助 unsponsor"),
            "refund": parse_alias("refund_aliases", "退款 refund"),
        }

    def _get_lock(self, chat_id: int) -> asyncio.Lock:
        if chat_id not in self._locks:
            self._locks[chat_id] = asyncio.Lock()
        return self._locks[chat_id]

    def _get_state(self, chat_id: int) -> RoundState:
        if chat_id not in self._state:
            self._state[chat_id] = RoundState()
        return self._state[chat_id]

    def _draw_numbers(self) -> list[str]:
        raw = str(self._cfg.get("draw_numbers", "1,2,3,4,5,6"))
        nums = [x.strip() for x in raw.split(",") if x.strip()]
        return nums or ["1", "2", "3", "4", "5", "6"]

    def _is_admin(self, sender_id: int, account_id: int) -> bool:
        raw = str(self._cfg.get("admin_ids", "")).strip()
        if not raw:
            return sender_id == account_id
        admins = {x.strip() for x in raw.split(",") if x.strip()}
        return str(sender_id) in admins or sender_id == account_id

    def _alias_hit(self, kind: str, token: str) -> bool:
        return token.lower() in self._aliases.get(kind, set())

    @staticmethod
    def _command_prefix() -> str:
        return current_command_prefix(fallback=",")

    @staticmethod
    def _normalize_command_template(template: str) -> str:
        return str(template).replace(",{command}", "{prefix}{command}")

    def _render_template(self, template: str, payload: dict[str, Any]) -> str:
        normalized = self._normalize_command_template(template)
        try:
            return normalized.format_map(payload)
        except Exception:
            return normalized

    def _draw_clock(self) -> tuple[int, int]:
        hour = max(0, min(23, self._int_value(self._cfg.get("draw_hour", 21))))
        minute = max(0, min(59, self._int_value(self._cfg.get("draw_minute", 0))))
        return hour, minute

    def _close_minutes_before_draw(self) -> int:
        raw = self._cfg.get("close_minutes_before_draw", 1)
        try:
            return max(0, min(1440, int(raw)))
        except (TypeError, ValueError):
            return 1

    def _seconds_until_next_draw(self) -> float:
        now = datetime.now()
        hour, minute = self._draw_clock()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return max(1.0, (target - now).total_seconds())

    def _is_market_open(self) -> bool:
        now = datetime.now()
        hour, minute = self._draw_clock()
        draw_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        close_at = draw_at - timedelta(minutes=self._close_minutes_before_draw())
        return not (close_at <= now <= draw_at)

    async def _cmd_handler(self, client: Any, event: Any, args: list[str], account_id: int, ctx: PluginContext) -> None:
        chat_id = int(getattr(event.chat_id, "channel_id", None) or event.chat_id or 0)
        if not chat_id:
            return

        if not args:
            await event.reply(self._render_help(), parse_mode="html")
            return

        action = args[0].strip().lower()
        rest = args[1:]
        sender = await event.get_sender()
        sender_id = int(getattr(sender, "id", 0) or 0)
        sender_name = public_entity_display_name(sender, default="玩家")

        if self._alias_hit("help", action):
            await event.reply(self._render_help(), parse_mode="html")
            return

        lock = self._get_lock(chat_id)
        async with lock:
            st = self._get_state(chat_id)
            self._ensure_auto_draw(chat_id, ctx)

            if self._alias_hit("buy", action):
                await self._act_buy(event, st, sender_id, sender_name, rest)
                return
            if self._alias_hit("my", action):
                await self._act_my(event, st, sender_id)
                return
            if self._alias_hit("pool", action):
                await event.reply(f"💰 第 {st.round_id} 期当前奖池：<b>{int(st.jackpot)}</b>", parse_mode="html")
                return
            if self._alias_hit("history", action):
                await self._act_history(event, st, rest)
                return
            if self._alias_hit("stats", action):
                await self._act_stats(event, st)
                return
            if self._alias_hit("hot", action):
                await self._act_hot(event, st)
                return

            if not self._is_admin(sender_id, account_id):
                await event.reply("❌ 该操作仅管理员可用", parse_mode="html")
                return

            if self._alias_hit("draw", action):
                await self._draw_once(chat_id, event, st)
                return
            if self._alias_hit("reset", action):
                st.bets.clear()
                await event.reply(f"🧹 已清空第 {st.round_id} 期全部注单", parse_mode="html")
                return
            if self._alias_hit("sponsor", action):
                await self._act_sponsor(event, st, rest, positive=True)
                return
            if self._alias_hit("unsponsor", action):
                await self._act_sponsor(event, st, rest, positive=False)
                return
            if self._alias_hit("refund", action):
                await self._act_refund(event, st, rest)
                return

            await event.reply(self._render_help(), parse_mode="html")

    def _render_help(self) -> str:
        tmpl = str(self._cfg.get("help_template", "{command}"))
        price = int(self._cfg.get("price_base", 10000))
        draw_hour, draw_minute = self._draw_clock()
        sample = {
            "prefix": self._command_prefix(),
            "command": self._command,
            "round": "1",
            "number": "3",
            "count": "5",
            "cost": str(price * 5 + 3),
            "pool": "188888",
            "winners": "2",
            "payout": "53888",
            "history_limit": str(int(self._cfg.get("history_show_limit", 5))),
            "draw_numbers": ",".join(self._draw_numbers()),
            "draw_time": f"{draw_hour:02d}:{draw_minute:02d}",
            "close_minutes": str(self._close_minutes_before_draw()),
            "interval": "每日",
        }
        return self._render_template(tmpl, sample)

    async def _act_buy(self, event: Any, st: RoundState, user_id: int, user_name: str, rest: list[str]) -> None:
        if len(rest) < 2:
            await event.reply(f"❌ 用法：{self._command_prefix()}{self._command} 买 号码 注数", parse_mode="html")
            return
        await event.reply(self._place_bet(st, user_id, user_name, rest[0], rest[1]), parse_mode="html")

    def _place_bet(self, st: RoundState, user_id: int, user_name: str, number_raw: Any, count_raw: Any) -> str:
        if not self._is_market_open():
            hour, minute = self._draw_clock()
            return f"⛔️ <b>当前已封盘</b>，请等待 {hour:02d}:{minute:02d} 开奖后下期再参与。"
        number = str(number_raw).strip()
        if number not in self._draw_numbers():
            return f"❌ 号码无效，可选：{', '.join(self._draw_numbers())}"
        try:
            count = int(count_raw)
        except (TypeError, ValueError):
            count = 0
        if count <= 0:
            return "❌ 注数必须大于 0"

        max_bets_per_num = int(self._cfg.get("max_bets_per_num", 1000))
        user_number_bets = sum(b.count for b in st.bets if b.user_id == user_id and b.number == number)
        if user_number_bets + count > max_bets_per_num:
            return f"❌ 超出单号码注数上限：{max_bets_per_num}"

        max_numbers = int(self._cfg.get("max_numbers_per_user", 1))
        owned_numbers = {b.number for b in st.bets if b.user_id == user_id}
        if number not in owned_numbers and len(owned_numbers) >= max_numbers:
            return f"❌ 每期最多可买 {max_numbers} 个号码"

        price_base = int(self._cfg.get("price_base", 10000))
        cost = price_base * count + int(number)
        st.jackpot += cost
        st.bets.append(Bet(user_id=user_id, user_name=user_name, number=number, count=count, cost=cost))

        tmpl = str(self._cfg.get("bet_ok_template", "下注成功"))
        draw_hour, draw_minute = self._draw_clock()
        payload = {
            "prefix": self._command_prefix(),
            "command": self._command,
            "round": str(st.round_id),
            "number": number,
            "count": str(count),
            "cost": str(cost),
            "pool": str(int(st.jackpot)),
            "winners": "0",
            "payout": "0",
            "history_limit": str(int(self._cfg.get("history_show_limit", 5))),
            "draw_numbers": ",".join(self._draw_numbers()),
            "draw_time": f"{draw_hour:02d}:{draw_minute:02d}",
            "close_minutes": str(self._close_minutes_before_draw()),
            "interval": "每日",
        }
        return self._render_template(tmpl, payload)

    async def _act_my(self, event: Any, st: RoundState, user_id: int) -> None:
        await event.reply(self._render_my(st, user_id), parse_mode="html")

    def _render_my(self, st: RoundState, user_id: int) -> str:
        mine = [b for b in st.bets if b.user_id == user_id]
        if not mine:
            return f"📭 第 {st.round_id} 期你还没有下注"
        lines = [f"📒 第 {st.round_id} 期我的注单"]
        total = 0
        for idx, b in enumerate(mine, start=1):
            lines.append(f"{idx}. 号码 {b.number} · 注数 {b.count} · 扣款 {b.cost}")
            total += b.cost
        lines.append(f"\n合计扣款：<b>{total}</b>")
        return "\n".join(lines)

    async def _act_history(self, event: Any, st: RoundState, rest: list[str]) -> None:
        await event.reply(self._render_history(st, rest), parse_mode="html")

    def _render_history(self, st: RoundState, rest: list[str]) -> str:
        limit = int(self._cfg.get("history_show_limit", 5))
        if rest:
            try:
                limit = max(1, min(50, int(rest[0])))
            except ValueError:
                pass
        rows = st.history[-limit:]
        if not rows:
            return "📜 暂无开奖记录"
        rows.reverse()
        lines = [f"📜 最近 {len(rows)} 期开奖记录"]
        for item in rows:
            lines.append(
                f"第 {item['round']} 期：号 <b>{item['number']}</b> · 奖池 {item['pool']} · 中奖 {item['winners']} · 派发 {item['payout']}"
            )
        return "\n".join(lines)

    async def system_agent_list_recent_rounds(
        self,
        arguments: dict[str, Any],
        _ctx: Any = None,
    ) -> dict[str, Any]:
        """System Agent 只读工具：近期开奖轮次摘要。"""

        try:
            limit = int(arguments.get("limit") or 5)
        except (TypeError, ValueError):
            limit = 5
        limit = max(1, min(20, limit))
        chat_id = arguments.get("chat_id")
        items: list[dict[str, Any]] = []
        if chat_id not in (None, ""):
            try:
                cid = int(chat_id)
            except (TypeError, ValueError):
                return {"error": "invalid_chat_id", "business_changed": False}
            st = self._get_state(cid)
            for item in reversed(st.history[-limit:]):
                items.append(
                    {
                        "chat_id": cid,
                        "round": item.get("round"),
                        "number": item.get("number"),
                        "pool": item.get("pool"),
                        "winners": item.get("winners"),
                        "payout": item.get("payout"),
                    }
                )
        else:
            # 汇总各会话最近一期
            for cid, st in self._state.items():
                for item in reversed(st.history[-limit:]):
                    items.append(
                        {
                            "chat_id": cid,
                            "round": item.get("round"),
                            "number": item.get("number"),
                            "pool": item.get("pool"),
                            "winners": item.get("winners"),
                            "payout": item.get("payout"),
                        }
                    )
                    if len(items) >= limit:
                        break
                if len(items) >= limit:
                    break
        return {
            "count": len(items),
            "items": items[:limit],
            "business_changed": False,
        }

    async def _act_stats(self, event: Any, st: RoundState) -> None:
        await event.reply(self._render_stats(st), parse_mode="html")

    def _render_stats(self, st: RoundState) -> str:
        users = len({b.user_id for b in st.bets})
        total_bets = sum(b.count for b in st.bets)
        total_amount = sum(b.cost for b in st.bets)
        return (
            f"📈 第 {st.round_id} 期统计\n"
            f"👥 参与人数：{users}\n"
            f"📦 总注数：{total_bets}\n"
            f"💰 总投入：{total_amount}\n"
            f"🌊 当前奖池：{int(st.jackpot)}"
        )

    async def _act_hot(self, event: Any, st: RoundState) -> None:
        await event.reply(self._render_hot(st), parse_mode="html")

    def _render_hot(self, st: RoundState) -> str:
        if not st.bets:
            return "📊 当前暂无热度数据"
        c = Counter()
        for b in st.bets:
            c[b.number] += b.count
        lines = [f"🔥 第 {st.round_id} 期热度"]
        for number, cnt in c.most_common(10):
            lines.append(f"号码 {number}: {cnt} 注")
        return "\n".join(lines)

    async def _act_sponsor(self, event: Any, st: RoundState, rest: list[str], positive: bool) -> None:
        if not rest:
            await event.reply("❌ 用法：赞助/取消赞助 金额", parse_mode="html")
            return
        try:
            amount = int(rest[0])
        except ValueError:
            amount = 0
        if amount <= 0:
            await event.reply("❌ 金额必须大于 0", parse_mode="html")
            return
        if positive:
            st.jackpot += amount
            await event.reply(f"🎁 赞助成功 +{amount}，当前奖池 {int(st.jackpot)}", parse_mode="html")
        else:
            st.jackpot = max(0, st.jackpot - amount)
            await event.reply(f"💸 已扣减 {amount}，当前奖池 {int(st.jackpot)}", parse_mode="html")

    async def _act_refund(self, event: Any, st: RoundState, rest: list[str]) -> None:
        if not rest:
            await event.reply("❌ 用法：退款 注单序号（从 1 开始）", parse_mode="html")
            return
        try:
            idx = int(rest[0]) - 1
        except ValueError:
            idx = -1
        if idx < 0 or idx >= len(st.bets):
            await event.reply("❌ 注单序号不存在", parse_mode="html")
            return
        bet = st.bets.pop(idx)
        st.jackpot = max(0, st.jackpot - bet.cost)
        await event.reply(
            f"♻️ 已退款：{bet.user_name} 号码 {bet.number} 注数 {bet.count}，退回 {bet.cost}，当前奖池 {int(st.jackpot)}",
            parse_mode="html",
        )

    def _ensure_auto_draw(self, chat_id: int, ctx: PluginContext) -> None:
        job_name = f"_lottery_draw_{chat_id}"
        for t in self._tasks:
            if t.get_name() == job_name and not t.done():
                return
        task = asyncio.create_task(self._auto_draw_loop(chat_id, ctx), name=job_name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _auto_draw_loop(self, chat_id: int, ctx: PluginContext) -> None:
        while True:
            await asyncio.sleep(self._seconds_until_next_draw())
            lock = self._get_lock(chat_id)
            async with lock:
                st = self._get_state(chat_id)
                if not st.bets:
                    st.round_id += 1
                    if ctx.client:
                        try:
                            await ctx.client.send_message(chat_id, f"📢 第 {st.round_id - 1} 期无投注，自动进入第 {st.round_id} 期。")
                        except Exception:
                            pass
                    continue
                await self._draw_once(chat_id, None, st, ctx)

    async def _draw_once(self, chat_id: int, event: Any | None, st: RoundState, ctx: PluginContext | None = None) -> None:
        lucky = random.choice(self._draw_numbers())
        all_winners = [b for b in st.bets if b.number == lucky]
        fee = float(self._cfg.get("service_fee_rate", 0.05))
        refund_per = int(self._cfg.get("refund_per_action", 66))
        max_payout = int(self._cfg.get("max_payout_per_user", 5000000))

        total_win_count = sum(b.count for b in all_winners)
        payout_pool = int(st.jackpot * 0.6)
        paid = 0
        if total_win_count > 0:
            per = payout_pool / total_win_count
            user_paid: dict[int, int] = {}
            for b in all_winners:
                raw = int(per * b.count)
                after_fee = max(0, int(raw * (1 - fee)))
                reward = after_fee + b.count * refund_per
                user_paid[b.user_id] = user_paid.get(b.user_id, 0) + reward

            for uid, amount in user_paid.items():
                amount = min(amount, max_payout)
                paid += amount
                if event is not None:
                    await event.reply(f"+{amount}")
                elif ctx and ctx.client:
                    try:
                        await ctx.client.send_message(chat_id, f"+{amount}")
                    except Exception:
                        pass

        st.jackpot = max(0, st.jackpot - paid)
        result = {
            "round": st.round_id,
            "number": lucky,
            "pool": int(st.jackpot),
            "winners": len({b.user_id for b in all_winners}),
            "payout": paid,
        }
        st.history.append(result)

        tmpl = str(self._cfg.get("draw_template", "开奖"))
        draw_hour, draw_minute = self._draw_clock()
        payload = {
            "prefix": self._command_prefix(),
            "command": self._command,
            "round": str(st.round_id),
            "number": str(lucky),
            "count": str(total_win_count),
            "cost": "0",
            "pool": str(result["pool"]),
            "winners": str(result["winners"]),
            "payout": str(result["payout"]),
            "history_limit": str(int(self._cfg.get("history_show_limit", 5))),
            "draw_numbers": ",".join(self._draw_numbers()),
            "draw_time": f"{draw_hour:02d}:{draw_minute:02d}",
            "close_minutes": str(self._close_minutes_before_draw()),
            "interval": "每日",
        }
        msg = self._render_template(tmpl, payload)

        if event is not None:
            await event.reply(msg, parse_mode="html")
        elif ctx and ctx.client:
            try:
                await ctx.client.send_message(chat_id, msg, parse_mode="html")
            except Exception:
                pass

        st.round_id += 1
        st.bets.clear()
