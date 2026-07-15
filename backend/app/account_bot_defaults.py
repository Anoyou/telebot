"""Shared defaults for account Bot interaction configuration."""

DEFAULT_INTERACTION_DISABLED_MESSAGE = "本条互动规则已暂停，暂时不能开启。"
DEFAULT_INTERACTION_MODULE_START_TEXT = "正在启动{规则名称}"
DEFAULT_INTERACTION_QUERY_COMMANDS = ["。玩法", "。联动玩法"]
DEFAULT_INTERACTION_QUERY_RESPONSE_TEMPLATE = "<b>当前可用联动玩法</b>\n{items}"
DEFAULT_INTERACTION_QUERY_ITEM_TEMPLATE = "{index}. <b>{name}</b>\n触发方式：{trigger}"
DEFAULT_INTERACTION_QUERY_EMPTY_MESSAGE = "当前群暂无开启中的联动玩法。"
DEFAULT_INTERACTION_RESPONSE_TEMPLATE = "已收到 {payer_name} 给 {receiver_name} 的转账 {amount}，互动流程已准备就绪。"
LEGACY_TRANSFER_NOTICE_TEMPLATE = "\n".join(
    (
        "转账成功",
        "付款人：{payer_name}",
        "{payer_user_id_line}",
        "收款人：{receiver_name}",
        "金额：{amount}",
        "{receiver_user_id_line}",
    )
)
DEFAULT_TRANSFER_NOTICE_TEMPLATE = "\n".join(
    (
        '<pre><code class="language-转账成功">付款人：{payer_name}',
        "{payer_user_id_line}",
        "收款人：{receiver_name}",
        "金额：{amount}",
        "{receiver_user_id_line}</code></pre>",
    )
)
DEFAULT_DEBIT_NOTICE_TEMPLATE = "\n".join(
    (
        '<pre><code class="language-扣减成功">{payer_name} 扣减 {amount} 蝌蚪',
        "{receiver_name} 接收 {amount} 蝌蚪</code></pre>",
    )
)
