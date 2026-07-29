"""彩票系统 Plus 远程插件 Manifest。"""

from __future__ import annotations

from app.worker.plugins.manifest import Manifest


SAMPLE = {
    "prefix": "{prefix}",
    "command": "lotto",
    "round": "12",
    "number": "3",
    "count": "5",
    "cost": "50003",
    "pool": "188888",
    "winners": "2",
    "payout": "53888",
    "history_limit": "5",
    "draw_numbers": "1-6",
    "draw_time": "21:00",
    "close_minutes": "1",
}


def _render(template: str) -> str:
    try:
        return template.format_map(SAMPLE)
    except Exception:
        return template


HELP_TEMPLATE_DEFAULT = (
    "<b>🎰 彩票系统</b>\n"
    "指令：{prefix}{command} 买 号码 注数｜{prefix}{command} 我的｜{prefix}{command} 盘口｜{prefix}{command} 历史\n"
    "示例：{prefix}{command} 买 {number} {count}（本例成本 {cost}）\n"
    "每日 {draw_time} 开奖，封盘提前 {close_minutes} 分钟，号码范围：{draw_numbers}"
)
BET_OK_TEMPLATE_DEFAULT = (
    "✅ 第 {round} 期下注成功\n"
    "号码：<b>{number}</b> · 注数：<b>{count}</b>\n"
    "扣款：<b>{cost}</b> · 当前奖池：<b>{pool}</b>"
)
DRAW_TEMPLATE_DEFAULT = (
    "🎲 第 {round} 期开奖结果：<b>{number}</b>\n"
    "奖池：<b>{pool}</b> · 中奖人数：<b>{winners}</b>\n"
    "总派发：<b>{payout}</b>"
)

CONFIG_SCHEMA = {
    "type": "object",
    "x-ui-mode": "single",
    "additionalProperties": False,
    "properties": {
        "usage_preview": {
            "type": "string",
            "title": "玩法预览（只读）",
            "readOnly": True,
            "default": (
                "彩票系统支持命令和交互 Bot 两种入口。\n"
                "{prefix}{command} 帮助：查看玩法说明。\n"
                "{prefix}{command} 买 3 5：买 3 号 5 注，扣款按“基础下注金额 × 注数 + 号码”计算。\n"
                "{prefix}{command} 我的 / 盘口 / 热度 / 统计 / 历史：查询本期与往期信息。\n"
                "交互 Bot 入口会把正常用户转账自动判定为下注；奖池、历史、热度、我的注单仍使用上面的查询命令。"
            ),
        },
        "command": {
            "type": "string",
            "title": "触发指令名",
            "default": "lotto",
            "minLength": 1,
            "maxLength": 32,
            "pattern": "^\\S+$",
        },
        "buy_aliases": {"type": "string", "title": "下注别名", "default": "买 buy bet"},
        "my_aliases": {"type": "string", "title": "我的注单别名", "default": "我的 my"},
        "pool_aliases": {"type": "string", "title": "盘口别名", "default": "盘口 pool"},
        "history_aliases": {"type": "string", "title": "历史别名", "default": "历史 history"},
        "stats_aliases": {"type": "string", "title": "统计别名", "default": "统计 stats"},
        "hot_aliases": {"type": "string", "title": "热度别名", "default": "热度 hot trend"},
        "help_aliases": {"type": "string", "title": "帮助别名", "default": "帮助 help"},
        "draw_aliases": {"type": "string", "title": "开奖别名（管理员）", "default": "开奖 draw"},
        "reset_aliases": {"type": "string", "title": "清盘别名（管理员）", "default": "清盘 reset"},
        "sponsor_aliases": {"type": "string", "title": "赞助别名（管理员）", "default": "赞助 sponsor"},
        "unsponsor_aliases": {"type": "string", "title": "取消赞助别名（管理员）", "default": "取消赞助 unsponsor"},
        "refund_aliases": {"type": "string", "title": "退款别名（管理员）", "default": "退款 refund"},
        "price_base": {
            "type": "integer",
            "title": "基础下注金额",
            "default": 10000,
            "minimum": 1,
            "maximum": 100000000,
        },
        "service_fee_rate": {
            "type": "number",
            "title": "中奖服务费比例",
            "default": 0.05,
            "minimum": 0,
            "maximum": 0.9,
        },
        "refund_per_action": {
            "type": "integer",
            "title": "中奖额外返还（每注）",
            "default": 66,
            "minimum": 0,
            "maximum": 100000,
        },
        "max_numbers_per_user": {
            "type": "integer",
            "title": "每人每期可买号码数",
            "default": 1,
            "minimum": 1,
            "maximum": 20,
        },
        "max_bets_per_num": {
            "type": "integer",
            "title": "单号码最大注数",
            "default": 1000,
            "minimum": 1,
            "maximum": 1000000,
        },
        "max_payout_per_user": {
            "type": "integer",
            "title": "单人单期最大派发上限",
            "default": 5000000,
            "minimum": 1,
            "maximum": 1000000000,
        },
        "draw_numbers": {
            "type": "string",
            "title": "可开奖号码（逗号分隔）",
            "default": "1,2,3,4,5,6",
            "minLength": 1,
            "maxLength": 120,
        },
        "draw_hour": {
            "type": "integer",
            "title": "每日开奖小时",
            "default": 21,
            "minimum": 0,
            "maximum": 23,
        },
        "draw_minute": {
            "type": "integer",
            "title": "每日开奖分钟",
            "default": 0,
            "minimum": 0,
            "maximum": 59,
        },
        "close_minutes_before_draw": {
            "type": "integer",
            "title": "开奖前封盘分钟数",
            "default": 1,
            "minimum": 0,
            "maximum": 1440,
        },
        "auto_draw_interval_sec": {
            "type": "integer",
            "title": "旧版开奖间隔（兼容保留，当前使用每日开奖时间）",
            "default": 86400,
            "minimum": 30,
            "maximum": 86400,
        },
        "history_show_limit": {
            "type": "integer",
            "title": "历史默认展示期数",
            "default": 5,
            "minimum": 1,
            "maximum": 50,
        },
        "admin_ids": {
            "type": "string",
            "title": "管理员用户ID列表",
            "default": "",
            "description": "逗号分隔；为空时仅允许当前账号触发管理动作。",
        },
        "help_template": {
            "type": "string",
            "title": "帮助消息模板",
            "default": HELP_TEMPLATE_DEFAULT,
            "minLength": 1,
            "maxLength": 1500,
        },
        "bet_ok_template": {
            "type": "string",
            "title": "下注成功模板",
            "default": BET_OK_TEMPLATE_DEFAULT,
            "minLength": 1,
            "maxLength": 1000,
        },
        "draw_template": {
            "type": "string",
            "title": "开奖消息模板",
            "default": DRAW_TEMPLATE_DEFAULT,
            "minLength": 1,
            "maxLength": 1200,
        },
        "template_placeholders": {
            "type": "string",
            "title": "模板占位符说明（只读）",
            "readOnly": True,
            "default": "{prefix} 当前 userbot 全局指令前缀，{command} 指令名，{round} 期号，{number} 号码，{count} 注数，{cost} 金额，{pool} 奖池，{winners} 中奖人数，{payout} 派发金额，{draw_numbers} 开奖号码范围，{draw_time} 每日开奖时间，{close_minutes} 封盘提前分钟数。",
        },
        "help_preview": {
            "type": "string",
            "title": "帮助模板预览（只读）",
            "readOnly": True,
            "default": _render(HELP_TEMPLATE_DEFAULT),
        },
        "bet_ok_preview": {
            "type": "string",
            "title": "下注模板预览（只读）",
            "readOnly": True,
            "default": _render(BET_OK_TEMPLATE_DEFAULT),
        },
        "draw_preview": {
            "type": "string",
            "title": "开奖模板预览（只读）",
            "readOnly": True,
            "default": _render(DRAW_TEMPLATE_DEFAULT),
        },
    },
    "required": ["command", "price_base", "service_fee_rate", "draw_numbers", "draw_hour", "draw_minute"],
}


MANIFEST = Manifest(
    key="lottery_plus",
    display_name="彩票系统 Plus",
    version="1.0.5",
    min_telebot_version="0.10.0",
    author="Anoyou",
    description="群内彩票玩法，支持下注、奖池滚存、自动开奖、历史和消息模板预览",
    permissions=["send_message", "edit_message", "read_chat", "delete_message", "ai_agent"],
    category="interactive",
    capabilities={"agent_tools": {"enabled": True}},
    agent_keywords=["彩票", "开奖", "lottery", "奖池", "下注", "期号"],
    agent_tools=[
        {
            "name": "list_recent_rounds",
            "description": "查询近期开奖轮次摘要（只读）",
            "read_only": True,
            "expose": ["system_agent"],
            "parameters": {
                "type": "object",
                "properties": {
                    "account_id": {
                        "type": "integer",
                        "description": "账号 ID（System Agent 调用时必填）",
                    },
                    "chat_id": {
                        "type": "integer",
                        "description": "群聊 ID；缺省汇总所有已知会话的历史",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "返回条数，默认 5，最大 20",
                    },
                },
                "additionalProperties": False,
            },
        }
    ],
    interaction_entries=[
        {
            "key": "start_lottery_plus",
            "title": "发起彩票下注",
            "description": "由交互 Bot 接收转账通知后按金额自动下注；查询仍使用彩票模块自己的命令入口。",
            "interaction_profile": "reward_pool",
            "launch_mode": "hybrid",
            "session_scope": "chat",
            "events": ["payment_confirmed", "message", "session_close"],
            "triggers": {
                "command": "lotto",
            },
            "preserve_command_trigger": True,
            "command_fallback": {
                "enabled": True,
                "command": "lotto",
                "mode": "hint_only",
            },
            "payload_contract": {
                "required_envelope": ["source", "actor", "trigger", "session"],
                "required_event_fields": ["type", "chat_id"],
            },
            "result_contract": {
                "actions": ["send_message", "end_session"],
                "send_via": ["interaction_bot", "userbot_reply"],
            },
            "settlement": {
                "mode": "announce_only",
                "winner_field": "actor.user_id",
                "amount_field": "message.amount",
            },
            "input_schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "message": {"type": "string", "title": "自定义提示", "default": "开始下注，祝你好运。"}
                },
            },
        }
    ],
    config_schema=CONFIG_SCHEMA,
)

__all__ = ["MANIFEST"]
