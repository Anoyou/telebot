"""Config Bundle 导出 / dry-run 的纯函数工具。"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models.command import AccountCommandLink, CommandTemplate
from ..db.models.feature import AccountFeature
from ..db.models.ignored_peer import IgnoredPeer
from ..db.models.rule import Rule
from ..schemas.config_bundle import (
    ConfigBundleCommandLinkItem,
    ConfigBundleDiffCounts,
    ConfigBundleDiffItem,
    ConfigBundleDryRunResponse,
    ConfigBundleExport,
    ConfigBundleFeatureItem,
    ConfigBundleIgnoredPeerItem,
    ConfigBundleRuleItem,
    ConfigBundleSourceAccount,
)
from .redactor import redact_value
from .scheduler_target import SchedulerTargetError, normalize_scheduler_action_target

MAX_BUNDLE_BYTES = 1_048_576

class BundleTooLarge(ValueError):
    """导出 / 上传 bundle 超过 1MB。"""

    def __init__(self, size_bytes: int) -> None:
        super().__init__(f"bundle too large: {size_bytes}")
        self.size_bytes = size_bytes


class BundleConfirmError(ValueError):
    """confirm 阶段的业务校验错误。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def sanitize_bundle_value(value: Any) -> Any:
    """递归移除 bundle 里的敏感字段。"""
    return redact_value(value, drop_sensitive_keys=True)


def bundle_json_bytes(bundle: ConfigBundleExport) -> bytes:
    """把 bundle 序列化成紧凑 JSON。"""
    payload = bundle.model_dump(mode="json")
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def assert_bundle_size(bundle: ConfigBundleExport) -> bytes:
    """返回序列化字节；超限则抛 ``BundleTooLarge``。"""
    data = bundle_json_bytes(bundle)
    if len(data) > MAX_BUNDLE_BYTES:
        raise BundleTooLarge(len(data))
    return data


def _config_diff_fields(source_cfg: dict[str, Any], target_cfg: dict[str, Any]) -> list[str]:
    keys = sorted(set(source_cfg) | set(target_cfg))
    return [key for key in keys if source_cfg.get(key) != target_cfg.get(key)]


_CHAT_ID_EXACT_KEYS = {"chat_id", "target_chat_id"}
_CHAT_ID_HINT_KEYS = {
    "source_peers",
    "group_ids",
    "peer",
    "peers",
    "chat",
    "chats",
    "group",
    "groups",
}
_CHAT_ID_HINT_PARTS = ("peer", "chat", "group")


def _contains_chat_id_signal(path: str, key: str, value: Any) -> bool:
    lowered_key = key.lower()
    lowered_path = path.lower()
    if lowered_key in _CHAT_ID_EXACT_KEYS:
        return True
    if lowered_key in _CHAT_ID_HINT_KEYS and isinstance(value, (list, tuple, dict, int, str)):
        return True
    if lowered_key.endswith("_chat_id") or lowered_key.endswith("_peer_id") or lowered_key.endswith("_group_id"):
        return True
    if any(part in lowered_key for part in _CHAT_ID_HINT_PARTS) and lowered_key.endswith("_id"):
        return True
    if lowered_path.endswith(".action.target_chat_id"):
        return True
    return False


def _collect_chat_id_paths(value: Any, path: str = "config") -> set[str]:
    out: set[str] = set()
    if isinstance(value, dict):
        for raw_key, raw_item in value.items():
            key = str(raw_key)
            next_path = f"{path}.{key}"
            if _contains_chat_id_signal(next_path, key, raw_item):
                out.add(next_path)
            out.update(_collect_chat_id_paths(raw_item, next_path))
        return out
    if isinstance(value, list):
        for idx, item in enumerate(value):
            out.update(_collect_chat_id_paths(item, f"{path}[{idx}]"))
    return out


def rule_has_chat_id_conflict(source_cfg: dict[str, Any], target_cfg: dict[str, Any]) -> bool:
    src_paths = _collect_chat_id_paths(source_cfg)
    dst_paths = _collect_chat_id_paths(target_cfg)
    for path in sorted(src_paths | dst_paths):
        src_val = _read_path(source_cfg, path)
        dst_val = _read_path(target_cfg, path)
        if src_val != dst_val:
            return True
    return False


def _read_path(cfg: Any, path: str) -> Any:
    cur = cfg
    token = path.removeprefix("config.")
    segments: list[str] = []
    buff = ""
    for ch in token:
        if ch == ".":
            if buff:
                segments.append(buff)
                buff = ""
            continue
        buff += ch
    if buff:
        segments.append(buff)
    for seg in segments:
        if "[" in seg and seg.endswith("]"):
            name, _, rest = seg.partition("[")
            if name:
                if not isinstance(cur, dict):
                    return None
                cur = cur.get(name)
            if not isinstance(cur, list):
                return None
            try:
                idx = int(rest[:-1])
            except ValueError:
                return None
            if idx < 0 or idx >= len(cur):
                return None
            cur = cur[idx]
            continue
        if not isinstance(cur, dict):
            return None
        cur = cur.get(seg)
    return cur


def build_preview_signature(
    *,
    account_id: int,
    file_content: bytes,
    apply_conflicts: bool,
    confirm_chat_id_conflicts: bool,
    preview_context_digest: str = "",
) -> str:
    digest = hashlib.sha256(file_content).hexdigest()
    raw = (
        f"{account_id}:{digest}:{int(apply_conflicts)}:"
        f"{int(confirm_chat_id_conflicts)}:{preview_context_digest}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_preview_context_digest(
    *,
    target: ConfigBundleExport,
    available_features: dict[str, str],
    available_command_templates: dict[str, dict[str, Any]],
) -> str:
    """把 dry-run 依赖的目标状态压成摘要，避免 stale preview 被确认写入。"""
    payload = {
        "target": target.model_dump(mode="json"),
        "available_features": available_features,
        "available_command_templates": available_command_templates,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _build_source_account(account) -> ConfigBundleSourceAccount:
    label = (
        getattr(account, "display_name", None)
        or getattr(account, "phone", None)
        or f"account-{account.id}"
    )
    return ConfigBundleSourceAccount(id=int(account.id), label=str(label))


def build_config_bundle(
    account,
    feature_rows,
    rule_rows,
    command_link_rows,
    ignored_peer_rows=None,
) -> ConfigBundleExport:
    """把账号及其配置行拼成 bundle。"""
    features: dict[str, ConfigBundleFeatureItem] = {}
    for row in sorted(feature_rows, key=lambda r: str(r.feature_key)):
        features[str(row.feature_key)] = ConfigBundleFeatureItem(
            feature_key=str(row.feature_key),
            enabled=bool(row.enabled),
            config=sanitize_bundle_value(dict(row.config or {})),
        )

    rules = [
        ConfigBundleRuleItem(
            feature_key=str(row.feature_key),
            name=str(row.name),
            enabled=bool(row.enabled),
            priority=int(row.priority),
            config=sanitize_bundle_value(dict(row.config or {})),
        )
        for row in sorted(
            rule_rows,
            key=lambda r: (str(r.feature_key), -int(r.priority), str(r.name)),
        )
    ]

    command_links = [
        ConfigBundleCommandLinkItem(
            template_id=int(tpl.id),
            template_name=str(tpl.name),
            aliases=list(tpl.aliases or []),
            type=str(tpl.type),
            enabled=True,
        )
        for _link_row, tpl in sorted(command_link_rows, key=lambda item: str(item[1].name))
    ]

    ignored_peers = [
        ConfigBundleIgnoredPeerItem(
            peer_id=int(row.peer_id),
            peer_kind=str(row.peer_kind),
            peer_label=row.peer_label,
        )
        for row in sorted(ignored_peer_rows or [], key=lambda r: int(r.peer_id))
    ]

    return ConfigBundleExport(
        source_account=_build_source_account(account),
        rules=rules,
        features=features,
        command_links=command_links,
        ignored_peers=ignored_peers,
    )


def _index_bundle(bundle: ConfigBundleExport) -> dict[str, dict[str, Any]]:
    features = {k: v.model_dump(mode="json") for k, v in bundle.features.items()}
    rules = {
        f"{item.feature_key}:{item.name}": item.model_dump(mode="json")
        for item in bundle.rules
    }
    commands = {item.template_name: item.model_dump(mode="json") for item in bundle.command_links}
    ignored_peers = {str(item.peer_id): item.model_dump(mode="json") for item in bundle.ignored_peers}
    return {
        "features": features,
        "rules": rules,
        "command_links": commands,
        "ignored_peers": ignored_peers,
    }


def compare_bundles(
    source: ConfigBundleExport,
    target: ConfigBundleExport,
    *,
    available_features: dict[str, str],
    available_command_templates: dict[str, dict[str, Any]],
) -> ConfigBundleDryRunResponse:
    """把 source bundle 和 target bundle 做 dry-run 对比。"""
    src = _index_bundle(source)
    dst = _index_bundle(target)
    items: list[ConfigBundleDiffItem] = []
    counts = ConfigBundleDiffCounts()
    warnings: list[str] = []

    for feature_key, item in src["features"].items():
        if feature_key not in available_features:
            items.append(
                ConfigBundleDiffItem(
                    entity="feature",
                    key=feature_key,
                    action="conflict",
                    conflict_kind="blocked",
                    fields=["feature_key"],
                    note="feature not registered",
                )
            )
            counts.conflict += 1
            continue
        current = dst["features"].get(feature_key)
        if current is None:
            items.append(ConfigBundleDiffItem(entity="feature", key=feature_key, action="add"))
            counts.add += 1
            continue
        changed = []
        if item["enabled"] != current["enabled"]:
            changed.append("enabled")
        changed.extend(_config_diff_fields(dict(item["config"]), dict(current["config"])))
        if changed:
            items.append(
                ConfigBundleDiffItem(
                    entity="feature",
                    key=feature_key,
                    action="conflict",
                    conflict_kind="overridable",
                    fields=sorted(dict.fromkeys(changed)),
                )
            )
            counts.conflict += 1
        else:
            items.append(ConfigBundleDiffItem(entity="feature", key=feature_key, action="skip"))
            counts.skip += 1

    for rule_key, item in src["rules"].items():
        feature_key = str(item["feature_key"])
        if feature_key not in available_features:
            items.append(
                ConfigBundleDiffItem(
                    entity="rule",
                    key=rule_key,
                    action="conflict",
                    conflict_kind="blocked",
                    fields=["feature_key"],
                    note="feature not registered",
                )
            )
            counts.conflict += 1
            continue
        current = dst["rules"].get(rule_key)
        if current is None:
            items.append(ConfigBundleDiffItem(entity="rule", key=rule_key, action="add"))
            counts.add += 1
            continue
        changed = []
        if item["enabled"] != current["enabled"]:
            changed.append("enabled")
        if int(item["priority"]) != int(current["priority"]):
            changed.append("priority")
        changed.extend(_config_diff_fields(dict(item["config"]), dict(current["config"])))
        if rule_has_chat_id_conflict(dict(item["config"]), dict(current["config"])):
            changed.append("chat_id")
        if changed:
            items.append(
                ConfigBundleDiffItem(
                    entity="rule",
                    key=rule_key,
                    action="conflict",
                    conflict_kind="overridable",
                    fields=sorted(dict.fromkeys(changed)),
                )
            )
            counts.conflict += 1
        else:
            items.append(ConfigBundleDiffItem(entity="rule", key=rule_key, action="skip"))
            counts.skip += 1

    for template_name, item in src["command_links"].items():
        available = available_command_templates.get(template_name)
        if available is None:
            items.append(
                ConfigBundleDiffItem(
                    entity="command_link",
                    key=template_name,
                    action="conflict",
                    conflict_kind="blocked",
                    fields=["template_name"],
                    note="command template not registered",
                )
            )
            counts.conflict += 1
            continue
        changed = []
        for field in ("template_name", "aliases", "type"):
            if item.get(field) != available.get(field):
                changed.append(field)
        if changed:
            items.append(
                ConfigBundleDiffItem(
                    entity="command_link",
                    key=template_name,
                    action="conflict",
                    conflict_kind="blocked",
                    fields=sorted(dict.fromkeys(changed)),
                    note="template metadata changed",
                )
            )
            counts.conflict += 1
            continue
        current = dst["command_links"].get(template_name)
        if current is None:
            items.append(
                ConfigBundleDiffItem(entity="command_link", key=template_name, action="add")
            )
            counts.add += 1
        else:
            items.append(
                ConfigBundleDiffItem(entity="command_link", key=template_name, action="skip")
            )
            counts.skip += 1

    for peer_key, item in src["ignored_peers"].items():
        current = dst["ignored_peers"].get(peer_key)
        if current is None:
            items.append(ConfigBundleDiffItem(entity="ignored_peer", key=peer_key, action="add"))
            counts.add += 1
            continue
        changed = []
        for field in ("peer_kind", "peer_label"):
            if item.get(field) != current.get(field):
                changed.append(field)
        if changed:
            items.append(
                ConfigBundleDiffItem(
                    entity="ignored_peer",
                    key=peer_key,
                    action="conflict",
                    conflict_kind="overridable",
                    fields=sorted(dict.fromkeys(changed)),
                )
            )
            counts.conflict += 1
        else:
            items.append(ConfigBundleDiffItem(entity="ignored_peer", key=peer_key, action="skip"))
            counts.skip += 1

    return ConfigBundleDryRunResponse(
        source_account=source.source_account,
        target_account=target.source_account,
        size_bytes=len(bundle_json_bytes(source)),
        counts=counts,
        items=items,
        warnings=warnings,
    )


async def apply_bundle_confirm(
    db: AsyncSession,
    *,
    account_id: int,
    source: ConfigBundleExport,
    dry_run: ConfigBundleDryRunResponse,
    available_command_templates: dict[str, dict[str, Any]],
    apply_conflicts: bool,
    confirm_chat_id_conflicts: bool,
) -> tuple[int, int, int, list[str]]:
    """按 dry-run 结果把 bundle 写入目标账号。"""
    imported = 0
    skipped = 0
    conflicts = 0
    warnings: list[str] = []
    src_features = source.features
    src_rules = {f"{item.feature_key}:{item.name}": item for item in source.rules}
    src_cmds = {item.template_name: item for item in source.command_links}
    src_ignored_peers = {str(item.peer_id): item for item in source.ignored_peers}

    if apply_conflicts and not confirm_chat_id_conflicts:
        has_chat_id_conflict = any(
            item.action == "conflict" and item.entity == "rule" and "chat_id" in set(item.fields)
            for item in dry_run.items
        )
        if has_chat_id_conflict:
            raise BundleConfirmError(
                "CHAT_ID_CONFIRM_REQUIRED",
                "存在 chat_id 冲突，请先确认后再写入",
            )

    template_rows = (await db.execute(select(CommandTemplate))).scalars().all()
    template_by_name = {row.name: row for row in template_rows}

    for item in dry_run.items:
        key = item.key
        if item.action == "skip":
            skipped += 1
            continue
        if item.action == "conflict":
            if item.conflict_kind == "blocked":
                conflicts += 1
                continue
            if not apply_conflicts:
                conflicts += 1
                continue

        if item.entity == "feature":
            src = src_features.get(key)
            if src is None:
                conflicts += 1
                warnings.append(f"missing feature payload: {key}")
                continue
            await db.execute(
                delete(AccountFeature).where(
                    AccountFeature.account_id == account_id,
                    AccountFeature.feature_key == key,
                )
            )
            db.add(
                AccountFeature(
                    account_id=account_id,
                    feature_key=src.feature_key,
                    enabled=src.enabled,
                    config=dict(src.config or {}),
                )
            )
            imported += 1
            continue

        if item.entity == "rule":
            src_rule = src_rules.get(key)
            if src_rule is None:
                conflicts += 1
                warnings.append(f"missing rule payload: {key}")
                continue
            rule_config = dict(src_rule.config or {})
            if src_rule.feature_key == "scheduler":
                try:
                    rule_config = normalize_scheduler_action_target(rule_config)
                except SchedulerTargetError as exc:
                    conflicts += 1
                    warnings.append(f"invalid scheduler target in {key}: {exc}")
                    continue
            await db.execute(
                delete(Rule).where(
                    Rule.account_id == account_id,
                    Rule.feature_key == src_rule.feature_key,
                    Rule.name == src_rule.name,
                )
            )
            db.add(
                Rule(
                    account_id=account_id,
                    feature_key=src_rule.feature_key,
                    name=src_rule.name,
                    enabled=src_rule.enabled,
                    priority=src_rule.priority,
                    config=rule_config,
                )
            )
            imported += 1
            continue

        if item.entity == "command_link":
            src_cmd = src_cmds.get(key)
            if src_cmd is None:
                conflicts += 1
                warnings.append(f"missing command payload: {key}")
                continue
            tpl = template_by_name.get(src_cmd.template_name)
            if tpl is None or src_cmd.template_name not in available_command_templates:
                conflicts += 1
                warnings.append(f"command template not found: {src_cmd.template_name}")
                continue
            await db.execute(
                delete(AccountCommandLink).where(
                    AccountCommandLink.account_id == account_id,
                    AccountCommandLink.template_id == int(tpl.id),
                )
            )
            db.add(
                AccountCommandLink(
                    account_id=account_id,
                    template_id=int(tpl.id),
                    enabled=True,
                )
            )
            imported += 1
            continue

        if item.entity == "ignored_peer":
            src_peer = src_ignored_peers.get(key)
            if src_peer is None:
                conflicts += 1
                warnings.append(f"missing ignored peer payload: {key}")
                continue
            await db.execute(
                delete(IgnoredPeer).where(
                    IgnoredPeer.account_id == account_id,
                    IgnoredPeer.peer_id == int(src_peer.peer_id),
                )
            )
            db.add(
                IgnoredPeer(
                    account_id=account_id,
                    peer_id=int(src_peer.peer_id),
                    peer_kind=src_peer.peer_kind,
                    peer_label=src_peer.peer_label,
                )
            )
            imported += 1
            continue

        conflicts += 1
        warnings.append(f"unknown entity: {item.entity}")

    return imported, skipped, conflicts, warnings
